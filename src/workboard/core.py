#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""WorkBoard core: schema, board lookup, registry, crash-safe persistence.

Every board lives in the per-user store, home()/boards/<dir>/, and the registry
(home()/boards.json) links each board to one project folder. A board is
board.db (SQLite, one row per card, WAL) plus board.json, a schema-5 stub that
older installs refuse. A write is one SQLite transaction that rewrites only the
cards it changed and keeps their previous bodies in card_versions for restore.
Nothing here starts a server or opens a browser.
"""
from __future__ import annotations

import contextlib
import datetime
import functools
import json
import os
import re
import sys
import time
import stat
import threading
from pathlib import Path

from . import __version__, usage

SCHEMA_VERSION = 4  # The logical document's schema; every saved board is schema 4.
SUPPORTED_SCHEMA_VERSIONS = (3, SCHEMA_VERSION)  # What a legacy board.json may hold to migrate.
API_VERSION = 2
CAPABILITIES = ("context", "attachment-cli", "shared-attachments", "expected-rev",
                "schema-guard", "delegated-subtasks", "focused-context", "handoff",
                "write-scopes", "review-inbox", "subtask-notes")
LOCK_NAME = ".board.lock"
BACKUP_DIR = ".backups"
ARCHIVE_DIR = "archive"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
NOTE_SUMMARY_MAX = 160
NOTE_BODY_MAX = 32000
PROTECTED_CARD_FIELDS = frozenset({
    "activeOwner", "claimedAt", "dependsOn", "outcome", "cancelReason",
    "reworkReason", "comments", "attachments", "cycles", "doneAt",
    "reopenReason", "blockedReason", "unblockWhen", "blockedAt",
    "verification", "reviews", "log",
    "id", "num", "createdAt", "updatedAt", "history", "changedRev",
})


def home() -> Path:
    """Per-user WorkBoard state directory: $WORKBOARD_HOME, else ~/.workboard (resolved per call)."""
    configured = os.environ.get("WORKBOARD_HOME")
    return Path(configured).expanduser().absolute() if configured else Path.home() / ".workboard"


def registry_path() -> Path:
    return home() / "boards.json"


def server_state_path() -> Path:
    return home() / "server.json"


def logs_dir() -> Path:
    return home() / "logs"


def boards_dir() -> Path:
    return home() / "boards"


def deleted_dir() -> Path:
    return home() / "deleted"


DEFAULT_COLUMNS = [
    {"id": "backlog", "name": "Backlog", "kind": "todo"},
    {"id": "task", "name": "Task", "kind": "todo"},
    {"id": "inprogress", "name": "In Progress", "kind": "active"},
    {"id": "done", "name": "Done", "kind": "done"},
    {"id": "blocked", "name": "Blocked", "kind": "blocked"},
]
CORE_COLUMN_IDS = tuple(c["id"] for c in DEFAULT_COLUMNS)
CORE_COLUMN_ID_SET = set(CORE_COLUMN_IDS)
CORE_COLUMN_BY_ID = {c["id"]: c for c in DEFAULT_COLUMNS}


def configure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            pass


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, max_len: int = 32, fallback: str = "card") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or fallback


class WorkflowError(ValueError):
    """Domain failure with an HTTP-like status and a stable machine code."""

    def __init__(self, message: str, status: int = 422, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code or {403: "scope", 404: "not_found"}.get(status, "invalid")


class RevisionConflict(WorkflowError):
    def __init__(self, rev: int, card: dict | None = None, reviewed: int | None = None):
        if card is None:
            message = f"board changed; current revision is {rev}; refresh context before retrying"
        else:
            last = (card.get("history") or [None])[-1]
            message = f"card #{card['num']} changed at rev {card['changedRev']} after reviewed rev {reviewed}"
            if isinstance(last, dict) and last.get("ev"):
                actor = f" by {last['by']}" if last.get("by") else ""
                message += f" (last: {last['ev']}{actor})"
            message += f"; re-read with context {card['num']}"
        super().__init__(message, 409, "stale")
        self.rev = rev
        self.card = None if card is None else {
            "num": card["num"], "id": card["id"], "changedRev": card["changedRev"],
            "last": last}


def check_revision(doc: dict, expected_rev: int | None, ref=None) -> None:
    """Without ref, the board revision must match; with ref, only that card must be unchanged."""
    if expected_rev is None:
        return
    if type(expected_rev) is not int or expected_rev < 0:
        raise WorkflowError("expected revision must be a nonnegative integer")
    if ref is None:
        if doc["rev"] != expected_rev:
            raise RevisionConflict(doc["rev"])
        return
    if expected_rev > doc["rev"]:
        raise WorkflowError(f"expected revision {expected_rev} is newer than board revision {doc['rev']}")
    card = resolve_ref(doc, ref)
    if card["changedRev"] > expected_rev:
        raise RevisionConflict(doc["rev"], card, expected_rev)


def require_write_scope(path) -> Path:
    """Check lexical links before canonicalizing; never redirect a caller's destination."""
    raw = Path(path).absolute()
    for candidate in (raw, *raw.parents):
        if _has_reparse_point(candidate):
            raise WorkflowError(f"write path crosses a link or reparse point: {raw}", 403)
    # Resolving the final component opens the file on Windows and races ReplaceFileW under concurrent writers.
    return raw.parent.resolve(strict=False) / raw.name


def runtime_info() -> dict:
    return {"version": __version__, "apiVersion": API_VERSION,
            "schemaVersion": SCHEMA_VERSION, "supportedSchemaVersions": list(SUPPORTED_SCHEMA_VERSIONS),
            "capabilities": list(CAPABILITIES)}

def validate_actor(value) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 80
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise WorkflowError("actor must be a nonempty label of at most 80 characters")
    return value.strip()


DEFAULT_PORT = 7891


def config() -> dict:
    """Optional settings in home()/config.json: {"port", "actor"}. Read per call, never written."""
    path = home() / "config.json"
    try:
        with open_shared(path, "rb") as stream:
            raw = json.loads(stream.read())
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise WorkflowError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkflowError(f"{path} must be a JSON object")
    unknown = sorted(set(raw) - {"port", "actor"})
    if unknown:
        raise WorkflowError(f"{path}: unknown setting {unknown[0]!r}; supported: port, actor")
    port = raw.get("port", DEFAULT_PORT)
    if type(port) is not int or not 1 <= port <= 65535:
        raise WorkflowError(f"{path}: port must be an integer 1..65535")
    if "actor" in raw:
        try:
            validate_actor(raw["actor"])
        except WorkflowError:
            raise WorkflowError(f"{path}: actor must be a nonempty label of at most 80 characters") from None
    return raw


def configured_port() -> int:
    """$WORKBOARD_PORT, else config.json port, else 7891; `serve --port` overrides all three."""
    port = config().get("port", DEFAULT_PORT)
    raw = os.environ.get("WORKBOARD_PORT")
    if not raw:
        return port
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise WorkflowError(f"WORKBOARD_PORT must be an integer 1..65535, not {raw!r}")
    return port


ACTOR_REQUIRED = ('this command writes the board: pass --actor LABEL (or "actor" in the call request, '
                  'or set WORKBOARD_ACTOR) so the board records who wrote this')


def actor(required: bool = False) -> str:
    """CLI writer label: --actor (the CLI sets $WORKBOARD_ACTOR) > $WORKBOARD_ACTOR > config actor.

    With none set, reads act as `agent` and writes (required=True) are refused; the browser records `user`."""
    configured = config().get("actor")
    value = os.environ.get("WORKBOARD_ACTOR", configured)
    if value is None:
        if required:
            raise WorkflowError(ACTOR_REQUIRED)
        return "agent"
    return validate_actor(value)


def _validate_board_name(value) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 80
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise WorkflowError("board name must be nonempty text of at most 80 characters, "
                            "without control characters or surrounding spaces")
    return value


# ===== board lookup =====

def _project_key(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _board_path(entry: dict) -> Path:
    return boards_dir() / entry["dir"] / "board.json"


def board_file(name: str) -> Path:
    entry = registry_load()["boards"].get(name)
    if entry is None:
        raise WorkflowError(f"no board named {name!r}", 404, "not_found")
    return _board_path(entry)


def _checkout(start: Path) -> tuple[Path, Path]:
    """(folder holding the nearest .git, project root); they differ only inside a linked worktree."""
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate, candidate
        if marker.is_file():
            try:
                text = marker.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir:"):
                    return candidate, candidate
                gitdir = candidate / text[len("gitdir:"):].strip()
                common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
            except (OSError, ValueError):  # no commondir: a submodule, not a linked worktree
                return candidate, candidate
            return candidate, common.parent if os.path.normcase(common.name) == ".git" else candidate
    return start, start


def project_root(directory) -> Path:
    """The project a folder belongs to: its git checkout (a linked worktree's main checkout), else itself.

    Pure file reads; never runs git."""
    return _checkout(Path(directory).resolve())[1]


def _board_names(boards: dict) -> str:
    return ", ".join(sorted(boards, key=lambda name: (name.casefold(), name))) or "none yet"


def _named_board(boards: dict, explicit: str) -> str | None:
    """The registered name `explicit` means: exact, then ignoring case, then its folder slug."""
    if explicit in boards:
        return explicit
    folded = explicit.casefold()
    matches = [name for name in boards if name.casefold() == folded]
    if not matches:
        slugs = {folded, slugify(explicit, 48, fallback="")}
        matches = [name for name, entry in boards.items() if entry["dir"] in slugs]
    if len(matches) > 1:
        raise WorkflowError(f"{explicit!r} matches several boards: {_board_names(matches)}; "
                            "pass the exact name")
    return matches[0] if matches else None


def resolve_board(explicit: str | None = None) -> tuple[str, Path]:
    """(name, board.json) for --board / $WORKBOARD_DEFAULT_BOARD (a name or a folder), else the cwd.
    The usage record gets the board's name, or (boardArg) the board argument that matched none."""
    explicit = explicit or os.environ.get("WORKBOARD_DEFAULT_BOARD")
    try:
        name, path = _resolve_board(explicit)
    except WorkflowError:
        usage.note(boardArg=explicit)
        raise
    usage.note(board=name)
    return name, path


def _resolve_board(explicit: str | None) -> tuple[str, Path]:
    boards = registry_load()["boards"]
    hint = (f"Registered boards: {_board_names(boards)}. Pass one with --board NAME "
            '(or "board": NAME in a call request)')
    if explicit:
        name = _named_board(boards, explicit)
        if name is not None:
            return name, _board_path(boards[name])
        if not Path(explicit).is_dir():
            raise WorkflowError(f"no board named or linked to {explicit!r}. {hint}", 404, "not_found")
    start = Path(explicit or Path.cwd()).resolve()
    linked = {os.path.normcase(entry["project"]): name for name, entry in boards.items()}
    worktree, root = _checkout(start)
    # Nearest linked folder wins. Inside a linked worktree (even one kept inside the main checkout),
    # the worktree's own folders come first, then the same spots in the main checkout, then the rest.
    order = [start, *start.parents]
    if root != worktree:
        mapped = root / start.relative_to(worktree)
        order = [start, *(p for p in start.parents if p.is_relative_to(worktree)),
                 mapped, *(p for p in mapped.parents if p.is_relative_to(root)),
                 *root.parents, *worktree.parents]
    for candidate in order:
        name = linked.get(os.path.normcase(str(candidate)))
        if name is not None:
            return name, _board_path(boards[name])
    raise WorkflowError(f"no board for {start}: the folder is outside every linked project. {hint}, "
                        "or run `workboard init` in the project.", 404, "not_found")


def find_board(explicit: str | None = None) -> Path:
    return resolve_board(explicit)[1]


# ===== Windows share-friendly reads + contention-tolerant atomic replace =====
# Proven under real concurrent agent sessions (WinError 5 root cause: any plain
# reader blocks rename). Readers open FILE_SHARE_DELETE handles; committed writes
# swap via ReplaceFileW; the sharing-violation family retries with backoff+jitter.

_IS_WINDOWS = os.name == "nt"
REPLACE_RETRY_SECONDS = 10.0
_SHARING_WINERRORS = (5, 32, 33)
_ERROR_FILE_NOT_FOUND = 2
_PARTIAL_REPLACE_WINERRORS = (1176, 1177)


class ReplaceFilePartialFailure(OSError):
    def __init__(self, winerror, replacement_path, target_path, recovery_error=None):
        self.winerror = winerror
        self.replacement_path = os.fspath(replacement_path)
        self.target_path = os.fspath(target_path)


try:
    import fcntl  # type: ignore
    _HAVE_FCNTL = True
except ImportError:
    _HAVE_FCNTL = False

if _IS_WINDOWS:
    import _winapi
    import msvcrt

    def _win_share_delete_opener(path, flags):
        # GENERIC_READ; share read, write and delete; OPEN_EXISTING; FILE_ATTRIBUTE_NORMAL.
        handle = _winapi.CreateFile(os.fspath(path), 0x80000000, 0x7, 0, 3, 0x80, 0)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise

    @functools.cache
    def _replace_file_w():
        import ctypes
        from ctypes import wintypes
        function = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        function.restype = wintypes.BOOL
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.LPVOID]
        return ctypes, function

    def _win_replace_file(src, dst):
        ctypes, replace_file = _replace_file_w()
        if not replace_file(dst, src, None, 0, None, None):
            code = ctypes.get_last_error()
            if code in _PARTIAL_REPLACE_WINERRORS:
                raise ReplaceFilePartialFailure(code, src, dst)
            raise ctypes.WinError(code)


def _is_sharing_violation(err) -> bool:
    return _IS_WINDOWS and getattr(err, "winerror", None) in _SHARING_WINERRORS


def open_shared(path, mode="r", encoding="utf-8", **kwargs):
    read_only = ("r" in mode) and not any(m in mode for m in ("w", "a", "x", "+"))
    if _IS_WINDOWS and read_only:
        try:
            if "b" in mode:
                return open(path, mode, opener=_win_share_delete_opener, **kwargs)
            return open(path, mode, encoding=encoding,
                        opener=_win_share_delete_opener, **kwargs)
        except OSError as error:
            if _is_sharing_violation(error):
                raise
    if "b" in mode:
        return open(path, mode, **kwargs)
    return open(path, mode, encoding=encoding, **kwargs)


def _read_shared(path, mode, encoding=None, missing=REPLACE_RETRY_SECONDS):
    """Read a file another process may be replacing. On Windows a sharing violation is retried for
    REPLACE_RETRY_SECONDS and a missing file (ReplaceFileW's window) for `missing` seconds."""
    start = time.monotonic()
    delay = 0.002
    while True:
        try:
            kwargs = {} if "b" in mode else {"encoding": encoding or "utf-8"}
            with open_shared(path, mode, **kwargs) as file:
                return file.read()
        except OSError as error:
            transient = _is_sharing_violation(error) or (
                _IS_WINDOWS and isinstance(error, (FileNotFoundError, PermissionError))
            )
            limit = missing if isinstance(error, FileNotFoundError) else REPLACE_RETRY_SECONDS
            if not transient or time.monotonic() - start >= limit:
                raise
            time.sleep(_jitter(delay))
            delay = min(0.05, delay * 2)


def read_text_shared(path, encoding="utf-8") -> str:
    return _read_shared(path, "r", encoding)


def _fsync_parent_directory(path) -> None:
    if _IS_WINDOWS:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(os.fspath(Path(path).parent), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _recover_partial_replace(src, target, failure) -> bool:
    if not os.path.exists(target) and os.path.exists(src):
        try:
            os.replace(src, target)
        except OSError as recovery_error:
            raise ReplaceFilePartialFailure(
                failure.winerror, src, target) from recovery_error
        _fsync_parent_directory(target)
        return True
    return False


def atomic_replace(src, target, timeout=REPLACE_RETRY_SECONDS) -> None:
    require_write_scope(src)
    require_write_scope(target)
    src = os.fspath(src)
    target = os.fspath(target)
    deadline = time.monotonic() + float(timeout)
    delay = 0.02
    while True:
        try:
            if _IS_WINDOWS and os.path.exists(target):
                try:
                    _win_replace_file(src, target)
                except ReplaceFilePartialFailure as e:
                    if not _recover_partial_replace(src, target, e):
                        raise
                except OSError as e:
                    if getattr(e, "winerror", None) == _ERROR_FILE_NOT_FOUND:
                        os.replace(src, target)
                    else:
                        raise
            else:
                os.replace(src, target)
            _fsync_parent_directory(target)
            return
        except OSError as e:
            if not _is_sharing_violation(e) or time.monotonic() >= deadline:
                raise
        time.sleep(_jitter(min(0.4, delay)))
        delay = min(0.4, delay * 2)


def _jitter(delay: float) -> float:
    import random
    return random.uniform(0.0, delay)


# ===== locking =====

class LockTimeout(Exception):
    pass


_lock_local = threading.local()


@contextlib.contextmanager
def board_lock(board_path, timeout: float = 5.0):
    """Exclusive cross-process lock on <dir>/.board.lock: the registry's, and a legacy board's while
    it migrates (0.3.x writers take the same lock).

    Reentrant within the owning thread. Policy: wait up to `timeout`, then RAISE.
    """
    lock_path = require_write_scope(Path(board_path).absolute().parent / LOCK_NAME)
    key = os.path.normcase(str(lock_path))
    depths = getattr(_lock_local, "depths", None)
    if depths is None:
        depths = _lock_local.depths = {}
    if depths.get(key):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return
    f = None
    acquired = False
    try:
        f = open(lock_path, "a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        if not acquired:
            raise LockTimeout(
                f"could not acquire the board lock ({lock_path}) within {timeout}s; "
                "nothing was written — re-run the command."
            )
        depths[key] = 1
        try:
            yield
        finally:
            del depths[key]
    finally:
        if f is not None:
            if acquired:
                try:
                    if _HAVE_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    else:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            f.close()


# ===== schema =====

def validate_schema(raw: dict) -> int:
    if not isinstance(raw, dict):
        raise WorkflowError("board document must be an object")
    version = raw.get("schemaVersion", 1)
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        hint = ("; WorkBoard 0.3.11 is the last version that reads it: write to the board once with 0.3.11, "
                "then retry" if type(version) is int and version < 3 else "")
        raise WorkflowError(f"unsupported board schemaVersion {version!r}; supported: "
                            + ", ".join(map(str, SUPPORTED_SCHEMA_VERSIONS)) + hint, 409)
    return version


def _validate_aliases(raw: dict) -> None:
    if not isinstance(raw, dict):
        raise WorkflowError("card must be an object")
    for canonical, alias in (("cycles", "lifecycleCycles"),):
        if canonical in raw and alias in raw and (
                type(raw[canonical]) is not type(raw[alias])
                or json.dumps(raw[canonical], sort_keys=True) != json.dumps(raw[alias], sort_keys=True)):
            raise WorkflowError(f"conflicting legacy aliases: {canonical}/{alias}", 409)


_SCOPE_GLOB = re.compile(r"[*?\[\]{}]")


def _norm_scope(value) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkflowError("delegation scope must be an array of relative paths")
    normalized = []
    for item in value:
        item = item.strip().replace("\\", "/")
        if (not item or item.startswith("/") or any(char in item for char in ':<>"|')
                or any(ord(char) < 32 for char in item) or _SCOPE_GLOB.search(item)):
            raise WorkflowError("delegation scopes must be unambiguous portable relative paths from the "
                                "project root, e.g. packages/web/src/app/App.tsx")
        item = item.rstrip("/")
        parts = item.split("/")
        if item != "." and any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in parts):
            raise WorkflowError("delegation scopes must not contain empty, dot, or parent segments")
        if item not in normalized:
            normalized.append(item)
    return normalized


def flatten_subtasks(items) -> list[dict]:
    """Legacy nested checklists as one list in pre-order (parent before its children), without
    `children` or `collapsed`; every other item field is kept."""
    flat = []
    for item in items:
        if not isinstance(item, dict) or (item.get("children") is not None and not isinstance(item["children"], list)):
            raise WorkflowError("subtasks must be objects with an array of children")
        flat.append(item if "children" not in item and "collapsed" not in item else
                    {key: value for key, value in item.items() if key not in ("children", "collapsed")})
        flat.extend(flatten_subtasks(item.get("children") or []))
    return flat


def _norm_subtask(st: dict) -> dict:
    normalized = {
        **st,
        "id": str(st.get("id", "")),
        "text": str(st.get("text", "")),
        "done": bool(st.get("done", False)),
        "createdAt": st.get("createdAt"),
        "doneAt": st.get("doneAt"),
    }
    if "changedRev" in st and not (type(st["changedRev"]) is int and st["changedRev"] >= 0):
        del normalized["changedRev"]  # save() owns the stamp: drop an invalid one rather than refuse the board.
    if "delegation" in st:
        delegation = st["delegation"]
        if not isinstance(delegation, dict):
            raise WorkflowError("subtask delegation must be an object")
        state = delegation.get("state")
        owner, claimed_at, result = (delegation.get("owner"), delegation.get("claimedAt"),
                                     delegation.get("result", ""))
        if state == "blocked":  # Blocking delegated work was removed: blocked work is claimed again.
            state = "claimed"
        if state not in ("available", "claimed", "completed"):
            raise WorkflowError("subtask delegation state must be available, claimed, or completed")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise WorkflowError("subtask delegation owner must be text or null")
        if claimed_at is not None and not isinstance(claimed_at, str):
            raise WorkflowError("subtask delegation claimedAt must be text or null")
        if not isinstance(result, str):
            raise WorkflowError("subtask delegation result must be text")
        if state == "available" and (owner is not None or claimed_at is not None):
            raise WorkflowError("available delegated work cannot have an owner")
        if state in ("claimed", "completed") and (owner is None or claimed_at is None):
            raise WorkflowError("claimed delegated work needs an owner and claim time")
        if (state == "completed") != bool(st.get("done", False)):
            raise WorkflowError("completed delegation state must match subtask completion")
        if state == "completed" and not result.strip():
            raise WorkflowError("completed delegated work needs a result")
        review = delegation.get("review", {})
        if not isinstance(review, dict) \
                or review.get("state", "pending") not in ("pending", "accepted", "changes_requested"):
            raise WorkflowError("subtask review must have a valid state")
        for key in ("by", "at", "reason"):
            if review.get(key) is not None and not isinstance(review[key], str):
                raise WorkflowError(f"subtask review {key} must be text or null")
        # Removed fields go: the blocker, the prerequisites (dependsOn) and review.required (every result
        # needs acceptance).
        normalized["delegation"] = {**{key: value for key, value in delegation.items()
                                       if key not in ("blocker", "dependsOn")},
                                    "state": state, "owner": owner,
                                    "claimedAt": claimed_at, "result": result,
                                    "scope": _norm_scope(delegation.get("scope", [])),
                                    "review": {**{key: value for key, value in review.items()
                                                  if key != "required"},
                                               "state": review.get("state", "pending"),
                                               "by": review.get("by"), "at": review.get("at"),
                                               "reason": review.get("reason", "")}}
    return normalized


def validate_note(summary, body) -> tuple[str, str]:
    """Return one log entry's canonical (summary, body) or raise 422 invalid."""
    if not isinstance(summary, str) or not summary.strip():
        raise WorkflowError("note summary must be nonempty text")
    summary = summary.strip()
    if "\n" in summary or "\r" in summary or len(summary) > NOTE_SUMMARY_MAX:
        raise WorkflowError(f"note summary must be one line of at most {NOTE_SUMMARY_MAX} characters")
    body = "" if body is None else body
    if not isinstance(body, str) or len(body.rstrip()) > NOTE_BODY_MAX:
        raise WorkflowError(f"note body must be markdown text of at most {NOTE_BODY_MAX} characters")
    return summary, body.rstrip()


def normalize_log(entries) -> list[dict]:
    """Validate a card's note timeline; malformed entries are refused, never dropped."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise WorkflowError("card log must be an array")
    log, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WorkflowError("card log entries must be objects; refusing data loss")
        entry_id, by = entry.get("id"), entry.get("by")
        if not isinstance(entry_id, str) or not re.fullmatch(r"[0-9a-f]{32}", entry_id) or entry_id in seen:
            raise WorkflowError(f"card log entry IDs must be unique 32-digit lowercase hex: {entry_id!r}")
        if not isinstance(entry.get("at"), str) or (by is not None and not isinstance(by, str)):
            raise WorkflowError(f"card log entry {entry_id} needs text 'at' and text or null 'by'")
        seen.add(entry_id)
        summary, body = validate_note(entry.get("summary"), entry.get("body"))
        log.append({**entry, "by": by, "summary": summary, "body": body})
    return log


def fit_note(summary, body) -> tuple[str, str]:
    """Caller text → a valid (summary, body). A multi-line or over-long summary keeps its first line,
    clipped at a word, and moves whole to the top of the body: nothing is refused or lost."""
    if isinstance(summary, str) and summary.strip():
        text = summary.strip()
        first = re.split(r"[\r\n]", text, maxsplit=1)[0].rstrip()
        if first != text or len(text) > NOTE_SUMMARY_MAX:
            summary = _clip_line(first)
            if body is None or isinstance(body, str):  # Other bodies stay as given: validate_note refuses them.
                rest = (body or "").rstrip()
                body = f"{text}\n\n{rest}" if rest else text
    return validate_note(summary, body)


def _clip_line(line: str) -> str:
    """One line within NOTE_SUMMARY_MAX: cut at the last word break that fits, else hard, marked with …"""
    if len(line) <= NOTE_SUMMARY_MAX:
        return line
    cut = re.match(r"(.*\S)\s", line[:NOTE_SUMMARY_MAX - 1])
    return (cut[1] if cut else line[:NOTE_SUMMARY_MAX - 1]) + "…"


def _comment_note(comment: dict) -> dict:
    """A legacy comment as a log entry: the same id, time and author, and its edit stamp as a note
    edit's (editedAt, plus editedBy); its text split by fit_note."""
    summary, body = fit_note(comment.get("text"), None)
    entry = {"id": comment.get("id"), "at": comment.get("at"), "by": comment.get("by"),
             "summary": summary, "body": body}
    for key, legacy in (("editedAt", "updatedAt"), ("editedBy", "updatedBy")):
        if comment.get(legacy) is not None:
            entry[key] = comment[legacy]
    return entry


def normalize_card(raw: dict) -> dict:
    """The canonical card. Removed fields go (links, inbox acknowledgements); comments become log notes."""
    _validate_aliases(raw)
    for key in ("tags", "subtasks", "history", "cycles", "lifecycleCycles",
                "dependsOn", "comments", "attachments", "verification", "reviews"):
        if raw.get(key) is not None and not isinstance(raw[key], list):
            raise WorkflowError(f"card {key} must be an array")
    for key in ("comments", "attachments"):
        if any(not isinstance(item, dict) for item in (raw.get(key) or [])):
            raise WorkflowError(f"card {key} entries must be objects; refusing data loss")
    log = normalize_log(raw.get("log"))
    if raw.get("comments"):  # Merged in time order; a stable sort keeps equal times as they were.
        log = normalize_log(sorted(log + [_comment_note(c) for c in raw["comments"]],
                                   key=lambda entry: str(entry["at"])))
    return {
        **{key: value for key, value in raw.items()
           if key not in ("links", "linkedCards", "lifecycleCycles", "comments", "inboxAcknowledged")},
        "num": raw.get("num"),
        "id": str(raw.get("id") or ""),
        "code": str(raw.get("code") or ""),
        "title": str(raw.get("title", "")),
        "column": str(raw.get("column") or "task"),
        "priority": raw.get("priority") if raw.get("priority") in ("critical", "mid", "low") else None,
        "tags": [str(t) for t in (raw.get("tags") or [])],
        "origin": str(raw.get("origin") or ""),
        "notes": str(raw.get("notes") or ""),
        "writeup": str(raw.get("writeup") or ""),
        "subtasks": [_norm_subtask(s) for s in flatten_subtasks(raw.get("subtasks") or [])],
        "history": list(raw.get("history") or []),
        "cycles": list(raw.get("cycles", raw.get("lifecycleCycles")) or []),
        "createdAt": str(raw.get("createdAt") or now_iso()),
        "updatedAt": str(raw.get("updatedAt") or now_iso()),
        "doneAt": raw.get("doneAt"),
        "reopenReason": raw.get("reopenReason"),
        "blockedReason": raw.get("blockedReason"),
        "unblockWhen": raw.get("unblockWhen"),
        "blockedAt": raw.get("blockedAt"),
        "dependsOn": list(dict.fromkeys(str(i) for i in (raw.get("dependsOn") or []))),
        "activeOwner": raw.get("activeOwner") if raw.get("column") == "inprogress" else None,
        "claimedAt": raw.get("claimedAt") if raw.get("column") == "inprogress" else None,
        "outcome": ("canceled" if raw.get("outcome") == "canceled" else "completed")
                   if raw.get("column") == "done" else None,
        "cancelReason": raw.get("cancelReason"),
        "reworkReason": raw.get("reworkReason"),
        "attachments": [dict(a) for a in (raw.get("attachments") or []) if isinstance(a, dict)],
        "verification": list(raw.get("verification") or []),
        "reviews": list(raw.get("reviews") or []),
        "changedRev": raw["changedRev"] if type(raw.get("changedRev")) is int and raw["changedRev"] >= 0 else 0,
        "log": log,
    }


def flatten_column_stacks(columns: list[dict]) -> list[dict]:
    """Collapse stackUnder chains into one-level groups, preserving list order."""
    by_id = {c["id"]: c for c in columns}
    for col in columns:
        parent_id = col.get("stackUnder")
        seen = {col["id"]}
        root = None
        while parent_id:
            if parent_id in seen:
                root = None
                break
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                root = None
                break
            root = parent
            parent_id = parent.get("stackUnder")
        col["stackUnder"] = root["id"] if root is not None else None
    return columns


def normalize_doc(raw: dict) -> dict:
    version = validate_schema(raw)
    for key in ("columns", "cards"):
        if raw.get(key) is not None and not isinstance(raw[key], list):
            raise WorkflowError(f"board {key} must be an array")
    columns = raw.get("columns")
    if not columns:
        columns = [dict(c) for c in DEFAULT_COLUMNS]
    norm_columns = []
    for c in columns:
        if not isinstance(c, dict) or "id" not in c:
            raise WorkflowError("every board column must be an object with an id")
        col = {**c, "id": str(c["id"]), "name": str(c.get("name") or c["id"]),
               "kind": str(c.get("kind") or "custom")}
        if c.get("wipLimit") is not None:   # browser WIP constraint (board.html _moveConstraint)
            col["wipLimit"] = c["wipLimit"]
        col["stackUnder"] = (
            str(c["stackUnder"]) if c.get("stackUnder") is not None else None
        )
        norm_columns.append(col)
    columns = norm_columns
    seen, uniq = set(), []
    for c in columns:
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    flatten_column_stacks(uniq)
    cards = [normalize_card(r) for r in (raw.get("cards") or [])]
    nums = [c["num"] for c in cards if isinstance(c["num"], int)]
    next_num = raw.get("nextNum")
    if not isinstance(next_num, int) or next_num <= (max(nums) if nums else 0):
        next_num = (max(nums) + 1) if nums else 1
    doc = {
        **raw,
        "schemaVersion": version,
        "name": str(raw.get("name") or raw.get("title") or "WorkBoard"),
        "rev": int(raw.get("rev") or 0),
        "nextNum": next_num,
        "savedAt": raw.get("savedAt"),
        "savedBy": raw.get("savedBy"),
        "columns": uniq,
        "cards": cards,
    }
    if raw.get("title"):   # browser board-title rename (board.html commitStructure update-document)
        doc["title"] = str(raw["title"])
    return doc


# ===== board store: SQLite per board =====
# boards/<dir>/board.db holds one row per card (`body` is the card exactly as normalize_card returns it),
# every other top-level key in `meta`, and each changed card's previous body in `card_versions`.
# board.json becomes a schema-5 stub that 0.3.x and older refuse, so an old install never sees the
# board as empty. Each write is one SQLite transaction (WAL, synchronous=FULL, 30 s busy wait).

DB_NAME = "board.db"
STORE_SCHEMA = 5
VERSION_KEEP = 2000  # card_versions keeps the newest this many revisions: the restore horizon,
VERSION_BYTES = 64 << 20  # trimmed to about this many bytes of bodies from the oldest end.
_STUB_MAX = 4096
_DDL = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE cards(id TEXT PRIMARY KEY, num INTEGER UNIQUE, col TEXT NOT NULL, done_at TEXT,
                   changed_rev INTEGER NOT NULL, pos INTEGER NOT NULL, deps TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX cards_col ON cards(col);
CREATE INDEX cards_pos ON cards(pos, id);
CREATE INDEX cards_changed ON cards(changed_rev);
CREATE INDEX cards_deps ON cards(deps, id, col) WHERE deps != '[]';
CREATE TABLE card_versions(rev INTEGER NOT NULL, card_id TEXT NOT NULL, body TEXT,
                           PRIMARY KEY (rev, card_id));
PRAGMA user_version = 5;
"""
_store = threading.local()  # .open: {db key: _Loaded} for this thread's open write transactions


class _Loaded:
    """What one read of the store returned; save() writes only what differs from it."""
    __slots__ = ("con", "scope", "rev", "meta", "cards", "count", "card")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _db(board_path) -> Path:
    return Path(board_path).with_name(DB_NAME)


def _txn_key(board_path) -> str:
    # Lexical: write paths never cross links (require_write_scope), so this names the store.
    return os.path.normcase(os.path.abspath(_db(board_path)))


def _size(path) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _is_stub(data: bytes) -> bool:
    if len(data) > _STUB_MAX:
        return False
    try:
        raw = json.loads(data)
    except ValueError:
        return False
    return isinstance(raw, dict) and raw.get("schemaVersion") == STORE_SCHEMA and raw.get("storage") == DB_NAME


def _split_brain(board_path) -> WorkflowError:
    return WorkflowError(f"{board_path} holds a full board document next to {DB_NAME}, and WorkBoard never "
                         f"merges the two: move board.json aside to keep {DB_NAME}, or move {DB_NAME} aside "
                         "to migrate board.json again", 409, "state")


@contextlib.contextmanager
def _sql_errors(db: Path):
    """SQLite failures as the errors callers handle: a 30 s busy wait is a LockTimeout, the rest OSError."""
    import sqlite3
    try:
        yield
    except sqlite3.IntegrityError as exc:
        raise WorkflowError(f"the board store refused the write ({exc}); nothing was written", 409, "state") from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise LockTimeout(f"{db} stayed locked by other writers for 30s; nothing was written — "
                              "re-run the command.") from exc
        raise OSError(f"board store {db}: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise OSError(f"board store {db}: {exc}") from exc


def _open(board_path):
    """A connection to the board's store; a legacy board.json migrates on first touch."""
    import sqlite3
    db = _db(board_path)
    # No board.json is no board, whatever else is left. _migrate decides the rest under the lock,
    # split-brain included: a board.json read just before another process's migration commit is stale.
    if not (_is_stub(_read_shared(board_path, "rb", missing=0.1)) and db.exists()):
        _migrate(Path(board_path))
    # Path.as_uri() imports urllib (~35 ms per process); a SQLite URI only needs %, ? and # escaped.
    path = db.absolute().as_posix().replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30, isolation_level=None)
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA mmap_size=268435456")  # Reads map the file instead of copying pages: 256 MB.
    return con


def _migrate(board_path: Path) -> None:
    """Move a legacy board.json into board.db, or finish a migration a crash interrupted. Runs under the
    legacy lock, so 0.3.x writers wait; the stub replacing board.json is the commit point."""
    db, tmp = _db(board_path), board_path.with_name(DB_NAME + ".tmp")
    with board_lock(board_path, timeout=30):
        data = _read_shared(board_path, "rb")
        if _is_stub(data):
            if db.exists():
                return  # Another process migrated it first.
            if not tmp.exists():
                raise WorkflowError(f"{board_path} points at {DB_NAME}, which is missing; restore the board "
                                    f"folder from {deleted_dir()} or a {BACKUP_DIR} copy", 409, "state")
            atomic_replace(tmp, db)  # Crashed after the commit point: finish it.
            return
        if db.exists():
            raise _split_brain(board_path)
        try:
            raw = json.loads(data)
        except ValueError as e:
            raise WorkflowError(f"{board_path} is not valid JSON ({e}); restore it from {BACKUP_DIR}") from e
        doc = normalize_doc(raw)
        ids = {card["id"] for card in doc["cards"]}
        nums = {card["num"] for card in doc["cards"] if type(card["num"]) is int}
        columns = {column["id"] for column in doc["columns"]}
        for card in _archived_cards(board_path.parent):  # Swept cards become rows again.
            if card["column"] not in columns:  # columns-core purged it with its column: it stays gone.
                continue
            num = card["num"] if type(card["num"]) is int else None
            if card["id"] not in ids and num not in nums:
                doc["cards"].append(card)
                ids.add(card["id"])
                if num is not None:
                    nums.add(num)
        doc["nextNum"] = max(doc["nextNum"], max(nums, default=0) + 1)
        _install_store(board_path, doc, original=data)


def _archived_cards(folder: Path) -> list[dict]:
    """Cards in archive/*.json (sweeps and purges); a file that does not parse stays for doctor."""
    cards = []
    for path in sorted((folder / ARCHIVE_DIR).glob("*.json")):
        try:
            raw = json.loads(_read_shared(path, "rb"))
            cards += [normalize_card(card) for card in (raw.get("cards") if isinstance(raw, dict) else raw)]
        except (OSError, ValueError, TypeError):
            continue
    return cards


def _row(card: dict, pos: int, body: str) -> tuple:
    """A card's cards-table row, in column order."""
    return (card["id"], card["num"] if type(card["num"]) is int else None, card["column"],
            card["doneAt"] if isinstance(card["doneAt"], str) else None, card["changedRev"], pos,
            _dump(card["dependsOn"]), body)


def _install_store(board_path: Path, doc: dict, original: bytes | None = None) -> None:
    """Write a normalized doc to board.db.tmp and keep `original` in .backups; then commit: the stub
    replaces board.json and the tmp becomes board.db. A crash before the stub leaves board.json in charge."""
    import sqlite3
    db, tmp = _db(board_path), board_path.with_name(DB_NAME + ".tmp")
    for leftover in (tmp, *(tmp.with_name(tmp.name + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        require_write_scope(leftover).unlink(missing_ok=True)
    doc["schemaVersion"] = SCHEMA_VERSION
    for key in ("changeEpoch", "changeJournal"):  # Dropped incremental-context journal (dev builds).
        doc.pop(key, None)
    con = sqlite3.connect(tmp, isolation_level=None)
    try:
        with _sql_errors(tmp):
            con.executescript(_DDL)
            con.execute("BEGIN")
            con.executemany("INSERT INTO meta VALUES (?, ?)",
                            [(key, _dump(value)) for key, value in doc.items() if key != "cards"])
            con.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            [_row(card, pos, _dump(card)) for pos, card in enumerate(doc["cards"])])
            con.execute("COMMIT")
            con.execute("PRAGMA journal_mode=WAL")
    finally:
        con.close()
    if original is not None:
        backup = board_path.parent / BACKUP_DIR / f"board-{doc['rev']}.json"
        if not backup.exists():
            _atomic_write(backup, original)
    stub = {"schemaVersion": STORE_SCHEMA, "storage": DB_NAME, "name": doc["name"]}
    _atomic_write(board_path, (_dump(stub) + "\n").encode("utf-8"))
    atomic_replace(tmp, db)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _ref_ids(con, ref) -> list:
    """Every card resolve_ref(doc, ref) could pick or name, so a scoped doc resolves the same way."""
    ref = str(ref).strip().lstrip("#")
    if ref.isdigit():
        num = int(ref)
        return [cid for (cid,) in con.execute("SELECT id FROM cards WHERE num = ?", (num,))] if num < 1 << 63 else []
    if con.execute("SELECT 1 FROM cards WHERE id = ?", (ref,)).fetchone():
        return [ref]
    coded = con.execute("SELECT id FROM cards WHERE body LIKE ? ESCAPE '\\'",
                        (_like(_dump({"code": ref})[1:-1]),)).fetchall()
    prefixed = con.execute("SELECT id FROM cards WHERE id >= ? AND id < ? ORDER BY pos LIMIT 7",
                           (ref, ref + "\U0010ffff")).fetchall()
    return [cid for (cid,) in coded + prefixed]


def _read(con, refs=(), scope="all", ids=()) -> tuple[dict, _Loaded]:
    """The doc for one load scope (see load) plus what save() compares against; `ids` are exact IDs."""
    if scope not in ("all", "open", "card"):
        raise ValueError(f"unknown board load scope {scope!r}")
    base = _Loaded()
    base.con, base.scope = None, scope
    base.meta = dict(con.execute("SELECT k, v FROM meta ORDER BY rowid").fetchall())
    first = None
    if scope == "all":
        rows = con.execute("SELECT id, pos, body FROM cards ORDER BY pos").fetchall()
        base.count = len(rows)
    else:
        wanted = set(ids)
        for ref in refs:
            matched = _ref_ids(con, ref)
            first = first or next(iter(matched), None)
            wanted.update(matched)
        graph = {cid: (col, json.loads(deps)) for cid, col, deps in
                 con.execute("SELECT id, col, deps FROM cards WHERE deps != '[]'")}
        keep = set(wanted)
        for cid in wanted:
            keep.update(graph.get(cid, ("", ()))[1])
        keep.update(cid for cid, (_, deps) in graph.items() if not wanted.isdisjoint(deps))
        where, params = "id IN (SELECT value FROM json_each(?))", ()
        if scope == "open":
            keep.update(dep for col, deps in graph.values() if col != "done" for dep in deps)
            params = tuple(col for (col,) in con.execute("SELECT DISTINCT col FROM cards") if col != "done")
            where = f"col IN ({', '.join('?' * len(params))}) OR {where}"
        rows = con.execute(f"SELECT id, pos, body FROM cards WHERE {where} ORDER BY pos",
                           (*params, _dump(sorted(keep)))).fetchall()
        base.count = con.execute("SELECT count(*) FROM cards").fetchone()[0]
    doc = {key: json.loads(value) for key, value in base.meta.items()}
    doc["cards"] = [json.loads(body) for _, _, body in rows]
    base.rev = doc["rev"]
    base.cards = {cid: (body, pos) for cid, pos, body in rows}
    base.card = next((card["num"] for card in doc["cards"] if card["id"] == first), None) if first else None
    return doc, base


def _note_read(db: Path, base: _Loaded) -> None:
    usage.note(rev=base.rev, cards=base.count, bytes=_size(db), card=base.card)


def load(board_path, refs=(), scope: str = "all", ids=(), con=None) -> dict:
    """The normalized board document, from one consistent snapshot; the caller may mutate it.

    scope "all": every card, in board order. "open": cards not in Done, the cards they depend on, and
    `refs` with their dependencies and dependents. "card": only `refs` with their dependencies and
    dependents. Refs resolve as resolve_ref does (number, #number, ID, code, ID prefix); `ids` are exact
    card IDs that join `refs`. With `con` (from snapshot()) it reads in the caller's snapshot."""
    t0 = time.perf_counter()
    db = _db(board_path)
    with snapshot(board_path) if con is None else contextlib.nullcontext(con) as con:
        doc, base = _read(con, refs, scope, ids)
    _note_read(db, base)
    usage.stage("load", t0)
    return doc


@contextlib.contextmanager
def snapshot(board_path):
    """A read connection inside one transaction, so every query sees one consistent revision; readers
    never wait on writers. A legacy board.json migrates first; SQLite failures raise as load's do."""
    with _sql_errors(_db(board_path)):
        con = _open(board_path)
        try:
            con.execute("BEGIN")
            yield con
        finally:
            con.close()


def board_rev(board_path) -> int:
    """The board's current revision in one small read: what `wait` and the server's watcher poll."""
    with _sql_errors(_db(board_path)):
        con = _open(board_path)
        try:
            return json.loads(con.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
        finally:
            con.close()


# Reads inside one snapshot() for query and digest, so neither has to load every card.

def query_candidates(con, column=None, held=False, tags=(), words=()) -> list | None:
    """IDs of the cards a query may match, or None when no filter narrows; never fewer than match, so the
    caller filters them again. column: a column ID; held: only cards with a holder (In Progress and Blocked
    alone have one); tags: values the card's tags hold; words: text lowercased by str.lower(). Bodies are
    JSON and SQLite's lower() folds only ASCII, so only printable ASCII words without a space, `"` or `\\`
    narrow. str.lower() also turns U+212A into k and U+0130 into i + U+0307, so a body holding U+212A stays
    for a word with a k, and one holding U+0130 for a word ending in i."""
    where, params = [], []
    if column is not None:
        where.append("col = ?")
        params.append(column)
    if held:
        where.append("col IN ('inprogress', 'blocked')")
    for tag in tags:
        where.append("instr(body, ?)")
        params.append(_dump(tag))
    for word in words:
        if word and word.isascii() and word.isprintable() and not any(ch in word for ch in ' "\\'):
            folded = [ch for ch, hit in (("\u212a", "k" in word), ("\u0130", word.endswith("i"))) if hit]
            where.append(f"({' OR '.join(['instr(lower(body), ?)'] + ['instr(body, ?)'] * len(folded))})")
            params += [word, *folded]
    if not where:
        return None
    return [cid for (cid,) in con.execute(f"SELECT id FROM cards WHERE {' AND '.join(where)}", params)]


def card_has_key(con, key: str) -> bool:
    """Some card has this top-level key."""
    return con.execute("SELECT 1 FROM cards WHERE instr(body, ?) AND EXISTS (SELECT 1 FROM json_each(body) "
                       "WHERE key = ?) LIMIT 1", (_dump(key) + ":", key)).fetchone() is not None


def column_counts(con) -> dict:
    return dict(con.execute("SELECT col, count(*) FROM cards GROUP BY col").fetchall())


def claimed_done(con) -> list:
    """IDs of the Done cards that may hold claimed delegated work."""
    return [cid for (cid,) in con.execute("SELECT id FROM cards WHERE col = 'done' AND instr(body, '\"claimed\"')")]


def done_lines(con, columns, keep: int) -> tuple[list, list]:
    """Done cards for digest without loading Done: the newest `keep` shipped from `columns` (by doneAt, ties in
    board order) and the last `keep` canceled, in board order. Each reads bodies only until it has `keep`."""
    shipped = []
    for (cid,) in con.execute(f"SELECT id FROM cards WHERE col IN ({', '.join('?' * len(columns))}) "
                              "AND done_at != '' ORDER BY done_at DESC, pos", list(columns)).fetchall():
        card = json.loads(con.execute("SELECT body FROM cards WHERE id = ?", (cid,)).fetchone()[0])
        if card.get("outcome") != "canceled":
            shipped.append(card)
            if len(shipped) == keep:
                break
    canceled = []  # +col keeps the walk on cards_pos, newest first, so it stops early.
    for (body,) in con.execute("SELECT body FROM cards WHERE +col = 'done' "
                               "AND instr(body, '\"outcome\":\"canceled\"') ORDER BY pos DESC"):
        card = json.loads(body)
        if card.get("outcome") == "canceled":
            canceled.insert(0, card)
            if len(canceled) == keep:
                break
    return shipped, canceled


@contextlib.contextmanager
def board_transaction(board_path, expected_rev=None, ref=None, scope: str = "all", refs=()):
    """Yield the doc (see load for scope; `ref` joins `refs`) inside one write transaction: other writers
    wait up to 30 s, readers never wait. save() commits; leaving without save() writes nothing.
    expected_rev with `ref` is card-scoped (that card's changedRev), else it must equal the board rev."""
    key = _txn_key(board_path)
    board_path = require_write_scope(board_path)
    db = _db(board_path)
    txns = _store.__dict__.setdefault("open", {})
    if key in txns:
        raise RuntimeError(f"nested write transaction on {db}")
    with _sql_errors(db):
        t0 = time.perf_counter()
        con = _open(board_path)
        usage.stage("load", t0)  # Opening (and a first-touch migration) counts as load; lockWait is the wait.
        try:
            t0 = time.perf_counter()
            con.execute("BEGIN IMMEDIATE")
            usage.stage("lockWait", t0)
            t0 = time.perf_counter()
            doc, base = _read(con, refs if ref is None else (ref, *refs), scope)
            usage.stage("load", t0)
            _note_read(db, base)
            check_revision(doc, expected_rev, ref)
            base.con = con
            txns[key] = base
            try:
                yield doc
            finally:
                txns.pop(key, None)
        finally:
            con.close()  # Closing rolls back whatever save() did not commit.


def _card_content(card: dict) -> str:
    """A card without the stamps save() owns: its changedRev and its subtasks'."""
    content = {key: value for key, value in card.items() if key != "changedRev"}
    content["subtasks"] = [{key: value for key, value in st.items() if key != "changedRev"}
                           if "changedRev" in st else st for st in card["subtasks"]]
    return json.dumps(content, sort_keys=True)


def _subtask_contents(card: dict) -> dict:
    """Each delegated subtask's content as `wait --subtask` compares it: the item and its notes."""
    delegated = [st for st in card["subtasks"] if "delegation" in st]
    if not delegated:
        return {}
    notes = {}
    for entry in card["log"]:
        notes.setdefault(entry.get("subtaskId"), []).append(entry)
    return {st["id"]: json.dumps([{key: value for key, value in st.items() if key != "changedRev"},
                                  notes.get(st["id"], [])], sort_keys=True) for st in delegated}


def _subtask_stamps(card: dict) -> dict:
    return {st["id"]: st["changedRev"] for st in card["subtasks"] if "changedRev" in st}


def _stamp_subtasks(card: dict, stamps: dict) -> None:
    for st, _ in iter_subtasks(card.get("subtasks") or []):
        if "delegation" in st and st.get("id") in stamps:
            st["changedRev"] = stamps[st["id"]]
        else:
            st.pop("changedRev", None)


def save(board_path, doc: dict, by: str | None = None) -> int:
    """Commit the doc as one new revision and return it. Only cards that differ from what the
    transaction loaded are written, stamped changedRev = the new rev; loaded cards missing from the doc
    are deleted; each one's previous body goes to card_versions. Outside board_transaction it only
    creates a board whose board.json does not exist yet (init); an existing board raises RuntimeError,
    since a doc from a scoped load would delete every card it didn't load."""
    base = getattr(_store, "open", {}).get(_txn_key(board_path))  # The transaction checked the path.
    if base is None:
        board_path = require_write_scope(board_path)
        if board_path.exists() or _db(board_path).exists():
            raise RuntimeError(f"save() outside board_transaction only creates boards; {board_path} exists")
        _install_store(board_path, normalize_doc({"schemaVersion": SCHEMA_VERSION, **doc, "cards": []}))
        with board_transaction(board_path):
            return save(board_path, doc, by)
    db = _db(board_path)
    with _sql_errors(db):
        rev = _commit(base, doc, by)
    usage.note(rev=rev, wrote=True, cards=base.count, bytes=_size(db))
    return rev


def _commit(base: _Loaded, doc: dict, by: str | None) -> int:
    t0 = time.perf_counter()
    con = base.con
    if not con.in_transaction:  # A second save() in one transaction.
        con.execute("BEGIN IMMEDIATE")
        current = json.loads(con.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
        if current != base.rev:
            raise RevisionConflict(current)
    validate_schema({"schemaVersion": SCHEMA_VERSION, **doc})  # No version means the current one.
    rev = max(int(doc.get("rev") or 0), base.rev) + 1
    by = actor() if by is None else validate_actor(by)
    loaded, order, seen, written = base.cards, [], set(), {}
    for card in doc["cards"]:
        old = loaded.get(card.get("id")) if isinstance(card, dict) else None
        if old is not None and _dump(card) == old[0]:  # Untouched: no normalizing, no diff.
            cid = card["id"]
        else:
            canonical = normalize_card(card)
            cid = canonical["id"]
            old = loaded.get(cid)
            before = json.loads(old[0]) if old is not None else None
            if before is not None and _card_content(before) == _card_content(canonical):
                card["changedRev"] = before["changedRev"]  # Never accept a supplied or restored stamp.
                _stamp_subtasks(card, _subtask_stamps(before))
            else:
                card["changedRev"] = rev
                # Restamp the delegated subtasks that changed; an unchanged one without a stamp (legacy) gets
                # the card's old changedRev, an upper bound.
                now = _subtask_contents(canonical)
                prior = _subtask_contents(before) if now and before is not None else {}
                kept = _subtask_stamps(before) if before is not None else {}
                _stamp_subtasks(card, {sid: kept.get(sid, before["changedRev"]) if prior.get(sid) == content
                                       else rev for sid, content in now.items()})
                final = normalize_card(card)
                written[cid] = (final, _dump(final), old)
        if cid in seen:
            raise WorkflowError(f"duplicate card id {cid!r}", 409, "state")
        seen.add(cid)
        order.append(cid)
    gone = [cid for cid in loaded if cid not in seen]
    new = [cid for cid in order if cid not in loaded]
    kept_ids = [cid for cid in order if cid in loaded]
    slots = sorted(loaded[cid][1] for cid in kept_ids)
    placed = [loaded[cid][1] for cid in kept_ids] == slots and (
        not new or not kept_ids or order.index(new[0]) > order.index(kept_ids[-1]))
    if placed or base.scope != "all":
        # New cards go last; a reorder within a partial doc reuses the positions its cards held.
        pos = {} if placed else dict(zip(kept_ids, slots))
        top = con.execute("SELECT max(pos) FROM cards").fetchone()[0]
        pos.update((cid, (-1 if top is None else top) + 1 + i) for i, cid in enumerate(new))
    else:
        pos = {cid: i for i, cid in enumerate(order)}  # The whole board reordered: renumber.
    con.executemany("DELETE FROM cards WHERE id = ?", [(cid,) for cid in gone])
    for cid, (final, body, old) in written.items():
        if old is not None:
            con.execute("UPDATE cards SET num = ?, col = ?, done_at = ?, changed_rev = ?, pos = ?, deps = ?, "
                        "body = ? WHERE id = ?", (*_row(final, pos.get(cid, old[1]), body)[1:], cid))
    con.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [_row(final, pos[cid], body) for cid, (final, body, old) in written.items() if old is None])
    moved = [(p, cid) for cid, p in pos.items() if cid not in written and loaded[cid][1] != p]
    con.executemany("UPDATE cards SET pos = ? WHERE id = ?", moved)
    versions = ([(rev, cid, None if old is None else old[0]) for cid, (_, _, old) in written.items()]
                + [(rev, cid, loaded[cid][0]) for cid in gone])
    con.executemany("INSERT INTO card_versions VALUES (?, ?, ?)", versions)
    con.execute("DELETE FROM card_versions WHERE rev <= ?", (rev - VERSION_KEEP,))
    if rev % 64 == 0 or sum(len(body or "") for _, _, body in versions) > VERSION_BYTES >> 6:
        # Over the byte budget, whole revisions go from the oldest end, so every revision list_versions
        # offers stays restorable. Checked every 64 revisions or after a write over 1/64 of the budget,
        # so between checks the table grows by under VERSION_BYTES. octet_length reads no bodies.
        import sqlite3
        size = "octet_length" if sqlite3.sqlite_version_info >= (3, 43) else "length"
        con.execute(f"DELETE FROM card_versions WHERE rev <= (SELECT rev FROM (SELECT rev, sum({size}(body)) "
                    "OVER (ORDER BY rev DESC) total FROM card_versions) WHERE total > ? ORDER BY rev DESC LIMIT 1)",
                    (VERSION_BYTES,))
    meta = normalize_doc({key: value for key, value in doc.items()  # Every save writes the current schema.
                          if key not in ("cards", "changeEpoch", "changeJournal")}
                         | {"schemaVersion": SCHEMA_VERSION, "cards": []})
    del meta["cards"]
    high = con.execute("SELECT max(num) FROM cards").fetchone()[0]
    meta.update(schemaVersion=SCHEMA_VERSION, rev=rev, nextNum=max(meta["nextNum"], (high or 0) + 1),
                savedAt=now_iso(), savedBy=by)
    stored = {key: _dump(value) for key, value in meta.items()}
    con.executemany("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    [(key, value) for key, value in stored.items() if base.meta.get(key) != value])
    con.executemany("DELETE FROM meta WHERE k = ?", [(key,) for key in base.meta.keys() - stored.keys()])
    usage.stage("save", t0)
    t0 = time.perf_counter()
    con.execute("COMMIT")
    usage.stage("commit", t0)
    for key in ("changeEpoch", "changeJournal"):
        doc.pop(key, None)
    doc.update(meta)
    base.rev, base.meta = rev, stored
    for cid in gone:
        del loaded[cid]
    for cid, (_, body, old) in written.items():
        loaded[cid] = (body, pos.get(cid, old[1] if old else None))
    for p, cid in moved:
        loaded[cid] = (loaded[cid][0], p)
    base.count += len(new) - len(gone)
    return rev


def _oldest(con, current: int) -> int:
    first = con.execute("SELECT min(rev) FROM card_versions").fetchone()[0]
    return current if first is None else first - 1


def list_versions(board_path) -> dict:
    """What restore() can return to: {"rev": current, "oldest": oldest restorable rev, "revisions":
    [{"rev": R, "cards": [numbers R changed]}, ...]} newest first; restoring to R - 1 undoes R."""
    with snapshot(board_path) as con:
        current = json.loads(con.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
        revisions = {}
        for rev, num in con.execute(
                "SELECT v.rev, coalesce(c.num, json_extract(v.body, '$.num')) FROM card_versions v "
                "LEFT JOIN cards c ON c.id = v.card_id ORDER BY v.rev DESC, 2"):
            revisions.setdefault(rev, []).append(num)
        return {"rev": current, "oldest": _oldest(con, current),
                "revisions": [{"rev": rev, "cards": nums} for rev, nums in revisions.items()]}


def restore(board_path, rev: int, *, expected_rev=None, by: str | None = None) -> dict:
    """Return the cards to their state at revision `rev`, committed as a new revision (rev and nextNum
    keep increasing, so a restore can be undone too): each card changed since gets its earliest saved
    version after `rev`, cards created since are removed. Columns and other board settings stay."""
    if type(rev) is not int:
        raise WorkflowError("restore revision must be an integer")
    key = _txn_key(board_path)
    with board_transaction(board_path, expected_rev, scope="card") as current:
        con = _store.open[key].con
        oldest = _oldest(con, current["rev"])
        if not oldest <= rev < current["rev"]:
            raise WorkflowError(f"revision {rev} cannot be restored; restorable revisions are {oldest}.."
                                f"{current['rev'] - 1}" if oldest < current["rev"] else
                                "no earlier revision of this board is kept yet", 404)
        earliest = dict(con.execute("SELECT card_id, body FROM card_versions WHERE rev > ? ORDER BY rev DESC",
                                    (rev,)).fetchall())
        doc, base = _read(con, scope="card", ids=earliest)
        base.con = con
        _store.open[key] = base
        cards = {card["id"]: card for card in doc["cards"]}
        removed = [cards.pop(cid)["num"] for cid, body in earliest.items() if body is None and cid in cards]
        restored = []
        for cid, body in earliest.items():
            if body is not None:
                cards[cid] = json.loads(body)
                restored.append(cards[cid]["num"])
        doc["cards"] = list(cards.values())
        new_rev = save(board_path, doc, by)
    return {"ok": True, "rev": new_rev, "restoredTo": rev, "restored": restored, "removed": removed}


# ===== card copies =====

def card_copy(source: dict, by: str) -> dict:
    """Requirements `add --from` copies: title, pinned notes, tags, priority and the
    checklist with fresh s-N IDs and reset completion/delegation state."""
    by, at = validate_actor(by), now_iso()
    subtasks = []
    for number, item in enumerate(source["subtasks"], 1):
        fresh = {"id": f"s-{number}", "text": item["text"], "done": False, "createdAt": at,
                 "doneAt": None, "by": by}
        if delegation := item.get("delegation"):
            fresh["delegation"] = {"state": "available", "scope": list(delegation["scope"])}
        subtasks.append(fresh)
    return {"title": source["title"], "notes": source["notes"], "tags": list(source["tags"]),
            "priority": source["priority"], "subtasks": subtasks}


# ===== card operations =====

class RefError(Exception):
    pass


def resolve_ref(doc: dict, ref: str) -> dict:
    ref = str(ref).strip().lstrip("#")
    if ref.isdigit():
        num = int(ref)
        for c in doc["cards"]:
            if c["num"] == num:
                return c
        raise RefError(f"no card #{num}")
    for c in doc["cards"]:
        if c["id"] == ref:
            return c
    matches = [c for c in doc["cards"]
               if c["code"] and c["code"].lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]
    prefixes = [c for c in doc["cards"] if c["id"].startswith(ref)]
    if len(prefixes) == 1:
        return prefixes[0]
    if len(prefixes) > 1:
        raise RefError(f"'{ref}' is ambiguous: "
                       + ", ".join(f"#{c['num']} {c['id']}" for c in prefixes[:6]))
    raise RefError(f"no card matching '{ref}'")


def ensure_column(doc: dict, col_id: str) -> dict:
    requested = slugify(col_id, 24)
    aliases = {"in-progress": "inprogress", "in_progress": "inprogress"}
    requested = aliases.get(requested, requested)
    for col in doc["columns"]:
        if col["id"] == requested or slugify(col.get("name") or "", 24) == requested:
            if col["id"] not in CORE_COLUMN_ID_SET:
                raise RefError(
                    f"'{col_id}' is not a core column; use "
                    + ", ".join(CORE_COLUMN_IDS)
                )
            return col
    if requested not in CORE_COLUMN_ID_SET:
        raise RefError(
            f"unknown core column '{col_id}'; use " + ", ".join(CORE_COLUMN_IDS)
        )
    col = dict(CORE_COLUMN_BY_ID[requested])
    doc["columns"].append(col)
    order = {column_id: i for i, column_id in enumerate(CORE_COLUMN_IDS)}
    doc["columns"].sort(key=lambda item: order.get(item["id"], len(order)))
    return col


def column_ids(doc: dict) -> set:
    return {c["id"] for c in doc["columns"]}


def new_num(doc: dict) -> int:
    n = doc["nextNum"]
    doc["nextNum"] = n + 1
    return n


def validate_new_identity(doc: dict, card_id) -> str:
    if (not isinstance(card_id, str) or len(card_id) > 120
            or not re.fullmatch(
                r"(?:[a-z0-9]+(?:-[a-z0-9]+)*-)?(?:[0-9a-f]{32}|"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                card_id)):
        raise WorkflowError("new card IDs must contain a UUID, not a reusable title slug")
    if any(c["id"] == card_id or card_id in (c.get("dependsOn") or []) for c in doc["cards"]):
        raise WorkflowError("card ID already exists or is reserved by an existing reference", 409)
    return card_id


def _uuid_hex() -> str:
    import uuid  # Lazy: only writes need it.
    return uuid.uuid4().hex


def unique_id(doc: dict, base: str) -> str:
    return validate_new_identity(doc, f"{slugify(base)}-{_uuid_hex()}")


def _required_text(value, label: str, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be nonempty text")
    if limit is not None and len(value) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return value.strip()


def _dependencies_satisfied(card: dict, by_id: dict) -> bool:
    return all(dep in by_id and by_id[dep].get("column") == "done"
               and by_id[dep].get("outcome", "completed") == "completed"
               for dep in (card.get("dependsOn") or []))


def _require_start(doc: dict, card: dict, ids: list | None = None, *, wip: bool = True) -> None:
    """Card prerequisites (deps), then, unless wip is false, the In Progress WIP limit (wip)."""
    by_id = {c["id"]: c for c in doc["cards"]}
    unmet = [f"#{by_id[dep]['num']} ({'canceled' if by_id[dep]['column'] == 'done' else by_id[dep]['column']})"
             if dep in by_id else f"{dep} (missing)"
             for dep in ((card.get("dependsOn") or []) if ids is None else ids)
             if not _dependencies_satisfied({"dependsOn": [dep]}, by_id)]
    if unmet:
        hint = ("finish them first; a canceled or missing one never completes, so change dependencies"
                if ids is None else f"{fmt_ref(card)} is In Progress and can't take an unfinished "
                "prerequisite; block it until they finish")
        raise WorkflowError(f"unmet prerequisites: {', '.join(unmet)}; {hint}", 409, "deps")
    column = next((c for c in doc["columns"] if c["id"] == "inprogress"), {})
    limit = column.get("wipLimit")
    if wip and card["column"] != "inprogress" and type(limit) is int and limit > 0:
        active = sum(c["column"] == "inprogress" for c in doc["cards"])
        if active >= limit:
            raise WorkflowError(
                f"In Progress holds {active} cards and its limit is {limit}, set by the user in the browser; "
                "finish or block active work, or ask the user to raise or clear the limit", 409, "wip")


def _archive_cycle(card: dict, mode: str, reason: str, by: str) -> None:
    if card["column"] != "done":
        return
    card.setdefault("cycles", []).append({
        "doneAt": card.get("doneAt"), "writeup": card.get("writeup", ""),
        "outcome": card.get("outcome") or "completed",
        "cancelReason": card.get("cancelReason"),
        "verification": card.get("verification") or [],
        "reviews": card.get("reviews") or [],
        "mode": "ship" if card.get("outcome") != "canceled" else "cancel",
        "reopenedAt": now_iso(), "reopenMode": mode, "reason": reason, "by": by,
    })


def workflow_action(doc: dict, card: dict, action: str, details: dict, by: str) -> dict:
    """Apply one domain action; callers hold the board lock and commit with save."""
    by = validate_actor(by)
    if not isinstance(details, dict):
        raise WorkflowError("action details must be an object")
    if not any(c is card for c in doc["cards"]):
        raise WorkflowError("action must use the current board card", 409)
    supported = {"start", "complete", "block", "resume", "takeover", "cancel",
                 "rework", "reopen", "bug", "improve", "move", "dependencies", "note", "subtask"}
    if not isinstance(action, str) or action not in supported:
        raise WorkflowError(f"unsupported lifecycle action '{action}'")
    frm = card["column"]
    owner = card.get("activeOwner")
    if action == "subtask":
        operation = details.get("op")
        if not isinstance(operation, str):
            raise WorkflowError("subtask action needs an operation")
        return subtask_action(doc, card, operation, details, by)
    if action == "note":   # timeline entries carry no ownership guard
        append_note(card, details.get("summary"), details.get("body"), by)
        return card
    # Input first, so a malformed request is invalid for anyone; then authority (owned), then state.
    destination, note, event, reason = frm, details.get("note"), action, None
    if note is not None and not isinstance(note, str):
        raise WorkflowError("note must be text")
    if action in ("takeover", "block", "cancel", "rework", "reopen", "bug", "improve"):
        reason = _required_text(details.get("text" if action == "improve" else "reason"), f"{action} reason")
    if action == "block":
        until = _required_text(details.get("until"), "unblock condition")
    elif action == "complete":
        writeup = _required_text(details.get("writeup"), "completion writeup")
    elif action == "resume":
        note = _required_text(note, "resolution note")
        destination = details.get("to", "inprogress")
        if destination not in ("task", "inprogress"):
            raise WorkflowError("resume.to must be task or inprogress")
    elif action == "reopen":
        destination = details.get("to", "task")
        if destination not in ("task", "inprogress"):
            raise WorkflowError("reopen.to must be task or inprogress")
        if details.get("as", "task") not in ("task", "bug", "improve"):
            raise WorkflowError("reopen.as must be task, bug, or improve")
    elif action == "move":
        target = details.get("to")
        if not isinstance(target, str):
            raise WorkflowError("move.to must name a core column")
        destination = slugify(target, 24)
        destination = {"in-progress": "inprogress"}.get(destination, destination)
        if destination not in CORE_COLUMN_ID_SET:
            raise WorkflowError("unknown core column; use " + ", ".join(CORE_COLUMN_IDS))
    elif action == "dependencies":
        ids = details.get("ids")
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
            raise WorkflowError("dependencies.ids must be a list of card IDs")
        ids = list(dict.fromkeys(ids))
        by_id = {c["id"]: c for c in doc["cards"]}
        if card["id"] in ids:
            raise WorkflowError("a card cannot depend on itself")
        if any(i not in by_id for i in ids):
            raise WorkflowError("dependency card does not exist")
        pending, seen = list(ids), set()
        while pending:
            dependency = pending.pop()
            if dependency == card["id"]:
                raise WorkflowError("dependency cycle is not allowed")
            if dependency in seen:
                continue
            seen.add(dependency)
            pending.extend(by_id.get(dependency, {}).get("dependsOn") or [])
    # done (complete) and cancel close another holder's card too; its history names them (previousOwner).
    if action not in ("takeover", "complete", "cancel") and owner and owner != by:
        raise WorkflowError(f"owned by {owner}; use takeover with a reason", 409, "owned")
    if action == "takeover":
        if frm != "inprogress":
            raise WorkflowError("takeover requires an In Progress card", 409, "state")
        if owner == by:
            return card
        card["activeOwner"], card["claimedAt"] = by, now_iso()
        hist(card, "takeover", by=by, note=f"{owner or 'unowned'} → {by}: {reason}")
        touch(card)
        return card
    if action == "dependencies":
        if frm == "inprogress":
            _require_start(doc, card, ids)
        if ids == (card.get("dependsOn") or []):
            return card
        card["dependsOn"] = ids
        hist(card, "dependencies", by=by, note=", ".join(ids) or "cleared")
        touch(card)
        return card
    if action == "move":
        if destination == frm and frm != "inprogress":
            return card
        if destination == "blocked":
            raise WorkflowError("use block with a reason and until condition", 409, "state")
        if frm == "blocked":
            raise WorkflowError("use resume with a resolution note", 409, "state")
        if frm == "done":
            raise WorkflowError("leaving Done requires reasoned reopen or rework", 409, "state")
        if destination == "done":
            return workflow_action(doc, card, "complete",
                                   {"writeup": details.get("writeup", card.get("writeup"))}, by)
        if destination == "inprogress":
            action = "start"
    if action == "start":
        if frm == "blocked":
            raise WorkflowError("use resume with a resolution note", 409, "state")
        if frm == "done":
            raise WorkflowError("leaving Done requires reasoned reopen or rework", 409, "state")
        if frm == "inprogress" and owner == by:
            return card
        _require_start(doc, card)
        destination = "inprogress"
    elif action == "complete":
        if frm == "done":
            raise WorkflowError(f"{fmt_ref(card)} is already Done; rework or reopen it to change the result",
                                409, "state")
        if frm == "blocked":
            raise WorkflowError("use resume with a resolution note", 409, "state")
        if unfinished := [st for st in unaccepted_delegations(card) if st["delegation"]["state"] != "completed"]:
            raise WorkflowError(f"delegated work keeps {fmt_ref(card)} open: "
                                + "; ".join(_unfinished_delegation(st, by) for st in unfinished), 409, "state")
        if frm != "inprogress":  # Started work keeps its claim even if a prerequisite was reworked since.
            _require_start(doc, card, wip=False)
        destination = "done"
    elif action == "block":
        if frm == "done":
            raise WorkflowError("reopen Done work before blocking it", 409, "state")
        destination, event, note = "blocked", "blocked", f"{reason}; unblocks when {until}"
    elif action == "resume":
        if frm != "blocked":
            raise WorkflowError("resume requires a Blocked card", 409, "state")
        if destination == "inprogress":
            _require_start(doc, card)
        event = "unblocked"
    elif action == "cancel":
        if frm == "done":
            raise WorkflowError("only nonterminal work can be canceled; rework it first", 409, "state")
        destination, note = "done", reason
    elif action in ("rework", "reopen", "bug", "improve"):
        if action != "reopen":
            destination = "inprogress" if action in ("bug", "improve") else "task"
        if destination == "inprogress":
            _require_start(doc, card)
        note = reason
        event = "reopened" if action == "reopen" else action

    ensure_column(doc, destination)
    if action in ("rework", "reopen", "bug", "improve"):
        _archive_cycle(card, details.get("as", action), reason, by)
        card["writeup"], card["verification"], card["reviews"] = "", [], []
        card["reworkReason"] = card["reopenReason"] = reason
        card["cancelReason"] = None
        if action in ("bug", "improve"):
            prefix = "s-bug-" if action == "bug" else "s-imp-"
            existing = {s["id"] for s, _ in iter_subtasks(card.get("subtasks") or [])}
            n = len(existing) + 1
            while f"{prefix}{n}" in existing:
                n += 1
            card.setdefault("subtasks", []).append({
                "id": f"{prefix}{n}", "text": f"fix bug: {reason}" if action == "bug" else reason,
                "done": False, "createdAt": now_iso(), "doneAt": None, "by": by,
            })
            if action == "bug" and "bug" not in card["tags"]:
                card["tags"].append("bug")
    if action == "complete":
        for item in unaccepted_delegations(card):  # Done accepts finished results, as subtask.accept does.
            _accept_result(card, item, by)
            hist(card, "subtask-accept", by=by, note=item["id"])
        card["writeup"], card["outcome"] = writeup, "completed"
        card["reworkReason"] = card["cancelReason"] = card["reopenReason"] = None
        card["tags"] = [tag for tag in card["tags"] if tag != "bug"]
    elif action == "cancel":
        card["outcome"], card["cancelReason"] = "canceled", reason
        card["reworkReason"] = card["reopenReason"] = None
    else:
        card["outcome"] = None
    if action == "block":
        card["blockedReason"], card["unblockWhen"], card["blockedAt"] = reason, until, now_iso()
    elif destination != "blocked":
        card["blockedReason"] = card["unblockWhen"] = card["blockedAt"] = None
    card["column"] = destination
    card["doneAt"] = now_iso() if destination == "done" else None
    if destination == "inprogress":
        if frm != "inprogress" or not owner:
            card["activeOwner"], card["claimedAt"] = by, now_iso()
    else:
        card["activeOwner"] = card["claimedAt"] = None
    hist(card, event, by=by, note=note, frm=frm, to=destination)
    if owner and owner != by:
        card["history"][-1]["previousOwner"] = owner
    touch(card)
    return card


def create_card(doc: dict, by: str, *, title, column="task", priority=None, tags=(), origin="", notes=None,
                depends_on=(), source=None) -> dict:
    """Add one card to `doc`; the caller saves. In Progress applies the start rules and the creator owns it.
    `source` copies requirements as `add --from` does; a None title, priority or notes comes from it."""
    by = validate_actor(by)
    copied = card_copy(source, by) if source is not None else {}
    title = _required_text(copied.get("title") if title is None else title, "card title")
    try:
        column = ensure_column(doc, column)["id"]
    except RefError as e:
        raise WorkflowError(str(e)) from e
    if column not in ("backlog", "task", "inprogress"):
        raise WorkflowError("new cards enter Backlog, Task, or In Progress; use lifecycle commands for later "
                            "stages", 409, "state")
    card = normalize_card({
        "num": new_num(doc), "id": unique_id(doc, title), "title": title,
        "column": "task" if column == "inprogress" else column,
        "priority": copied.get("priority") if priority is None else priority,
        "tags": list(dict.fromkeys([*copied.get("tags", []), *tags])),
        "origin": origin, "notes": copied.get("notes", "") if notes is None else notes,
        "subtasks": copied.get("subtasks", [])})
    hist(card, "created", by=by, note=origin or None)
    doc["cards"].append(card)
    if depends_on:
        workflow_action(doc, card, "dependencies", {"ids": list(depends_on)}, by)
    if column == "inprogress":
        workflow_action(doc, card, "start", {}, by)
    return card


def _iso_time(value) -> float:
    try:
        date = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        return date.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return float("inf")


def ready_cards(doc: dict) -> list:
    by_id = {c["id"]: c for c in doc["cards"]}
    cards = [c for c in doc["cards"] if c["column"] == "task" and not c.get("activeOwner")
             and _dependencies_satisfied(c, by_id)]
    priority = {"critical": 0, "mid": 1, "low": 2}
    return sorted(cards, key=lambda c: (
        priority.get(c.get("priority"), 3), _iso_time(c.get("createdAt")),
        c["num"] if type(c.get("num")) is int else float("inf"), c["id"]))


def board_stats(doc: dict) -> dict:
    stats = dict.fromkeys(("total", "open", "ready", "blocked", "inprogress",
                          "completed", "canceled", "rework", "completedLast7Days"), 0)
    stats["byColumn"] = dict.fromkeys(CORE_COLUMN_IDS, 0)
    stats["byPriority"] = dict.fromkeys(("critical", "mid", "low", "unset"), 0)
    stats["byOwner"] = {}
    now = time.time()
    for card in doc["cards"]:
        column = card["column"]
        stats["total"] += 1
        stats["byColumn"][column] = stats["byColumn"].get(column, 0) + 1
        priority = card.get("priority") or "unset"
        stats["byPriority"][priority] = stats["byPriority"].get(priority, 0) + 1
        if column == "done":
            canceled = card.get("outcome") == "canceled"
            stats["canceled" if canceled else "completed"] += 1
            if not canceled and now - 7 * 86400 <= _iso_time(card.get("doneAt")) <= now:
                stats["completedLast7Days"] += 1
        else:
            stats["open"] += 1
            stats["rework"] += bool(card.get("reworkReason"))
        if column in ("blocked", "inprogress"):
            stats[column] += 1
        if column == "inprogress" and card.get("activeOwner"):
            owner = card["activeOwner"]
            stats["byOwner"][owner] = stats["byOwner"].get(owner, 0) + 1
    stats["ready"] = len(ready_cards(doc))
    return stats


_STAT_FIELDS = ("priority", "outcome", "reworkReason", "activeOwner", "createdAt", "doneAt")


def store_stats(con) -> dict:
    """board_stats from one SQL row per card in board order (snapshot()): the fields it reads, not the cards."""
    paths = ", ".join(f"'$.{key}'" for key in _STAT_FIELDS)
    return board_stats({"cards": [
        {"id": cid, "num": num, "column": col, "dependsOn": json.loads(deps),
         **dict(zip(_STAT_FIELDS, json.loads(fields)))}
        for cid, num, col, deps, fields in
        con.execute(f"SELECT id, num, col, deps, json_extract(body, {paths}) FROM cards ORDER BY pos")]})


def append_note(card: dict, summary, body, by: str) -> dict:
    """Append one timeline entry (summary per fit_note, optional markdown body) and return it."""
    by = validate_actor(by)
    summary, body = fit_note(summary, body)
    entry = {"id": _uuid_hex(), "at": now_iso(), "by": by, "summary": summary, "body": body}
    card.setdefault("log", []).append(entry)
    hist(card, "note", by=by, note=summary[:80])
    touch(card)
    return entry


def _note_entry(card: dict, note_id) -> dict:
    entry = next((entry for entry in card.get("log") or [] if entry["id"] == note_id), None)
    if entry is None:
        raise WorkflowError(f"note {note_id!r} not found on card #{card['num']}", 404)
    return entry


def _require_note_author(entry: dict, by: str) -> None:
    # Generated entries (result, accepted, takeover...) are the delegation record: review and inbox read them.
    if entry.get("kind") == "generated":
        raise WorkflowError(f"note {entry['id']} is a workflow record ({entry['summary']}); it can't be "
                            "changed", 409, "state")
    if entry.get("by") != by:
        raise WorkflowError(f"note {entry['id']} was written by {entry.get('by') or 'an unknown author'}; "
                            "only its author can change it", 409, "owned")


def edit_note(card: dict, note_id, summary, body, by: str) -> dict:
    """Replace the summary and/or body of the actor's own timeline note; stamps editedAt."""
    by = validate_actor(by)
    if summary is None and body is None:
        raise WorkflowError("note edit needs a new summary or body")
    entry = _note_entry(card, note_id)
    summary, body = fit_note(entry["summary"] if summary is None else summary,
                             entry["body"] if body is None else body)
    _require_note_author(entry, by)
    entry.update(summary=summary, body=body, editedAt=now_iso())
    hist(card, "note-edited", by=by, note=entry["id"])
    touch(card)
    return entry


def remove_note(card: dict, note_id, by: str) -> dict:
    """Remove the actor's own timeline note; returns the entry."""
    by = validate_actor(by)
    entry = _note_entry(card, note_id)
    _require_note_author(entry, by)
    card["log"].remove(entry)
    hist(card, "note-removed", by=by, note=entry["id"])
    touch(card)
    return entry


def attachment_path(board_path: Path, attachment_id: str) -> Path:
    if not isinstance(attachment_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
        raise WorkflowError("invalid attachment ID")
    root = Path(board_path).absolute().parent / "attachments"
    path = root / attachment_id
    for candidate in (path, *path.parents):
        if _has_reparse_point(candidate):
            raise WorkflowError("attachment path crosses a link or reparse point")
    if root.exists() and not root.is_dir():
        raise WorkflowError("attachment storage is not a directory")
    if path.exists():
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkflowError("attachment is not a private regular file")
    if path.resolve(strict=False).parent != root.resolve(strict=False):
        raise WorkflowError("attachment path escapes storage")
    return path


def attachment_store(board_path: Path, name: str, data: bytes, by: str,
                     mime: str | None = None) -> dict:
    import hashlib
    import mimetypes
    import tempfile
    board_path = require_write_scope(board_path)
    by = validate_actor(by)
    name = _required_text(name, "attachment name", 255)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise WorkflowError("attachment name contains control characters")
    if not isinstance(data, bytes):
        raise WorkflowError("attachment data must be bytes")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise WorkflowError("attachment exceeds 10 MiB", 413)
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if (not isinstance(mime, str) or len(mime) > 255
            or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)):
        raise WorkflowError("invalid attachment MIME type")
    attachment_id = _uuid_hex()
    path = attachment_path(board_path, attachment_id)
    require_write_scope(path)
    path.parent.mkdir(exist_ok=True)
    path = attachment_path(board_path, attachment_id)
    if path.exists():
        raise WorkflowError("attachment ID collision", 409)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".upload-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        attachment_path(board_path, attachment_id)
        os.link(tmp, path)  # Immutable IDs must never replace recovery bytes, even in a race.
        os.unlink(tmp)
        _fsync_parent_directory(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # ponytail: retained recovery blobs may grow; reachability GC only if storage becomes a problem.
    return {"id": attachment_id, "name": name, "size": len(data), "mime": mime,
            "sha256": hashlib.sha256(data).hexdigest(), "createdAt": now_iso(), "by": by}


def attachment_remove(board_path: Path, attachment_id: str) -> None:
    """Remove only a failed, uncommitted upload; user detach must retain its bytes."""
    require_write_scope(board_path)
    path = attachment_path(board_path, attachment_id)
    require_write_scope(path)
    path.unlink(missing_ok=True)
    _fsync_parent_directory(path)


def card_context(board_path: Path, ref: str, full: bool = False, *,
                 subtask_id: str | None = None, assigned_to: str | None = None) -> dict:
    """One snapshot read of the card, its dependencies and dependents (focused context also loads the
    open cards, for scope warnings); no lock file, unread state, or embedded file bytes.

    Default output keeps pinned notes and open subtasks whole but trims done subtasks
    to the 10 most recently completed, leaves out history (counted in omitted), and
    trims the note log to the newest 10 entries.
    """
    focused = subtask_id is not None or assigned_to is not None
    return _card_context_from_doc(load(board_path, (ref,), "open" if focused else "card"), board_path, ref,
                                  full, subtask_id=subtask_id, assigned_to=assigned_to)


def _card_context_from_doc(doc: dict, board_path: Path, ref: str, full: bool = False, *,
                           subtask_id: str | None = None, assigned_to: str | None = None) -> dict:
    """Context projection of one loaded document; handoff reuses it."""
    card = resolve_ref(doc, ref)
    original_card = card
    focus, unrelated = None, 0
    if subtask_id is not None or assigned_to is not None:
        if subtask_id is not None and assigned_to is not None:
            raise WorkflowError("choose either subtask or assigned-to context")
        index = _subtask_index(card)
        if subtask_id is not None:
            subtask_id = _required_text(subtask_id, "subtask ID")
            if subtask_id not in index:
                raise WorkflowError(f"subtask '{subtask_id}' not found", 404)
            selected = [subtask_id]
            focus = {"subtaskId": subtask_id}
        else:
            assigned_to = validate_actor(assigned_to)
            selected = [sid for sid, (st, _) in index.items()
                        if (st.get("delegation") or {}).get("owner") == assigned_to]
            focus = {"assignedTo": assigned_to}
        card = {**card, "subtasks": [st for st in card["subtasks"] if st["id"] in selected]}
        unrelated = len(index) - len(selected)
        focus["matchedSubtaskIds"] = selected
    by_id = {item["id"]: item for item in doc["cards"]}
    dependencies, missing = [], []
    for dependency_id in card["dependsOn"]:
        dependency = by_id.get(dependency_id)
        if dependency is None:
            missing.append(dependency_id)
            continue
        dependencies.append({
            **{key: dependency.get(key) for key in ("id", "num", "title", "column", "outcome")},
            "satisfied": dependency["column"] == "done" and dependency["outcome"] == "completed",
        })
    dependents = [{key: item.get(key) for key in ("num", "id", "title", "column", "outcome")}
                  for item in doc["cards"] if card["id"] in item["dependsOn"]]
    context = {"ok": True, "board": str(Path(board_path).absolute()), "schemaVersion": doc["schemaVersion"],
               "rev": doc["rev"], "num": card["num"], "card": card, "dependencies": dependencies,
               "missingDependencies": missing, "dependents": dependents,
               "ready": card["column"] == "task" and not card.get("activeOwner")
                        and _dependencies_satisfied(card, by_id)}
    if focus is not None:
        focus["scopeWarnings"] = {
            sid: scope_conflicts(doc, original_card, sid) for sid in selected
            if (index[sid][0].get("delegation") or {}).get("scope")
        }
        context["focus"] = focus
        context["omitted"] = {"unrelatedSubtasks": unrelated}
    if full:
        return context
    done = [st for st in card["subtasks"] if st["done"]]
    recent = {id(st) for st in sorted(done, key=lambda st: st.get("doneAt") or "")[-10:]}
    # A requested assignment must survive completed-work trimming.
    subtasks = card["subtasks"] if focus is not None else [
        st for st in card["subtasks"] if not st["done"] or id(st) in recent]
    omitted = {"doneSubtasks": len(card["subtasks"]) - len(subtasks), "history": len(card["history"])}
    if len(card["log"]) > 10:
        omitted["log"] = len(card["log"]) - 10
    context["card"] = {**{key: value for key, value in card.items() if key != "history"}, "subtasks": subtasks,
                       "log": card["log"][-10:]}
    if any(omitted.values()):
        context.setdefault("omitted", {}).update(omitted)
    return context


def subtask_handoff(board_path: Path, ref: str, subtask_id: str, by: str, worker: str) -> dict:
    """Validate and describe a read-only handoff; no claim, spawn, or message."""
    by, worker = validate_actor(by), validate_actor(worker)
    if worker == by:
        raise WorkflowError("worker must differ from the parent owner")
    doc = load(board_path, (ref,), "open")
    context = _card_context_from_doc(doc, board_path, ref, subtask_id=subtask_id)
    card = resolve_ref(doc, context["card"]["id"])
    _require_parent_owner(card, by, "handoff")
    index = _subtask_index(card)
    item = index[context["focus"]["subtaskId"]][0]
    sid, assignment = item["id"], item.get("delegation")
    if assignment is None:
        raise WorkflowError(f"{sid} is a plain checklist item; delegate it first", 409, "state")
    # An empty saved scope means read-only work; claimed work held by another actor is a takeover.
    state, holder = assignment["state"], assignment["owner"]
    previous = holder if state == "claimed" and holder != worker else None
    if state not in ("available", "claimed") and previous is None:
        raise WorkflowError(f"{sid} is {state}" + (f" by {holder}" if holder else "") + "; hand off available "
                            "work, or claimed work held by another worker", 409, "state")
    return {"ok": True, "board": context["board"], "rev": context["rev"], "num": card["num"],
            "title": card["title"], "worker": worker, "previousOwner": previous, "assignment": item,
            "scopeWarnings": scope_conflicts(doc, card, sid), "writeScope": list(assignment["scope"]),
            "origin": card["origin"], "notes": card["notes"]}


def _attachment_member(card: dict, attachment_id: str) -> dict:
    item = next((item for item in card["attachments"] if item.get("id") == attachment_id), None)
    if item is None:
        raise WorkflowError("attachment is not present on this card", 404)
    return item


def attachment_add(board_path: Path, ref: str, name: str, data: bytes, by: str,
                   mime: str | None = None, expected_rev=None, card_scoped: bool = True) -> tuple:
    """card_scoped=False keeps the browser's exact board-revision guard; either way only the card loads."""
    with board_transaction(board_path, expected_rev, ref if card_scoped else None, "card", (ref,)) as doc:
        card = resolve_ref(doc, ref)
        metadata = attachment_store(board_path, name, data, by, mime)
        try:
            card["attachments"].append(metadata)
            hist(card, "attachment-added", by=by, note=metadata["name"])
            touch(card)
            save(board_path, doc, by=by)
        except BaseException:
            # An uncertain/partially finalized commit must retain recoverable bytes.
            try:
                committed = load(board_path, (card["id"],), "card")
            except (OSError, ValueError, KeyError, TypeError):
                committed = None
            if committed is not None and not any(
                    item.get("id") == metadata["id"] for saved_card in committed["cards"]
                    for item in saved_card["attachments"]):
                attachment_remove(board_path, metadata["id"])
            raise
    return doc, card, metadata


def attachment_detach(board_path: Path, ref: str, attachment_id: str, by: str,
                      expected_rev=None, card_scoped: bool = True) -> tuple:
    by = validate_actor(by)
    with board_transaction(board_path, expected_rev, ref if card_scoped else None, "card", (ref,)) as doc:
        card = resolve_ref(doc, ref)
        metadata = _attachment_member(card, attachment_id)
        if metadata.get("by") and metadata["by"] != by and card.get("activeOwner") != by:
            raise WorkflowError("only the attachment author or card owner can detach it", 409, "owned")
        card["attachments"].remove(metadata)
        hist(card, "attachment-removed", by=by, note=metadata["name"])
        touch(card)
        save(board_path, doc, by=by)
    # Detached bytes belong to backups, archives, and deleted-board recovery too.
    return doc, card, metadata


def attachment_verified_bytes(board_path: Path, metadata: dict) -> bytes:
    import hashlib
    size = metadata.get("size")
    if type(size) is not int or not 0 <= size <= MAX_ATTACHMENT_BYTES:
        raise WorkflowError("attachment declared size is outside 0..10 MiB", 413)
    expected_hash = metadata.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch("[0-9a-f]{64}", expected_hash):
        raise WorkflowError("attachment has an invalid SHA256", 409)
    path = attachment_path(board_path, metadata.get("id"))
    try:
        with open_shared(path, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkflowError("attachment is not a private regular file", 409)
            body = stream.read(MAX_ATTACHMENT_BYTES + 1)
    except FileNotFoundError as exc:
        raise WorkflowError("attachment bytes are missing", 404) from exc
    attachment_path(board_path, metadata["id"])
    if len(body) > MAX_ATTACHMENT_BYTES:
        raise WorkflowError("attachment bytes exceed 10 MiB", 413)
    if len(body) != size or hashlib.sha256(body).hexdigest() != expected_hash:
        raise WorkflowError("attachment bytes do not match their recorded size/hash", 409)
    return body


def attachment_read(board_path: Path, ref: str, attachment_id: str) -> tuple:
    doc = load(board_path, (ref,), "card")
    card = resolve_ref(doc, ref)
    metadata = _attachment_member(card, attachment_id)
    body = attachment_verified_bytes(board_path, metadata)
    return doc, card, metadata, body


def require_export_destination(destination, board_path: Path) -> Path:
    path = require_write_scope(destination)
    if path.exists() or path.is_symlink():
        raise WorkflowError(f"export destination already exists: {path}; choose a new path")
    managed = [Path(board_path).absolute().parent, home(), Path(__file__).resolve().parent]
    if any(path.is_relative_to(root.resolve(strict=False)) for root in managed):
        raise WorkflowError("exports cannot target managed board, registry, or runtime storage", 403)
    for ancestor in path.parents:
        if os.path.normcase(ancestor.name) in (".workboard", ".git") or (ancestor / "board.json").exists():
            raise WorkflowError("exports cannot target managed storage", 403)
    return path


def attachment_export(board_path: Path, ref: str, attachment_id: str, destination) -> dict:
    import tempfile
    path = require_export_destination(destination, board_path)
    doc, card, metadata, body = attachment_read(board_path, ref, attachment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    require_export_destination(path, board_path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".download-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        require_export_destination(path, board_path)
        # Hard-link publication is atomic and fails if any destination wins the race.
        os.link(temporary, path)
        _fsync_parent_directory(path)
    finally:
        os.unlink(temporary)
    return {"ok": True, "board": str(Path(board_path).absolute()), "rev": doc["rev"],
            "cardId": card["id"], "id": card["id"], "num": card["num"], "attachment": metadata,
            "out": str(path), "sha256": metadata["sha256"]}


def touch(card: dict) -> None:
    card["updatedAt"] = now_iso()


def hist(card: dict, ev: str, by: str | None = None, note: str | None = None,
         frm: str | None = None, to: str | None = None) -> None:
    entry = {"at": now_iso(), "ev": ev}
    if frm is not None:
        entry["from"] = frm
    if to is not None:
        entry["to"] = to
    if by:
        entry["by"] = by
    if note:
        entry["note"] = note
    card.setdefault("history", []).append(entry)


def _legacy_core_destination(column: dict | None) -> str | None:
    if column is None:
        return "backlog"
    column_id = str(column.get("id") or "")
    if column_id in CORE_COLUMN_ID_SET:
        return column_id
    key = re.sub(
        r"[^a-z0-9]+", "",
        f"{column_id} {column.get('name', '')} {column.get('kind', '')}".lower(),
    )
    if any(token in key for token in ("discard", "trash", "archive")):
        return None
    if "review" in key:
        return "inprogress"
    if any(token in key for token in ("blocked", "waiting", "onhold", "paused")):
        return "blocked"
    if any(token in key for token in ("done", "complete", "deployed", "shipped")):
        return "done"
    if any(token in key for token in ("urgent", "mandatory", "bug", "ready")):
        return "task"
    if column.get("kind") == "active":
        return "inprogress"
    if column.get("kind") == "done":
        return "done"
    if column.get("kind") == "blocked":
        return "blocked"
    return "backlog"


def consolidate_to_core_columns(doc: dict) -> dict:
    """Mutate one board to the five-column model and return a migration report."""
    columns = {column["id"]: column for column in doc.get("columns") or []}
    removed_columns = [
        column["id"] for column in doc.get("columns") or []
        if column["id"] not in CORE_COLUMN_ID_SET
    ]
    moved: dict[str, int] = {}
    purged = []
    kept = []
    for card in doc.get("cards") or []:
        source = str(card.get("column") or "")
        source_column = columns.get(source)
        destination = _legacy_core_destination(source_column)
        if destination is None:
            purged.append(card)
            continue
        if destination != source:
            moved[f"{source or '(missing)'}->{destination}"] = (
                moved.get(f"{source or '(missing)'}->{destination}", 0) + 1
            )
            hist(
                card, "column-consolidated", by=actor(),
                note="five-column migration", frm=source or None, to=destination,
            )
            card["column"] = destination
            # This is a legacy role rename, not a new lifecycle transition.
            card["outcome"] = ("canceled" if card.get("outcome") == "canceled" else "completed") \
                if destination == "done" else None
            if destination != "inprogress":
                card["activeOwner"] = card["claimedAt"] = None
            key = re.sub(
                r"[^a-z0-9]+", "",
                f"{source} {source_column.get('name', '') if source_column else ''}".lower(),
            )
            if "idea" in key and "idea" not in card["tags"]:
                card["tags"].append("idea")
            if "note" in key and "note" not in card["tags"]:
                card["tags"].append("note")
        kept.append(card)
    doc["cards"] = kept
    doc["columns"] = [dict(column, stackUnder=None) for column in DEFAULT_COLUMNS]
    return {
        "removedColumns": removed_columns,
        "moved": moved,
        "purgedCards": purged,
    }


def iter_subtasks(subtasks, parent=None):
    for st in subtasks:
        yield st, parent
        yield from iter_subtasks(st.get("children") or [], st)


def guard_card_edit(card: dict, by: str) -> None:
    """Delegation management belongs to the card's active owner; anyone may act on an unowned card."""
    by = validate_actor(by)
    owner = card.get("activeOwner")
    if owner and owner != by:
        raise WorkflowError(f"only {owner}, the card's owner, can add, delegate or remove assignments; "
                            f"ask {owner}", 409, "owned")


def _require_parent_owner(card: dict, by: str, action: str) -> None:
    """Parent-owner verbs need an In Progress card (state) owned by the caller (owned)."""
    owner = card.get("activeOwner")
    if card.get("column") != "inprogress" or not owner:
        raise WorkflowError(f"{action} needs {fmt_ref(card)} In Progress (it is {card.get('column')}); "
                            "start or resume it first", 409, "state")
    if owner != by:
        raise WorkflowError(f"{action} is for {fmt_ref(card)}'s owner, {owner}; ask {owner}", 409, "owned")


def guard_subtask_replacement(current, incoming, by: str) -> None:
    """Generic replacements may edit checklists but never forge, change, or drop delegated state."""
    by = validate_actor(by)
    incoming_rows = [st for st in incoming or [] if isinstance(st, dict)]
    incoming_ids = [st.get("id") for st in incoming_rows]
    if len(incoming_ids) != len(set(incoming_ids)):
        raise WorkflowError("replacement subtasks must have unique IDs")
    incoming_by_id = {st.get("id"): st for st in incoming_rows}
    current_ids = {st.get("id") for st in current or [] if isinstance(st, dict)}
    for replacement in incoming_rows:
        if replacement.get("id") not in current_ids and "delegation" in replacement:
            raise WorkflowError("new delegated work must use the subtask lifecycle action", 409, "state")
    for existing in current or []:
        delegation = existing.get("delegation") or {}
        replacement = incoming_by_id.get(existing.get("id"))
        if not delegation:
            if replacement and "delegation" in replacement:
                raise WorkflowError("delegate checklist work through its lifecycle action", 409, "state")
            continue
        held = (delegation.get("state") in ("claimed", "completed")
                and delegation.get("owner") != by)
        if replacement is None:
            if held:
                raise WorkflowError("cannot remove another worker's delegated subtask", 409, "owned")
            continue
        if any(key in replacement and replacement.get(key) != existing.get(key)
               for key in ("done", "doneAt", "doneBy", "delegation")):
            raise WorkflowError("delegated state must change through its lifecycle action", 409, "state")
        if held and replacement.get("text", existing.get("text")) != existing.get("text"):
            raise WorkflowError("cannot replace another worker's delegated subtask", 409, "owned")


def guard_done_writeup(card: dict, writeup) -> None:
    """Done work keeps its write-up: a generic edit may change it but not blank it (as done refuses)."""
    if card.get("column") == "done" and writeup != card.get("writeup"):
        _required_text(writeup, "completion writeup")


def _subtask_index(card: dict) -> dict:
    rows = list(iter_subtasks(card.get("subtasks") or []))
    index = {st["id"]: (st, parent) for st, parent in rows}
    if len(index) != len(rows):
        raise WorkflowError("ambiguous duplicate subtask IDs; repair the checklist before acting")
    return index


def _scope_overlaps(left: str, right: str) -> bool:
    left, right = left.casefold(), right.casefold()
    if left == "." or right == ".":
        return True
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _claim_row(card: dict, item: dict) -> dict:
    """Who holds one claimed delegated subtask, and since when it last showed activity."""
    delegation = item["delegation"]
    claimed = delegation.get("claimedAt")
    activity = [entry["at"] for entry in card.get("log") or [] if entry.get("subtaskId") == item["id"]]
    return {"cardId": card["id"], "num": card["num"], "subtaskId": item["id"], "owner": delegation.get("owner"),
            "state": delegation["state"], "claimedAt": claimed,
            "lastActivityAt": max(filter(None, [claimed, *activity]), default=None)}


def scope_conflicts(doc: dict, card: dict, subtask_id: str) -> list[dict]:
    if not any(candidate is card for candidate in doc.get("cards") or []):
        raise WorkflowError("scope conflicts must use the current board card", 409)
    index = _subtask_index(card)
    if subtask_id not in index or not index[subtask_id][0].get("delegation"):
        raise WorkflowError("scope conflicts require delegated work", 409, "state")
    target_scope = index[subtask_id][0]["delegation"].get("scope") or []
    rows = []
    for other_card in doc.get("cards") or []:
        if other_card.get("column") == "done":
            continue
        for item, _ in iter_subtasks(other_card.get("subtasks") or []):
            sid = item["id"]
            if other_card is card and sid == subtask_id:
                continue
            delegation = item.get("delegation") or {}
            if delegation.get("state") != "claimed":
                continue
            overlaps = [(left, right) for left in target_scope for right in delegation.get("scope") or []
                        if _scope_overlaps(left, right)]
            if overlaps:
                rows.append({**_claim_row(other_card, item),
                             "overlaps": [{"scope": left, "otherScope": right} for left, right in overlaps]})
    return rows


def card_holder(card: dict) -> str | None:
    """Active owner, or for Blocked work (blocking releases ownership) the actor who blocked it."""
    if card.get("activeOwner"):
        return card["activeOwner"]
    if card.get("column") == "blocked":
        blocked = [entry for entry in card.get("history") or [] if entry.get("ev") == "blocked"]
        return blocked[-1].get("by") if blocked else None
    return None


def inbox(doc: dict, by: str, ref=None) -> list[dict]:
    by = validate_actor(by)
    cards = [resolve_ref(doc, ref)] if ref is not None else doc.get("cards") or []
    rows = []
    for card in cards:
        main = card.get("column") in ("inprogress", "blocked") and card_holder(card) == by
        # A tolerant walk: legacy checklists with duplicate IDs must not break the inbox.
        for item, _ in iter_subtasks(card.get("subtasks") or []):
            delegation = item.get("delegation")
            if not delegation:
                continue
            sid, review, state, owner = item["id"], delegation["review"], delegation["state"], delegation["owner"]
            # Every unaccepted result reaches the parent owner; subtask.accept or the card's done accepts it.
            if main and state == "completed" and owner != by and review["state"] != "accepted":
                rows.append({"kind": "review", "num": card["num"], "cardId": card["id"],
                             "title": card["title"], "text": item["text"], "result": delegation["result"],
                             "subtaskId": sid, "owner": owner, "review": review})
            if main and state == "claimed" and owner != by:
                rows.append({"kind": "claim", **_claim_row(card, item)})
            if owner == by and review["state"] == "changes_requested":
                rows.append({"kind": "changes_requested", "num": card["num"], "cardId": card["id"],
                             "subtaskId": sid, "review": review})
    return rows


def publish_subtask_note(doc: dict, card: dict, subtask_id: str, summary, body, by: str) -> dict:
    """Append a note about one subtask. Anyone may note a step nobody holds (a plain step, or available
    delegated work); a held one takes notes from its worker (claimed or completed, so rechecks after
    review need no reopen) or the card's owner."""
    by = validate_actor(by)
    subtask_id = _required_text(subtask_id, "subtask ID")
    fit_note(summary, body)  # Input errors first, so one round trip reports them.
    if not any(candidate is card for candidate in doc.get("cards") or []):
        raise WorkflowError("subtask note must use the current board card", 409)
    # No column gate: the card's owner may note any subtask, and a holder may recheck after Done or Blocked.
    item = _subtask_index(card).get(subtask_id)
    if item is None:
        raise WorkflowError(f"subtask '{subtask_id}' not found", 404)
    delegation = item[0].get("delegation")
    holder = delegation["owner"] if delegation and delegation["state"] != "available" else None
    if holder not in (None, by) and card.get("activeOwner") != by:
        raise WorkflowError(f"{subtask_id} is held by {holder}; only {holder} or the card's owner can post notes "
                            "on it; post it without subtask as a card note", 409, "owned")
    entry = append_note(card, summary, body, by)
    entry["subtaskId"] = subtask_id
    return entry


def subtask_action(doc: dict, card: dict, action: str, details: dict, by: str) -> list[dict]:
    """Apply a delegated-subtask lifecycle action under the caller's board lock.

    The returned list is the selected subtask when an action was idempotent, or the
    changed subtask otherwise.  The caller persists the containing document.
    """
    by = validate_actor(by)
    if not isinstance(details, dict):
        raise WorkflowError("subtask action details must be an object")
    if not any(candidate is card for candidate in doc.get("cards") or []):
        raise WorkflowError("subtask action must use the current board card", 409)
    if action not in ("add", "rm", "delegate", "configure", "claim", "takeover", "done", "undone", "accept",
                      "request-changes"):
        raise WorkflowError(f"unsupported subtask action '{action}'")
    index = _subtask_index(card)
    existing = {sid: row[0] for sid, row in index.items()}
    ids = details.get("ids", details.get("id"))
    if isinstance(ids, str):
        ids = [ids]
    if action == "add":
        if type(details.get("delegated", False)) is not bool:
            raise WorkflowError("subtask delegated must be a boolean")
        texts = details.get("texts")
        if not isinstance(texts, list) or not texts:
            raise WorkflowError("subtask add requires text")
        texts = [_required_text(text, "subtask text") for text in texts]
        if details.get("delegated"):
            guard_card_edit(card, by)
        destination = card.setdefault("subtasks", [])
        number, selected = len(existing) + 1, []
        for text in texts:
            while f"s-{number}" in existing:
                number += 1
            item = {"id": f"s-{number}", "text": text, "done": False, "createdAt": now_iso(),
                    "doneAt": None, "by": by}
            if details.get("delegated"):
                item["delegation"] = {"state": "available", "owner": None,
                                      "claimedAt": None, "result": "", "scope": [],
                                      "review": {"state": "pending", "by": None,
                                                 "at": None, "reason": ""}}
            destination.append(item)
            existing[item["id"]] = item
            selected.append(item)
            number += 1
        hist(card, "subtask-add", by=by, note=", ".join(item["id"] for item in selected))
        touch(card)
        return selected
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item for item in ids):
        raise WorkflowError("subtask action requires subtask IDs")
    ids = list(dict.fromkeys(ids))
    missing = next((item for item in ids if item not in existing), None)
    if missing is not None:
        raise WorkflowError(f"no subtask {missing} on {fmt_ref(card)}", 404, "not_found")
    selected_items = [existing[item] for item in ids]
    if action == "rm":
        if any(st.get("delegation") for st in selected_items):
            guard_card_edit(card, by)
        held = next((st for st in selected_items if (st.get("delegation") or {}).get("state") in
                     ("claimed", "completed") and st["delegation"]["owner"] != by), None)
        if held is not None:
            raise WorkflowError(f"cannot remove {held['id']}: it is held by {held['delegation']['owner']}",
                                409, "owned")
        remove_ids = set(ids)
        card["subtasks"] = [item for item in card["subtasks"] if item["id"] not in remove_ids]
        hist(card, "subtask-rm", by=by, note=", ".join(ids))
        touch(card)
        return selected_items
    if action in ("done", "undone") and not any(item.get("delegation") for item in selected_items):
        if details.get("result") is not None:
            raise WorkflowError(f"result applies only to delegated subtasks, not plain checklist steps "
                                f"({', '.join(ids)}); tick without result and post evidence with note and subtask")
        # Legacy checklist ticks are plain field edits: any actor, atomic for a batch.
        done = action == "done"
        changed = [item for item in selected_items if item["done"] != done]
        for item in changed:
            item["done"], item["doneAt"] = done, now_iso() if done else None
            if done:
                item["doneBy"] = by
            else:
                item.pop("doneBy", None)
        if changed:
            hist(card, f"subtask-{action}", by=by, note=", ".join(item["id"] for item in changed))
            touch(card)
        return selected_items
    if action == "accept" and len(selected_items) > 1:  # One revision; any refusal saves nothing.
        return [accepted for sid in ids
                for accepted in subtask_action(doc, card, action, {**details, "ids": [sid]}, by)]
    if len(selected_items) != 1:
        delegated = [item["id"] for item in selected_items if item.get("delegation")]
        raise WorkflowError(f"subtask {action} takes one ID per request"
                            + (f" for delegated work ({', '.join(delegated)})" if delegated else "")
                            + "; send one request per subtask")
    selected = selected_items[0]
    reason = (_required_text(details.get("reason"), f"subtask {action} reason")
              if action in ("takeover", "request-changes") else None)
    result = _required_text(details.get("result"), "subtask result") if action == "done" else None
    if action == "configure" and "reviewRequired" in details:
        raise WorkflowError("reviewRequired was removed: every delegated result is accepted, by subtask.accept "
                            "or by the card's done; drop the field")
    if action == "configure" and "dependsOn" in details:
        raise WorkflowError("subtask prerequisites (dependsOn) were removed; drop the field")
    delegation = selected.get("delegation")
    if action == "delegate":
        guard_card_edit(card, by)
        if selected.get("done"):
            raise WorkflowError("only open checklist work can be delegated", 409, "state")
        if delegation is not None:
            return [selected]
        selected["delegation"] = {"state": "available", "owner": None,
                                  "claimedAt": None, "result": "", "scope": [],
                                  "review": {"state": "pending", "by": None,
                                             "at": None, "reason": ""}}
    else:
        if delegation is None:
            raise WorkflowError(f"{selected['id']} is a plain checklist item; delegate it first", 409, "state")
        if action in ("configure", "accept", "request-changes"):
            _require_parent_owner(card, by, action)
        if action in ("claim", "done", "undone") and (
                card.get("column") != "inprogress" or not card.get("activeOwner")):
            later = ("wait for the card's owner to start or resume it, then claim" if action == "claim" else
                     f"post the result with note and subtask {selected['id']}, then wait for the card's owner "
                     "to start or resume it")
            raise WorkflowError(f"{action} needs {fmt_ref(card)} In Progress (it is {card.get('column')}); {later}",
                                409, "state")
        state, owner = delegation["state"], delegation["owner"]
        if action == "configure":
            # The parent owner may re-scope running work; claim, owner, result and review stay untouched.
            scope_only = state == "claimed" and "scope" in details
            if state != "available" and not scope_only:
                raise WorkflowError("configure requires available delegated work; claimed work "
                                    "accepts only a scope change", 409, "state")
            scope = _norm_scope(details.get("scope", delegation["scope"]))
            if scope_only:  # Held work keeps every saved path its worker may be editing.
                folded = [path.casefold() for path in scope]
                dropped = [path for path in delegation["scope"] if not any(
                    new in (".", path.casefold()) or path.casefold().startswith(new + "/") for new in folded)]
                if dropped:
                    raise WorkflowError(f"{selected['id']} is {state} by {owner}, who may be editing "
                                        f"{', '.join(dropped)}; send the saved paths plus the new ones", 409, "state")
            delegation["scope"] = scope
            if not scope_only:
                delegation["review"] = {"state": "pending", "by": None,
                                        "at": None, "reason": ""}
        elif action == "accept":
            if state != "completed":
                raise WorkflowError("accept requires completed delegated work: "
                                    + _unfinished_delegation(selected, by), 409, "state")
            if delegation["review"]["state"] == "accepted":
                return [selected]
            _accept_result(card, selected, by)
        elif action == "request-changes":
            if state != "completed":
                raise WorkflowError(f"{selected['id']}: request-changes requires completed delegated work "
                                    f"(it is {state})", 409, "state")
            selected["done"], selected["doneAt"] = False, None
            selected.pop("doneBy", None)
            delegation.update(state="claimed", result="")
            delegation["review"].update(state="changes_requested", by=by, at=now_iso(), reason=reason)
            entry = append_note(card, f"changes requested {selected['id']}", reason, by)
            entry.update(kind="generated", subtaskId=selected["id"])
        elif action == "claim":
            if state == "claimed" and owner == by:
                return [selected]
            if owner is not None and owner != by:
                raise WorkflowError(f"{selected['id']} is held by {owner}", 409, "owned")
            if state != "available":
                raise WorkflowError(f"{selected['id']} is {state}, not available", 409, "state")
            delegation.update(state="claimed", owner=by, claimedAt=now_iso(), result="")
        elif action == "takeover":
            if state != "claimed":
                raise WorkflowError("takeover requires claimed delegated work", 409, "state")
            if owner == by:
                return [selected]
            reason = f"{owner} → {by}: {reason}"
            entry = append_note(card, f"takeover {selected['id']}", reason, by)
            entry.update(kind="generated", subtaskId=selected["id"])
            delegation.update(owner=by, claimedAt=now_iso())
        else:  # Claimant verbs: unclaimed (state), someone else's (owned), then the item's state (state).
            if owner is None and action == "done" and state == "available" and by == card.get("activeOwner"):
                owner, state = by, "claimed"  # The card's owner finishes an available step itself.
            if owner is None:
                raise WorkflowError(f"{selected['id']} is not claimed; subtask.claim it first", 409, "state")
            if owner != by:
                raise WorkflowError(f"{selected['id']} is held by {owner}; only its claimant can run '{action}'; "
                                    "to replace a worker that is gone, use subtask.takeover with a reason",
                                    409, "owned")
            expected = {"done": ("claimed", "completed"), "undone": ("completed",)}[action]
            if state not in expected:
                raise WorkflowError(f"{action} is not valid while {selected['id']} is {state}", 409, "state")
            if action == "done":
                if state == "completed":
                    if result == delegation["result"]:
                        return [selected]
                    if delegation["review"]["state"] == "accepted":
                        raise WorkflowError(f"{selected['id']} was accepted by {delegation['review']['by']}; post "
                                            f"later evidence with note and subtask {selected['id']}", 409, "state")
                    raise WorkflowError(f"{selected['id']} is already completed with a different result; "
                                        "reopen it with undone first", 409, "state")
                if delegation["owner"] is None:
                    delegation.update(owner=by, claimedAt=now_iso())
                selected["done"], selected["doneAt"], selected["doneBy"] = True, now_iso(), by
                delegation.update(state="completed", result=result)
                delegation["review"].update(state="pending", by=None, at=None, reason="")
                entry = append_note(card, f"result {selected['id']}", result, by)
                entry.update(kind="generated", subtaskId=selected["id"])
                if by == card.get("activeOwner"):  # Nobody else reviews the card owner's own result.
                    _accept_result(card, selected, by)
            else:  # undone
                if delegation["review"]["state"] == "accepted":
                    raise WorkflowError(f"{selected['id']} was accepted by {delegation['review']['by']}; the card's "
                                        "owner reopens it with subtask.request-changes, or post later evidence "
                                        f"with note and subtask {selected['id']}", 409, "state")
                previous = delegation["result"]
                selected["done"], selected["doneAt"] = False, None
                selected.pop("doneBy", None)
                delegation.update(state="claimed", result="")
                delegation["review"].update(state="pending", by=None, at=None, reason="")
                entry = append_note(card, f"reopened {selected['id']}", f"Prior result: {previous}", by)
                entry.update(kind="generated", subtaskId=selected["id"])
                reason = f"reopened; prior result: {previous}"
    note = selected["id"] if reason is None else f"{selected['id']}: {reason}"
    hist(card, f"subtask-{action}", by=by, note=note)
    touch(card)
    return [selected]


def _accept_result(card: dict, item: dict, by: str) -> None:
    """Accept one completed delegated result: its review and the generated `accepted` note."""
    item["delegation"]["review"].update(state="accepted", by=by, at=now_iso(), reason="")
    entry = append_note(card, f"accepted {item['id']}", "", by)
    entry.update(kind="generated", subtaskId=item["id"])


def _unfinished_delegation(item: dict, by: str) -> str:
    """One unfinished delegated subtask that keeps work open, and the op that clears it."""
    delegation = item["delegation"]
    state, owner = delegation["state"], delegation["owner"]
    if state == "available":
        return f"{item['id']} is available (finish it yourself with subtask.done and a result, or subtask.rm it)"
    return (f"{item['id']} is {state}" + (f" by {owner}" if owner else "")
            + (f" (subtask.takeover with a reason if {owner} is gone, then subtask.done)"
               if owner and owner != by else ""))


def unaccepted_delegations(card: dict) -> list[dict]:
    """Delegated subtasks not accepted yet: unfinished (they keep the card from Done), or completed and
    awaiting review (done accepts those)."""
    return [st for st, _ in iter_subtasks(card.get("subtasks") or [])
            if (delegation := st.get("delegation")) and (
                delegation["state"] != "completed" or delegation["review"]["state"] != "accepted")]


def stage_attention(card: dict, stale_hours: int = 24) -> str | None:
    if card.get("column") != "inprogress":
        return None
    subtasks = [subtask for subtask, _ in iter_subtasks(card.get("subtasks") or [])]
    if subtasks and all(subtask.get("done") for subtask in subtasks):
        return "REVIEW PENDING" if unaccepted_delegations(card) else "READY TO CLOSE"
    try:
        changed = datetime.datetime.fromisoformat(
            str(card.get("updatedAt") or card.get("createdAt") or "").replace("Z", "+00:00")
        )
        age = datetime.datetime.now(datetime.timezone.utc) - changed
    except (TypeError, ValueError):
        return None
    return "REVIEW STATE" if age.total_seconds() >= stale_hours * 3600 else None


def _atomic_write(path: Path, data: bytes) -> None:
    import tempfile
    path = require_write_scope(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value) -> None:
    _atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"))


def archive_removed_cards(board_path: Path, cards: list[dict], reason: str) -> Path | None:
    if not cards:
        return None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(board_path).parent / ARCHIVE_DIR / f"{slugify(reason)}-{stamp}-{_uuid_hex()}.json"
    _atomic_write_json(path, {
        "archivedFrom": str(Path(board_path)),
        "reason": reason,
        "cards": cards,
    })
    return path


# ===== registry and board lifecycle =====

BOARD_DIR_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_DELETED_STAMP = re.compile(r"-\d{8}T\d{6}Z\Z")


class RegistryNotFound(Exception):
    pass


class RegistryConflict(Exception):
    pass


def _registry_json(path: Path, max_bytes=None):
    # Missing optional metadata is first-use, not a transient missing board swap.
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("registry byte limit must be a nonnegative integer")
    with open_shared(path, "rb") as stream:
        data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise WorkflowError(f"registry exceeds {max_bytes} bytes: {path}")
    try:
        return json.loads(data)
    except ValueError as exc:
        raise WorkflowError(f"invalid board registry {path}: {exc}") from exc


def registry_load(path=None, *, max_bytes=None) -> dict:
    """{"version": 2, "boards": {name: {"dir", "project"}}}; a missing file is an empty registry."""
    path = Path(path) if path is not None else registry_path()
    try:
        reg = _registry_json(path, max_bytes)
    except FileNotFoundError:
        if path.is_symlink():
            raise
        return {"version": 2, "boards": {}}
    boards = reg.get("boards") if isinstance(reg, dict) else None
    if not isinstance(boards, dict) or type(reg.get("version")) is not int or reg["version"] != 2:
        raise WorkflowError("boards.json uses an unsupported format")
    dirs, projects = set(), set()
    for name, entry in boards.items():
        if (not isinstance(entry, dict) or not isinstance(entry.get("dir"), str)
                or not BOARD_DIR_PATTERN.fullmatch(entry["dir"])
                or not isinstance(entry.get("project"), str) or not Path(entry["project"]).is_absolute()):
            raise WorkflowError(f"invalid board registry {path}: entry {name!r} needs a safe dir "
                                "and an absolute project")
        key = os.path.normcase(entry["project"])  # Lexical: validity never depends on the live filesystem.
        if entry["dir"] in dirs or key in projects:
            raise WorkflowError(f"invalid board registry {path}: {name!r} repeats another board's dir or project")
        dirs.add(entry["dir"])
        projects.add(key)
    return reg


@contextlib.contextmanager
def _registry_locked():
    """Yield the current registry under its lock, which is always taken before any board lock."""
    registry_load()  # Reject a malformed registry before creating the lock file.
    require_write_scope(registry_path())
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    with board_lock(registry_path()):
        yield registry_load()


def _has_reparse_point(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        # Root-owned POSIX links are system layout (macOS /var and /tmp, /home on Fedora Atomic):
        # only root can plant one, so it cannot redirect a user's writes. Windows has no uid.
        return _IS_WINDOWS or st.st_uid != 0
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _require_unlinked(boards: dict, name: str, project: Path) -> None:
    key = _project_key(project)
    for other, entry in boards.items():
        if other != name and _project_key(entry["project"]) == key:
            raise WorkflowError(f"{project} is already linked to board {other!r}; if that project moved, "
                                f"run `workboard link {other}` in its new folder", 409, "state")


def _require_new_board(boards: dict, name: str, project: Path) -> None:
    clash = next((other for other in boards if other.casefold() == name.casefold()), None)
    if clash is not None:
        raise WorkflowError(f"a board named {clash!r} already exists"
                            + ("" if clash == name else "; board names ignore case"), 409, "state")
    _require_unlinked(boards, name, project)


def _free_board_dir(boards: dict, name: str) -> str:
    """slugify(name), then -2, -3… past dirs in the registry, boards/ or deleted/<dir>-<stamp>/."""
    taken = {entry["dir"] for entry in boards.values()}
    for parent in (boards_dir(), deleted_dir()):
        if parent.is_dir():
            for child in parent.iterdir():
                taken.update((child.name.lower(), _DELETED_STAMP.sub("", child.name)))
    base = slugify(name, 48, fallback="board")
    folder, n = base, 1
    while folder in taken:
        n += 1
        folder = f"{base}-{n}"
    return folder


def create_board(name: str, project, doc: dict | None = None, *, populate=None) -> Path:
    """Create and register the board for `project`; a failure leaves no half-created board.

    `doc` (default: an empty five-column board) is saved like save(): its rev becomes rev + 1.
    `populate(folder)` may then add files to the still-unregistered board folder."""
    import shutil
    name = _validate_board_name(name)
    project = Path(project).resolve()
    if doc is None:
        doc = {"name": name, "rev": 0, "nextNum": 1,
               "columns": [dict(c) for c in DEFAULT_COLUMNS], "cards": []}
    with _registry_locked() as reg:
        boards = reg["boards"]
        _require_new_board(boards, name, project)
        folder = boards_dir() / _free_board_dir(boards, name)
        require_write_scope(folder)
        folder.mkdir(parents=True)
        try:
            path = folder / "board.json"
            save(path, doc)
            if populate is not None:
                populate(folder)
            boards[name] = {"dir": folder.name, "project": str(project)}
            _atomic_write_json(registry_path(), reg)
        except BaseException:
            # A registry replace can commit before its error surfaces: never delete a registered board.
            try:
                registered = any(entry["dir"] == folder.name for entry in registry_load()["boards"].values())
            except BaseException:
                registered = True
            if not registered:
                shutil.rmtree(folder, ignore_errors=True)
            raise
    return path


def link_board(name: str, project) -> Path:
    """Link board `name` to `project` (after a move or rename); returns its board.json."""
    project = Path(project).resolve()
    board_file(name)  # Unknown names fail before any lock file exists.
    with _registry_locked() as reg:
        entry = reg["boards"].get(name)
        if entry is None:
            raise WorkflowError(f"no board named {name!r}", 404, "not_found")
        _require_unlinked(reg["boards"], name, project)
        entry["project"] = str(project)
        _atomic_write_json(registry_path(), reg)
    return _board_path(entry)


def _registered_target(reg: dict, name: str, expected_board: str) -> Path:
    entry = reg["boards"].get(name)
    if entry is None:
        raise RegistryNotFound(name)
    target = _board_path(entry)
    if expected_board != str(target):
        raise RegistryConflict("registered board identity changed")
    return target


def delete_registered_board(name: str, expected_board: str,
                            base_rev: int | None) -> dict:
    """Recoverably delete one board: its folder moves to deleted/<dir>-<stamp>/ (registry lock, then the
    board's write transaction)."""
    target = _registered_target(registry_load(), name, expected_board)
    if target.exists():
        board_rev(target)  # Reject unsafe data (a legacy board migrates) before taking locks.
    with _registry_locked() as reg:
        target = _registered_target(reg, name, expected_board)
        folder = target.parent
        registered = {**reg, "boards": dict(reg["boards"])}
        del reg["boards"][name]
        if not folder.exists():
            if base_rev is not None:
                raise RegistryConflict("registered board is missing")
            _atomic_write_json(registry_path(), reg)
            return {"recoveryPath": None, "board": str(target)}
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recovery = deleted_dir() / f"{folder.name}-{stamp}"
        require_write_scope(recovery)
        present = target.exists()
        if present and base_rev is None:
            raise RegistryConflict("registered board exists")
        if not present and base_rev is not None:
            raise RegistryConflict("registered board is missing")
        # Writers wait on the store while the stub leaves; after that, opening the board finds nothing.
        with (board_transaction(target, scope="card") if present else contextlib.nullcontext()) as doc:
            if present and doc["rev"] != base_rev:
                raise RegistryConflict("registered board revision changed")
            # Unregister first: a crash before the move leaves an intact, unregistered boards/<dir>/.
            _atomic_write_json(registry_path(), reg)
            try:
                recovery.mkdir(parents=True)
                if present:  # The reviewed revision leaves the live path inside the transaction.
                    atomic_replace(target, recovery / "board.json")
            except BaseException:
                with contextlib.suppress(OSError):
                    recovery.rmdir()  # Succeeds only if nothing moved.
                _atomic_write_json(registry_path(), registered)
                raise
        # Windows cannot move a file another process holds open, so the rest moves after release. The store
        # goes first: atomic_replace waits out readers, and one that outlasts it gets the board put back.
        try:
            if (folder / DB_NAME).exists():
                atomic_replace(folder / DB_NAME, recovery / DB_NAME)
        except (OSError, WorkflowError) as e:
            if present:
                atomic_replace(recovery / "board.json", target)
            _atomic_write_json(registry_path(), registered)
            with contextlib.suppress(OSError):
                recovery.rmdir()
            raise OSError(f"the board is in use, so nothing was deleted; try again: {e}") from e
        try:
            for child in list(folder.iterdir()):
                if child.name != LOCK_NAME:
                    try:
                        atomic_replace(child, recovery / child.name)
                    except FileNotFoundError:  # The last SQLite connection deletes -wal/-shm as it closes.
                        continue
        except (OSError, WorkflowError) as e:
            raise OSError(f"board moved to {recovery}, but finishing the delete failed: {e}") from e
        with contextlib.suppress(OSError):  # A writer waiting on the lock may still hold it open.
            (folder / LOCK_NAME).unlink(missing_ok=True)  # stores made by 0.4 have no lock file
            folder.rmdir()
        return {"recoveryPath": str(recovery), "board": str(target)}


# ===== output =====

def fmt_ref(card: dict) -> str:
    return f"#{card['num']}"


def ok(line: str) -> None:
    print(line)
