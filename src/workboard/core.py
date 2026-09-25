#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""WorkBoard core: schema, board discovery, registry, crash-safe persistence.

One board/board.json per project is the single source of truth; the per-user
registry (home()/boards.json) lists the boards the local server serves.
Writes serialize on <board_dir>/.board.lock (msvcrt/fcntl), commit through an
fsync'd temp file swapped in atomically (ReplaceFileW on Windows, os.replace
elsewhere), and snapshot every committed rev to .backups/ (newest 10 kept).
Nothing here starts a server or opens a browser.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import mimetypes
import json
import os
import re
import sys
import tempfile
import time
import stat
import threading
import uuid
from pathlib import Path

from . import __version__

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, SCHEMA_VERSION)
API_VERSION = 2
CAPABILITIES = ("context", "attachment-cli", "shared-attachments", "expected-rev",
                "schema-guard", "scoped-writes")
BACKUP_KEEP = 10
HISTORY_CAP = 40
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


def slugify(text: str, max_len: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "card"


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
            message = f"card #{card['num']} changed at rev {card['changedRev']} after reviewed rev {reviewed}"
        super().__init__(message, 409, "stale")
        self.rev = rev
        self.card = None if card is None else {
            "num": card["num"], "id": card["id"], "changedRev": card["changedRev"],
            "last": (card.get("history") or [None])[-1]}


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
    canonical = raw.resolve(strict=False)
    scope = os.environ.get("WORKBOARD_SCOPE_ROOT")
    if scope and not canonical.is_relative_to(Path(scope).resolve(strict=False)):
        raise WorkflowError(f"write outside WORKBOARD_SCOPE_ROOT is forbidden: {raw}", 403)
    return canonical


def runtime_info() -> dict:
    return {"version": __version__, "apiVersion": API_VERSION,
            "schemaVersion": SCHEMA_VERSION, "supportedSchemaVersions": list(SUPPORTED_SCHEMA_VERSIONS),
            "capabilities": list(CAPABILITIES)}

def validate_actor(value) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 80
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise WorkflowError("actor must be a nonempty label of at most 80 characters")
    return value.strip()


def actor() -> str:
    """CLI writer label: agents use the CLI, so unlabeled writes are `agent` (the browser records `user`)."""
    return validate_actor(os.environ.get("WORKBOARD_ACTOR", "agent"))


# ===== board discovery =====

def find_board(explicit: str | None = None) -> Path:
    explicit = explicit or os.environ.get("WORKBOARD_DEFAULT_BOARD")
    if explicit:
        p = Path(explicit).absolute()
        if p.is_dir():
            p = p / "board" / "board.json"
        if not p.is_file():
            raise FileNotFoundError(f"no board at {p}")
        return p
    cur = Path.cwd().resolve()
    while True:
        c = cur / "board" / "board.json"
        if c.is_file():
            return c
        if cur.parent == cur:
            break
        cur = cur.parent
    raise FileNotFoundError(
        "no board found at/above cwd; run inside the project or pass --board /path/to/project")


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


if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _ReplaceFileW = _kernel32.ReplaceFileW
    _ReplaceFileW.restype = wintypes.BOOL
    _ReplaceFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.LPVOID, wintypes.LPVOID,
    ]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.restype = wintypes.BOOL
    _CloseHandle.argtypes = [wintypes.HANDLE]

    try:
        import fcntl  # type: ignore
        _HAVE_FCNTL = True
    except ImportError:
        _HAVE_FCNTL = False
    import msvcrt

    def _win_share_delete_opener(path, flags):
        handle = _CreateFileW(
            os.fspath(path), _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _CloseHandle(handle)
            raise

    def _win_replace_file(src, dst):
        if not _ReplaceFileW(dst, src, None, 0, None, None):
            code = ctypes.get_last_error()
            if code in _PARTIAL_REPLACE_WINERRORS:
                raise ReplaceFilePartialFailure(code, src, dst)
            raise ctypes.WinError(code)
else:
    try:
        import fcntl  # type: ignore
        _HAVE_FCNTL = True
    except ImportError:
        _HAVE_FCNTL = False


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


def _read_shared(path, mode, encoding=None):
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
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
            if not transient or time.monotonic() >= deadline:
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
    """Exclusive cross-process lock on <board_dir>/.board.lock.

    Reentrant within the owning thread (a verb holds it across save(), which
    re-acquires). Policy: wait up to `timeout`, then RAISE — no writer ever
    proceeds unlocked. A loud failure beats a silent lost update.
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
        raise WorkflowError(f"unsupported board schemaVersion {version!r}; supported: "
                            + ", ".join(map(str, SUPPORTED_SCHEMA_VERSIONS)), 409)
    return version


def _validate_aliases(raw: dict) -> None:
    if not isinstance(raw, dict):
        raise WorkflowError("card must be an object")
    for canonical, alias in (("links", "linkedCards"), ("cycles", "lifecycleCycles")):
        if canonical in raw and alias in raw and (
                type(raw[canonical]) is not type(raw[alias])
                or json.dumps(raw[canonical], sort_keys=True) != json.dumps(raw[alias], sort_keys=True)):
            raise WorkflowError(f"conflicting legacy aliases: {canonical}/{alias}", 409)


@contextlib.contextmanager
def board_transaction(board_path, expected_rev=None, ref=None):
    board_path = require_write_scope(board_path)
    load(board_path)  # Reject unsafe schemas/aliases before creating the lock file.
    with board_lock(board_path):
        doc = load(board_path)
        check_revision(doc, expected_rev, ref)
        yield doc


def _norm_subtask(st: dict) -> dict:
    if not isinstance(st, dict) or (st.get("children") is not None and not isinstance(st["children"], list)):
        raise WorkflowError("subtasks must be objects with an array of children")
    return {
        **st,
        "id": str(st.get("id", "")),
        "text": str(st.get("text", "")),
        "done": bool(st.get("done", False)),
        "createdAt": st.get("createdAt"),
        "doneAt": st.get("doneAt"),
        "collapsed": bool(st.get("collapsed", False)),
        "children": [_norm_subtask(c) for c in (st.get("children") or [])],
    }


_LEGACY_NOTE_STAMP = re.compile(r"^\[(?P<date>\d{4}-\d{2}-\d{2})(?: (?P<by>[^\]]{1,80}))?\] ?(?P<text>.*)$")


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


def derive_summary(text: str) -> tuple[str, str]:
    """Split one legacy note's text into a one-line summary and a markdown body."""
    lines = text.strip().splitlines()
    if not lines:
        return "(empty note)", ""
    first = lines[0].rstrip()
    end = re.search(r"[.;!?] ", first)
    if end and end.start() < NOTE_SUMMARY_MAX:
        summary = first[:end.start() + 1].removesuffix(";").rstrip()
        if summary:
            return summary, "\n".join([first[end.start() + 1:], *lines[1:]]).strip()
    if len(first) <= NOTE_SUMMARY_MAX:
        return first, "\n".join(lines[1:]).strip()
    cut = re.match(r"(.*\S)\s", first[:NOTE_SUMMARY_MAX - 1])
    return (cut[1] if cut else first[:NOTE_SUMMARY_MAX - 1]) + "…", "\n".join(lines).strip()


def split_legacy_notes(card_id: str, text: str) -> tuple[str, list[dict]]:
    """Move v1/v2 `[date actor] text` note lines into log entries; other text stays pinned."""
    segments = []   # (stamp match or None for free-form text, lines)
    for line in text.splitlines():
        stamp = _LEGACY_NOTE_STAMP.match(line)
        if stamp:
            segments.append((stamp, [stamp["text"]]))
        elif not segments or (segments[-1][0] and re.match(" *#", line)):
            segments.append((None, [line]))
        else:
            segments[-1][1].append(line)
    notes, log = [], []
    for stamp, lines in segments:
        if stamp is not None:
            summary, body = derive_summary("\n".join(lines))
            if len(body) <= NOTE_BODY_MAX:
                digest = hashlib.sha256(f"{card_id}\n{len(log)}\n{stamp.string}".encode("utf-8")).hexdigest()
                log.append({"id": digest[:32], "at": stamp["date"], "by": (stamp["by"] or "").strip() or None,
                            "summary": summary, "body": body})
                continue
            lines = [stamp.string, *lines[1:]]   # too large for one entry: keep it verbatim as pinned text
        notes.append("\n".join(lines).strip())
    return "\n\n".join(part for part in notes if part), log


def normalize_card(raw: dict) -> dict:
    _validate_aliases(raw)
    for key in ("tags", "subtasks", "links", "linkedCards", "history", "cycles", "lifecycleCycles",
                "dependsOn", "comments", "attachments", "verification", "reviews"):
        if raw.get(key) is not None and not isinstance(raw[key], list):
            raise WorkflowError(f"card {key} must be an array")
    for key in ("comments", "attachments"):
        if any(not isinstance(item, dict) for item in (raw.get(key) or [])):
            raise WorkflowError(f"card {key} entries must be objects; refusing data loss")
    card = {
        **{key: value for key, value in raw.items()
           if key not in ("linkedCards", "lifecycleCycles")},
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
        "subtasks": [_norm_subtask(s) for s in (raw.get("subtasks") or [])],
        "links": list(dict.fromkeys(str(l) for l in
                                   (raw.get("links", raw.get("linkedCards")) or []))),
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
        "comments": [dict(c) for c in (raw.get("comments") or []) if isinstance(c, dict)],
        "attachments": [dict(a) for a in (raw.get("attachments") or []) if isinstance(a, dict)],
        "verification": list(raw.get("verification") or []),
        "reviews": list(raw.get("reviews") or []),
        "changedRev": raw["changedRev"] if type(raw.get("changedRev")) is int and raw["changedRev"] >= 0 else 0,
        "log": normalize_log(raw.get("log")),
    }
    return card


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
    if version < 3:   # v1/v2 stamped note lines become the v3 timeline; a v3 doc never re-splits
        for card in cards:
            card["notes"], legacy = split_legacy_notes(card["id"], card["notes"])
            card["log"] = legacy + card["log"]
    nums = [c["num"] for c in cards if isinstance(c["num"], int)]
    next_num = raw.get("nextNum")
    if not isinstance(next_num, int) or next_num <= (max(nums) if nums else 0):
        next_num = (max(nums) + 1) if nums else 1
    doc = {
        **raw,
        "schemaVersion": SCHEMA_VERSION,
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


def load(board_path: Path) -> dict:
    data = read_text_shared(board_path)
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as e:
        raise WorkflowError(f"{board_path} is not valid JSON ({e}); restore via workboard recover") from e
    return normalize_doc(raw)


def write_backup(board_path: Path, data: bytes, keep: int = BACKUP_KEEP) -> None:
    require_write_scope(board_path)
    require_write_scope(Path(board_path).parent / BACKUP_DIR)
    try:
        board_path = Path(board_path)
        bdir = board_path.parent / BACKUP_DIR
        bdir.mkdir(exist_ok=True)
        try:
            rev = int(json.loads(data).get("rev", 0))
        except Exception:
            rev = 0
        dest = require_write_scope(bdir / f"board-{rev}.json")
        fd, tmp = tempfile.mkstemp(dir=bdir, prefix=".backup-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        snaps = sorted(bdir.glob("board-*.json"),
                       key=lambda p: _rev_of(p))
        for p in snaps[:-keep]:
            try:
                require_write_scope(p)
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _rev_of(p: Path) -> int:
    try:
        return int(p.stem.split("-", 1)[1])
    except (ValueError, IndexError):
        return -1


def list_backups(board_path: Path):
    bdir = Path(board_path).parent / BACKUP_DIR
    if not bdir.is_dir():
        return []
    snaps = [(_rev_of(p), p) for p in bdir.glob("board-*.json")]
    return sorted([(r, p) for r, p in snaps if r >= 0], key=lambda rp: rp[0], reverse=True)


def _card_content(card: dict) -> str:
    return json.dumps({key: value for key, value in card.items() if key != "changedRev"}, sort_keys=True)


def save(board_path: Path, doc: dict, by: str | None = None) -> int:
    """Commit one revision under the cross-process lock. Returns the new rev."""
    board_path = require_write_scope(board_path)
    normalized = normalize_doc(doc)  # Validate and compare canonical cards without discarding extensions.
    held = getattr(_lock_local, "depths", {}).get(os.path.normcase(str(board_path.parent / LOCK_NAME)))
    if board_path.exists() and not held:
        load(board_path)  # Reject unsafe on-disk data before creating the lock file.
    with board_lock(board_path):
        on_disk = load(board_path)["cards"] if board_path.exists() else []
        doc["schemaVersion"] = SCHEMA_VERSION
        doc["rev"] = int(doc.get("rev") or 0) + 1
        previous = {card["id"]: card for card in on_disk}
        for card, canonical in zip(doc["cards"], normalized["cards"]):
            old = previous.get(canonical["id"])
            if old is None or _card_content(old) != _card_content(canonical):
                card["changedRev"] = doc["rev"]
            else:
                card["changedRev"] = old["changedRev"]  # Never accept a supplied or restored stamp.
        doc["savedAt"] = now_iso()
        doc["savedBy"] = actor() if by is None else validate_actor(by)
        data = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(board_path.parent),
                                   prefix=".board.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp, board_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        write_backup(board_path, data)
    return doc["rev"]


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
    if any(c["id"] == card_id or card_id in (c.get("dependsOn") or [])
           or card_id in (c.get("links") or []) for c in doc["cards"]):
        raise WorkflowError("card ID already exists or is reserved by an existing reference", 409)
    return card_id


def unique_id(doc: dict, base: str) -> str:
    return validate_new_identity(doc, f"{slugify(base)}-{uuid.uuid4().hex}")


def _required_text(value, label: str, limit: int | None = None, code: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be nonempty text", code=code)
    if limit is not None and len(value) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return value.strip()


def _dependencies_satisfied(card: dict, by_id: dict) -> bool:
    return all(dep in by_id and by_id[dep].get("column") == "done"
               and by_id[dep].get("outcome", "completed") == "completed"
               for dep in (card.get("dependsOn") or []))


def _require_start(doc: dict, card: dict, ids: list | None = None) -> None:
    by_id = {c["id"]: c for c in doc["cards"]}
    candidate = card if ids is None else {"dependsOn": ids}
    if not _dependencies_satisfied(candidate, by_id):
        raise WorkflowError("dependencies must exist and be completed, not canceled", 409, "deps")
    column = next((c for c in doc["columns"] if c["id"] == "inprogress"), {})
    limit = column.get("wipLimit")
    if card["column"] != "inprogress" and type(limit) is int and limit > 0:
        active = sum(c["column"] == "inprogress" for c in doc["cards"])
        if active >= limit:
            raise WorkflowError(
                f"In Progress WIP limit is {limit}; finish, block, or pause active work", 409, "wip")


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
                 "rework", "reopen", "bug", "improve", "move", "dependencies", "workpad", "note"}
    if not isinstance(action, str) or action not in supported:
        raise WorkflowError(f"unsupported lifecycle action '{action}'")
    frm = card["column"]
    owner = card.get("activeOwner")
    if action == "workpad":
        notes = card.get("notes") or ""
        missing = [heading for heading in ("Acceptance criteria", "Verification")
                   if not re.search(rf"(?im)^ {{0,3}}#{{1,6}}[ \t]+{heading}"
                                    r"[ \t]*(?:#+[ \t]*)?$", notes)]
        if not missing:
            return card
        card["notes"] = notes + ("\n\n" if notes and not notes.endswith("\n\n") else "")
        card["notes"] += "\n\n".join(f"## {heading}\n" for heading in missing)
        hist(card, "workpad", by=by, note="Added missing notes sections")
        touch(card)
        return card
    if action == "note":   # timeline entries carry no ownership guard, like comments
        append_note(card, details.get("summary"), details.get("body"), by)
        return card
    if action != "takeover" and owner and owner != by:
        raise WorkflowError(f"owned by {owner}; use takeover with a reason", 409, "owned")
    if action == "takeover":
        reason = _required_text(details.get("reason"), "takeover reason")
        if frm != "inprogress":
            raise WorkflowError("takeover requires an In Progress card", 409, "state")
        if owner == by:
            return card
        card["activeOwner"], card["claimedAt"] = by, now_iso()
        hist(card, "takeover", by=by, note=f"{owner or 'unowned'} → {by}: {reason}")
        touch(card)
        return card
    if action == "dependencies":
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
        if frm == "inprogress":
            _require_start(doc, card, ids)
        if ids == (card.get("dependsOn") or []):
            return card
        card["dependsOn"] = ids
        hist(card, "dependencies", by=by, note=", ".join(ids) or "cleared")
        touch(card)
        return card

    destination, note, event = frm, details.get("note"), action
    if note is not None and not isinstance(note, str):
        raise WorkflowError("note must be text")
    if action == "move":
        target = details.get("to")
        if not isinstance(target, str):
            raise WorkflowError("move.to must name a core column")
        destination = slugify(target, 24)
        destination = {"in-progress": "inprogress"}.get(destination, destination)
        if destination not in CORE_COLUMN_ID_SET:
            raise WorkflowError("unknown core column; use " + ", ".join(CORE_COLUMN_IDS))
        if destination == frm and frm != "inprogress":
            return card
        if destination == "blocked":
            raise WorkflowError("use block with a reason and until condition", code="state")
        if frm == "blocked":
            raise WorkflowError("use resume with a resolution note", code="state")
        if frm == "done":
            raise WorkflowError("leaving Done requires reasoned reopen or rework", 409, "state")
        if destination == "done":
            return workflow_action(doc, card, "complete",
                                   {"writeup": details.get("writeup", card.get("writeup"))}, by)
        if destination == "inprogress":
            action = "start"
    if action == "start":
        if frm == "blocked":
            raise WorkflowError("use resume with a resolution note", code="state")
        if frm == "done":
            raise WorkflowError("leaving Done requires reasoned reopen or rework", 409, "state")
        if frm == "inprogress" and owner == by:
            return card
        _require_start(doc, card)
        destination = "inprogress"
    elif action == "complete":
        writeup = _required_text(details.get("writeup"), "completion writeup", code="state")
        if frm != "inprogress":
            raise WorkflowError("completion requires In Progress; start the card first", 409, "state")
        destination = "done"
    elif action == "block":
        reason = _required_text(details.get("reason"), "block reason")
        until = _required_text(details.get("until"), "unblock condition")
        if frm == "done":
            raise WorkflowError("reopen Done work before blocking it", 409, "state")
        destination, event, note = "blocked", "blocked", f"{reason}; unblocks when {until}"
    elif action == "resume":
        note = _required_text(details.get("note"), "resolution note")
        destination = details.get("to", "inprogress")
        if frm != "blocked":
            raise WorkflowError("resume requires a Blocked card", 409, "state")
        if destination not in ("task", "inprogress"):
            raise WorkflowError("resume.to must be task or inprogress")
        if destination == "inprogress":
            _require_start(doc, card)
        event = "unblocked"
    elif action == "cancel":
        reason = _required_text(details.get("reason"), "cancel reason")
        if frm == "done":
            raise WorkflowError("only nonterminal work can be canceled; rework it first", 409, "state")
        destination, note = "done", reason
    elif action in ("rework", "reopen", "bug", "improve"):
        reason = _required_text(details.get("text") if action == "improve"
                                else details.get("reason"), f"{action} reason")
        destination = "inprogress" if action in ("bug", "improve") else "task"
        if action == "reopen":
            destination = details.get("to", "task")
            if destination not in ("task", "inprogress"):
                raise WorkflowError("reopen.to must be task or inprogress")
            if details.get("as", "task") not in ("task", "bug", "improve"):
                raise WorkflowError("reopen.as must be task, bug, or improve")
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
                "children": [], "collapsed": False,
            })
            if action == "bug" and "bug" not in card["tags"]:
                card["tags"].append("bug")
    if action == "complete":
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
    touch(card)
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


def append_note(card: dict, summary, body, by: str) -> dict:
    """Append one timeline entry (one-line summary, optional markdown body) and return it."""
    by = validate_actor(by)
    summary, body = validate_note(summary, body)
    entry = {"id": uuid.uuid4().hex, "at": now_iso(), "by": by, "summary": summary, "body": body}
    card.setdefault("log", []).append(entry)
    hist(card, "note", by=by, note=summary[:80])
    touch(card)
    return entry


def comment_action(card: dict, operation: dict, by: str) -> dict | None:
    by = validate_actor(by)
    if not isinstance(operation, dict) or operation.get("type") not in ("add", "edit", "delete"):
        raise WorkflowError("comment operation must be add, edit, or delete")
    kind = operation["type"]
    allowed = {"type", "text"} if kind == "add" else {"type", "id", "text"}
    if set(operation) - allowed:
        raise WorkflowError("comment author, ID, and timestamps cannot be supplied or changed")
    text = _required_text(operation.get("text"), "comment text", 16000) if kind != "delete" else None
    comments = card.get("comments") or []
    if kind == "add":
        comment = {"id": uuid.uuid4().hex, "at": now_iso(), "by": by, "text": text}
        card.setdefault("comments", []).append(comment)
    else:
        comment_id = operation.get("id")
        if not isinstance(comment_id, str) or not comment_id:
            raise WorkflowError("comment ID is required")
        comment = next((c for c in comments if c["id"] == comment_id), None)
        if comment is None:
            raise WorkflowError("comment not found", 404)
        if kind == "delete":
            comments.remove(comment)
        else:
            comment["text"], comment["updatedAt"], comment["updatedBy"] = text, now_iso(), by
    hist(card, f"comment-{kind}", by=by, note=comment["id"])
    touch(card)
    return None if kind == "delete" else comment


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
    board_path = require_write_scope(board_path)
    if board_path.exists():
        load(board_path)
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
    attachment_id = uuid.uuid4().hex
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


def card_context(board_path: Path, ref: str, full: bool = False) -> dict:
    """One atomic document read; no lock file, unread state, or embedded file bytes.

    Default output keeps comments, pinned notes, and open subtasks whole but trims done
    subtasks to the 10 most recently completed, history to the last 25, and the note
    log to the newest 10 entries.
    """
    doc = load(board_path)
    card = resolve_ref(doc, ref)
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
               "rev": doc["rev"], "card": card, "dependencies": dependencies,
               "missingDependencies": missing, "dependents": dependents,
               "ready": any(item["id"] == card["id"] for item in ready_cards(doc))}
    if full:
        return context
    done = [st for st, _ in iter_subtasks(card["subtasks"]) if st["done"]]
    if len(done) <= 10 and len(card["history"]) <= 25 and len(card["log"]) <= 10:
        return context
    recent = {id(st) for st in sorted(done, key=lambda st: st.get("doneAt") or "")[-10:]}

    def prune(items):
        kept = []
        for st in items:
            children = prune(st["children"])
            if not st["done"] or id(st) in recent or children:
                kept.append({**st, "children": children})
        return kept

    subtasks = prune(card["subtasks"]) if len(done) > 10 else card["subtasks"]
    omitted = {"doneSubtasks": len(done) - sum(st["done"] for st, _ in iter_subtasks(subtasks)),
               "history": max(0, len(card["history"]) - 25)}
    if len(card["log"]) > 10:
        omitted["log"] = len(card["log"]) - 10
    if any(omitted.values()):
        context["card"] = {**card, "subtasks": subtasks, "history": card["history"][-25:],
                           "log": card["log"][-10:]}
        context["omitted"] = omitted
    return context


def _attachment_member(card: dict, attachment_id: str) -> dict:
    item = next((item for item in card["attachments"] if item.get("id") == attachment_id), None)
    if item is None:
        raise WorkflowError("attachment is not present on this card", 404)
    return item


def attachment_add(board_path: Path, ref: str, name: str, data: bytes, by: str,
                   mime: str | None = None, expected_rev=None, card_scoped: bool = True) -> tuple:
    """card_scoped=False keeps the browser's exact board-revision guard."""
    with board_transaction(board_path, expected_rev, ref if card_scoped else None) as doc:
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
                committed = load(board_path)
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
    with board_transaction(board_path, expected_rev, ref if card_scoped else None) as doc:
        card = resolve_ref(doc, ref)
        metadata = _attachment_member(card, attachment_id)
        card["attachments"].remove(metadata)
        hist(card, "attachment-removed", by=by, note=metadata["name"])
        touch(card)
        save(board_path, doc, by=by)
    # Detached bytes belong to backups, archives, and deleted-board recovery too.
    return doc, card, metadata


def attachment_verified_bytes(board_path: Path, metadata: dict) -> bytes:
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
    doc = load(board_path)
    card = resolve_ref(doc, ref)
    metadata = _attachment_member(card, attachment_id)
    body = attachment_verified_bytes(board_path, metadata)
    return doc, card, metadata, body


def require_export_destination(destination, board_path: Path) -> Path:
    path = require_write_scope(destination)
    if path.exists() or path.is_symlink():
        raise WorkflowError(f"export destination already exists: {path}", 409)
    managed = [Path(board_path).absolute().parent, home(), Path(__file__).resolve().parent]
    managed.extend(Path(value).absolute().parent for value in registry_load()["boards"].values())
    if any(path.is_relative_to(root.resolve(strict=False)) for root in managed):
        raise WorkflowError("exports cannot target managed board, registry, or runtime storage", 403)
    for ancestor in path.parents:
        if os.path.normcase(ancestor.name) in ("board", ".workboard", ".git") or (ancestor / "board.json").exists():
            raise WorkflowError("exports cannot target managed storage", 403)
    return path


def attachment_export(board_path: Path, ref: str, attachment_id: str, destination) -> dict:
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
    if len(card["history"]) > HISTORY_CAP:
        del card["history"][:-HISTORY_CAP]


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


def stage_attention(card: dict, stale_hours: int = 24) -> str | None:
    if card.get("column") != "inprogress":
        return None
    subtasks = [subtask for subtask, _ in iter_subtasks(card.get("subtasks") or [])]
    if subtasks and all(subtask.get("done") for subtask in subtasks):
        return "READY TO CLOSE"
    try:
        changed = datetime.datetime.fromisoformat(
            str(card.get("updatedAt") or card.get("createdAt") or "").replace("Z", "+00:00")
        )
        age = datetime.datetime.now(datetime.timezone.utc) - changed
    except (TypeError, ValueError):
        return None
    return "REVIEW STATE" if age.total_seconds() >= stale_hours * 3600 else None


def _atomic_write_json(path: Path, value) -> None:
    path = require_write_scope(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def archive_removed_cards(board_path: Path, cards: list[dict], reason: str) -> Path | None:
    if not cards:
        return None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(board_path).parent / ARCHIVE_DIR / f"{slugify(reason)}-{stamp}-{uuid.uuid4().hex}.json"
    _atomic_write_json(path, {
        "archivedFrom": str(Path(board_path)),
        "reason": reason,
        "cards": cards,
    })
    return path


def _sweep_candidates(doc: dict, days: int) -> list:
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    done_cols = {c["id"] for c in doc["columns"] if c["kind"] == "done"} | {"done"}
    required = {dep for c in doc["cards"] if c["column"] not in done_cols
                for dep in (c.get("dependsOn") or [])}
    return [c for c in doc["cards"]
            if c["id"] not in required and c["column"] in done_cols
            and c["doneAt"] and c["doneAt"] < cutoff]


# ===== sweep / archive =====

def sweep(board_path: Path, days: int = 14, apply: bool = True, expected_rev=None) -> list:
    board_path = Path(board_path)
    if not apply:
        return _sweep_candidates(load(board_path), days)
    with board_transaction(board_path, expected_rev) as doc:
        moving = _sweep_candidates(doc, days)
        if not moving:
            return []
        adir = board_path.parent / ARCHIVE_DIR
        month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        archive_file = adir / f"board-{month}.json"
        try:
            raw_archive = json.loads(read_text_shared(archive_file)) if archive_file.exists() else {}
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"cannot safely read archive: {exc}") from exc
        if isinstance(raw_archive, dict):
            validate_schema(raw_archive)
            archived = raw_archive.get("cards") or []
        elif isinstance(raw_archive, list):
            archived = raw_archive
        else:
            raise WorkflowError("cannot safely merge malformed archive")
        if not isinstance(archived, list) or any(
                not isinstance(c, dict) or not c.get("id") for c in archived):
            raise WorkflowError("cannot safely merge malformed archive cards")
        for card in archived:
            _validate_aliases(card)
        archived_content = {_card_content(c) for c in archived}
        for c in moving:
            if _card_content(c) not in archived_content:
                archived.append(c)
                archived_content.add(_card_content(c))
        _atomic_write_json(
            archive_file,
            {**(raw_archive if isinstance(raw_archive, dict) else {}),
             "archivedFrom": doc.get("name"), "cards": archived},
        )
        swept_ids = {c["id"] for c in moving}
        doc["cards"] = [c for c in doc["cards"] if c["id"] not in swept_ids]
        save(board_path, doc)
        return moving


# ===== multi-project registry =====


class RegistryNotFound(Exception):
    pass


class RegistryConflict(Exception):
    pass


class UnsafeBoardPath(Exception):
    pass

def _registry_json(path: Path, max_bytes=None):
    # Missing optional metadata is first-use, not a transient missing board swap.
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("registry byte limit must be a nonnegative integer")
    with open_shared(path, "rb") as stream:
        data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"registry exceeds {max_bytes} bytes: {path}")
    return json.loads(data)



def registry_load(path=None, *, max_bytes=None) -> dict:
    path = Path(path) if path is not None else registry_path()
    try:
        reg = _registry_json(path, max_bytes)
    except FileNotFoundError:
        if path.is_symlink():
            raise
        return {"boards": {}}
    if (not isinstance(reg, dict) or not isinstance(reg.get("boards"), dict)
            or any(not isinstance(name, str) or not isinstance(value, str)
                   or not Path(value).is_absolute() or Path(value).name != "board.json"
                   for name, value in reg["boards"].items())):
        raise ValueError(f"invalid board registry: {path}")
    return reg




def registry_save(reg: dict) -> None:
    require_write_scope(registry_path())
    registry_load()
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    with board_lock(registry_path()):
        _atomic_write_json(registry_path(), reg)


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


def canonical_registered_board(value) -> Path:
    """Validate and canonicalize an authoritative registry board path."""
    raw = Path(value)
    if not raw.is_absolute() or raw.name != "board.json":
        raise UnsafeBoardPath("registered board must be an absolute board.json path")
    for candidate in (raw, *raw.parents):
        if _has_reparse_point(candidate):
            raise UnsafeBoardPath(f"registered board crosses a link or reparse point: {raw}")
    lock_path = raw.parent / LOCK_NAME
    if _has_reparse_point(lock_path):
        raise UnsafeBoardPath(f"board lock is a link or reparse point: {lock_path}")
    canonical = raw.resolve(strict=False)
    lexical = os.path.normcase(os.path.normpath(str(raw)))
    if lexical != os.path.normcase(str(canonical)):
        raise UnsafeBoardPath(f"registered board path is not canonical: {raw}")
    if raw.exists() and not stat.S_ISREG(os.lstat(raw).st_mode):
        raise UnsafeBoardPath(f"registered board is not a regular file: {raw}")
    return canonical


def _registered_target(reg: dict, name: str, expected_board: str | None = None) -> Path:
    value = reg.get("boards", {}).get(name)
    if not isinstance(value, str):
        raise RegistryNotFound(name)
    target = canonical_registered_board(value)
    if expected_board is not None and expected_board != str(target):
        raise RegistryConflict("registered board identity changed")
    return target


def _without_board_aliases(reg: dict, target: Path) -> dict:
    boards = reg.get("boards", {})
    aliases = {}
    for name, value in boards.items():
        try:
            same = isinstance(value, str) and Path(value).resolve(strict=False) == target
        except (OSError, RuntimeError):
            same = False
        if not same:
            aliases[name] = value
    return {**reg, "boards": aliases}


def register_board(name: str, board_path: Path) -> None:
    """Register an existing board under the lifecycle lock, never a stale path."""
    require_write_scope(board_path)
    require_write_scope(registry_path())
    registry_load()
    load(board_path)
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    with board_lock(registry_path()):
        target = canonical_registered_board(board_path)
        with board_lock(target):
            target = canonical_registered_board(target)
            if not target.is_file():
                raise FileNotFoundError(target)
            reg = registry_load()
            reg.setdefault("boards", {})[name] = str(target)
            _atomic_write_json(registry_path(), reg)


def delete_registered_board(name: str, expected_board: str,
                            base_rev: int | None) -> dict:
    """Recoverably remove one registered board under global -> board locks."""
    require_write_scope(registry_path())
    target = require_write_scope(_registered_target(registry_load(), name, expected_board))
    if target.exists():
        load(target)
    with board_lock(registry_path()):
        reg = registry_load()
        target = _registered_target(reg, name, expected_board)

        if not target.parent.exists():
            if base_rev is not None:
                raise RegistryConflict("registered board is missing")
            _atomic_write_json(registry_path(), _without_board_aliases(reg, target))
            return {"recoveryPath": None, "board": str(target)}

        with board_lock(target):
            target = _registered_target(reg, name, expected_board)
            target = canonical_registered_board(target)
            if not target.exists():
                if base_rev is not None:
                    raise RegistryConflict("registered board is missing")
                _atomic_write_json(registry_path(), _without_board_aliases(reg, target))
                return {"recoveryPath": None, "board": str(target)}
            if base_rev is None:
                raise RegistryConflict("registered board exists")
            doc = load(target)
            if int(doc.get("rev") or 0) != base_rev:
                raise RegistryConflict("registered board revision changed")

            fd, recovery_name = tempfile.mkstemp(
                dir=str(target.parent), prefix="board.deleted-", suffix=".json")
            os.close(fd)
            recovery = Path(recovery_name)
            try:
                atomic_replace(target, recovery)
            except Exception as e:
                if target.exists():
                    recovery.unlink(missing_ok=True)
                    raise
                raise OSError(f"board recovered at {recovery}, but rename finalization failed") from e
            try:
                _atomic_write_json(registry_path(), _without_board_aliases(reg, target))
            except Exception as e:
                raise OSError(
                    f"board recovered at {recovery}, but registry update failed") from e
            return {"recoveryPath": str(recovery), "board": str(target)}


# ===== output =====

def fmt_ref(card: dict) -> str:
    return f"#{card['num']}"


def ok(line: str) -> None:
    print(line)
