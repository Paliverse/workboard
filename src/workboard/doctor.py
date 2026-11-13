# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""`workboard doctor`: installation health plus read-only board data integrity.

Data checks never create files, locks, or directories, and never migrate a board.
They validate the board store (board.db rows, or a legacy board.json read as
JSON), attachment blobs, and the board registry within fixed file/byte/time
budgets. Blockers exit 1; warnings are advice.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

from . import __version__
from . import core as wb

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_FILES = 10000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_SECONDS = 60


class _LimitError(ValueError):
    pass


class _Budget:
    def __init__(self):
        self.files = 0
        self.bytes = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    def take(self, size=0):
        self.files += 1
        self.bytes += size
        if self.files > MAX_FILES or self.bytes > MAX_TOTAL_BYTES or time.monotonic() > self.deadline:
            raise _LimitError("inspection limit reached; evidence is incomplete (files/bytes/time)")


def _finding(report, code, message, path, *, category="data", warning=False):
    report["warnings" if warning else "blockers"].append(
        {"code": code, "category": category, "path": str(path), "message": str(message)})


def _safe_path(path):
    """Inspect lexical ancestors before resolving away link evidence. Never create anything."""
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if wb._has_reparse_point(candidate):
            raise ValueError(f"path crosses a link or reparse point: {candidate}")
    if path.exists():
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError(f"path is not a regular file or directory: {path}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError(f"path is not a private regular file: {path}")
    return path.resolve(strict=False)


def _board_path(value):
    lexical = Path(value).absolute()
    canonical = _safe_path(lexical)
    if not lexical.is_file():
        raise FileNotFoundError(f"board does not exist: {lexical}")
    return lexical, canonical


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid JSON number: {value}")


def _read_bytes(path, budget):
    _safe_path(path)
    size = Path(path).stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds {MAX_JSON_BYTES} byte inspection limit")
    budget.take(size)
    with wb.open_shared(path, "rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON grew beyond inspection limit")
    return data


def _loads(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _json_read(path, budget):
    return _loads(_read_bytes(path, budget))


def _normalization_changes(raw, normalized, path="$", depth=0):
    """Raw values normalization would change."""
    if depth > 64:
        raise ValueError("document nesting exceeds inspection limit")
    if isinstance(raw, dict) and isinstance(normalized, dict):
        card = re.fullmatch(r"\$\.cards\[\d+\]", path)
        for key, value in raw.items():
            target = "cycles" if card and key == "lifecycleCycles" else key
            if target not in normalized:
                yield f"{path}.{key}: field would be removed"
            elif key == "schemaVersion" and path == "$" and value in wb.SUPPORTED_SCHEMA_VERSIONS:
                continue
            else:
                yield from _normalization_changes(value, normalized[target], f"{path}.{key}", depth + 1)
    elif isinstance(raw, list) and isinstance(normalized, list):
        if len(raw) != len(normalized):
            yield f"{path}: list length changes from {len(raw)} to {len(normalized)}"
        for index, (before, after) in enumerate(zip(raw, normalized)):
            yield from _normalization_changes(before, after, f"{path}[{index}]", depth + 1)
    elif raw is not None and (type(raw) is not type(normalized) or raw != normalized):
        yield f"{path}: existing value changes during normalization"


def _identities(items, label, *, nums=False):
    if not isinstance(items, list):
        raise ValueError(f"{label} must be an array")
    if len(items) > MAX_FILES:
        raise _LimitError(f"{label} exceeds inspection entry limit")
    ids, numbers = set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{label} entries must be objects")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 120:
            raise ValueError(f"{label} has an invalid or missing ID")
        if identity in ids:
            raise ValueError(f"{label} has duplicate ID {identity!r}")
        ids.add(identity)
        if nums:
            number = item.get("num")
            if type(number) is not int or number < 1 or number in numbers:
                raise ValueError(f"{label} has invalid or duplicate card number {number!r}")
            numbers.add(number)
    return ids, numbers


def _subtasks(items, seen=None, depth=0):
    if depth > 64:
        raise ValueError("subtask nesting exceeds inspection limit")
    seen = set() if seen is None else seen
    ids, _ = _identities(items, "subtasks")
    if seen & ids:
        raise ValueError("subtask IDs are duplicated across the card tree")
    seen.update(ids)
    for item in items:
        if not isinstance(item.get("text", ""), str) or type(item.get("done", False)) is not bool:
            raise ValueError("subtask text/done fields have invalid types")
        _subtasks(item.get("children") or [], seen, depth + 1)


def _attachment_metadata_size(metadata):
    """Validate every manifest, even when its immutable bytes were checked earlier."""
    size = metadata.get("size")
    if type(size) is not int or not 0 <= size <= wb.MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment declared size is outside 0..10 MiB or is not an integer")
    name = metadata.get("name")
    wb._required_text(name, "attachment name", 255)
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError("attachment name contains control characters")
    mime = metadata.get("mime")
    if (not isinstance(mime, str) or len(mime) > 255
            or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)):
        raise ValueError("invalid attachment MIME type")
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("attachment has an invalid SHA256")
    wb.validate_actor(metadata.get("by"))
    # Preserve historical timestamp spellings; presence/type, not chronology, is the contract.
    wb._required_text(metadata.get("createdAt"), "attachment creation time")
    return size


# What normalization changes by design: removed card fields (links, inbox acknowledgements; comments fold
# into the log), and in delegated subtasks the dropped blocker and prerequisites, the blocked state, and
# the review every result now needs.
_DROPPED = frozenset({"links", "linkedCards", "comments", "inboxAcknowledged"})
_BY_DESIGN = re.compile(r"\$\.cards\[\d+\]\.subtasks\[\d+\]\.delegation\.(review\.required|blocker|dependsOn|state)\b")


def _expected(card):
    """A raw card as normalization keeps it by design; nested checklists also load flat in pre-order."""
    kept = {key: value for key, value in card.items() if key not in _DROPPED}
    if card.get("subtasks"):
        kept["subtasks"] = wb.flatten_subtasks(card["subtasks"])
    if card.get("comments"):
        kept.pop("log", None)  # Comments merge into it.
    return kept


def _validate_document(raw, path, board, report, budget, blob_cache):
    if not isinstance(raw, dict):
        raise ValueError("document must be a JSON object")
    version = wb.validate_schema(raw)
    log_invalid = False
    cards = raw.get("cards")
    card_ids, numbers = _identities(cards, "cards", nums=True)
    columns, _ = _identities(raw.get("columns"), "columns")
    if columns != wb.CORE_COLUMN_ID_SET:
        raise ValueError("board must contain exactly the five core columns")
    if type(raw.get("rev")) is not int or raw["rev"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    next_num = raw.get("nextNum")
    if type(next_num) is not int or next_num <= max(numbers, default=0):
        raise ValueError("nextNum must be an integer greater than every card number")
    for card in cards:
        label = f"{path}#{card['id']}"
        if not isinstance(card.get("title", ""), str) or card.get("column") not in wb.CORE_COLUMN_ID_SET:
            raise ValueError(f"{label}: invalid title or column")
        if "lifecycleCycles" in card:
            if "cycles" in card and card["cycles"] != card["lifecycleCycles"]:
                raise ValueError(f"{label}: conflicting cycles/lifecycleCycles aliases")
            _finding(report, "legacy-alias", "Unambiguous lifecycleCycles will become cycles", label, warning=True)
        for key in ("tags", "dependsOn", "history", "cycles", "lifecycleCycles", "attachments", "subtasks",
                    "verification", "reviews"):
            if key in card and card[key] is not None and not isinstance(card[key], list):
                raise ValueError(f"{label}: {key} must be an array")
        for key in ("tags", "dependsOn"):
            if any(not isinstance(value, str) for value in (card.get(key) or [])):
                raise ValueError(f"{label}: {key} must contain strings")
        _subtasks(card.get("subtasks") or [])
        attachments = card.get("attachments") or []
        _identities(attachments, "attachments")
        for metadata in attachments:
            try:
                size = _attachment_metadata_size(metadata)
                # Boards may reuse attachment IDs, never their storage: `board` is the canonical board path.
                key = (os.path.normcase(str(board)), metadata["id"], metadata.get("sha256"), size)
                if key not in blob_cache:
                    budget.take(size)
                    try:
                        data = wb.attachment_verified_bytes(board, metadata)
                        blob_cache[key] = {"ok": True, "size": len(data)}
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        blob_cache[key] = {"ok": False, "error": str(exc)}
                if not blob_cache[key]["ok"]:
                    raise ValueError(blob_cache[key]["error"])
            except (OSError, ValueError, TypeError, KeyError) as exc:
                _finding(report, "attachment-integrity", exc, label)
                if isinstance(exc, _LimitError):
                    raise
        try:
            wb.normalize_log(card.get("log"))
        except wb.WorkflowError as exc:
            log_invalid = True
            _finding(report, "log-invalid", exc, label)
        missing = sorted(set(card.get("dependsOn") or []) - card_ids)
        if missing:
            _finding(report, "missing-dependency", f"Unresolved dependencies: {', '.join(missing)}", label, warning=True)
    summary = {"schemaVersion": version, "rev": raw["rev"], "cards": len(cards)}
    if log_invalid:
        return summary  # Normalization would reject the same entries; log-invalid already names them.
    try:
        # The core normalizer owns delegated scope and review invariants; doctor reports its refusal
        # without reproducing lifecycle rules here.
        normalized = wb.normalize_doc(copy.deepcopy(raw))
    except wb.WorkflowError as exc:
        if version == wb.SCHEMA_VERSION:
            _finding(report, "delegation-invalid", exc, path)
            return summary
        raise
    for change in _normalization_changes({**raw, "cards": [_expected(card) for card in cards]}, normalized):
        budget.take()
        if _BY_DESIGN.match(change):
            continue
        # A timeline entry normalization would still rewrite (untrimmed summary/body) breaks the log rules.
        code = "log-invalid" if re.match(r"\$\.cards\[\d+\]\.log\b", change) else "normalization-loss"
        _finding(report, code, change, path)
    return summary


def _inspect_store(store, board, report, budget, blob_cache):
    """board.db without writing: PRAGMA quick_check, then the document its rows make, checked as board.json
    was, and each row's indexed columns against its card body."""
    import sqlite3
    _safe_path(store)
    budget.take(store.stat().st_size)
    # A -wal file means a writer is open or crashed: read it as is, since a normal connection's close would
    # checkpoint it into the db. Without one, a normal connection removes the files it creates.
    wal = store.with_name(store.name + "-wal").exists()
    try:
        con = sqlite3.connect(store.absolute().as_uri() + ("?mode=ro" if wal else ""), uri=True,
                              isolation_level=None)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")  # One snapshot for every read below.
            problems = [line for (line,) in con.execute("PRAGMA quick_check")]
            if problems != ["ok"]:
                raise sqlite3.DatabaseError("; ".join(problems[:5]).replace("\n", " "))
            meta = con.execute("SELECT k, v FROM meta ORDER BY rowid").fetchall()
            rows = con.execute("SELECT * FROM cards ORDER BY pos").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        _finding(report, "store-corrupt", f"the board store is damaged ({exc}); copy the board folder aside "
                 "before any repair", store)
        return {}
    raw = {key: _loads(value) for key, value in meta}
    raw["cards"] = []
    for row in rows:
        try:
            raw["cards"].append(_loads(row[-1]))
        except ValueError as exc:
            raise ValueError(f"card row {row[0]!r}: {exc}") from exc
    summary = _validate_document(raw, store, board, report, budget, blob_cache)
    for row, card in zip(rows, raw["cards"]):
        try:
            derived = wb._row(card, row[5], row[-1])
        except (KeyError, TypeError):
            continue  # A body missing a field normalize_card adds is already a normalization-loss.
        if derived != row:
            _finding(report, "row-mismatch", "the row's num/column/doneAt/changedRev/dependsOn columns disagree "
                     "with its card body, so scoped reads can miss or misfile the card", f"{store}#{row[0]}")
    return summary


def _inspect_data(value, report, budget, blob_cache):
    result = {"requestedPath": os.fspath(value), "ok": False}
    start = len(report["blockers"])
    try:
        lexical, board = _board_path(value)
        result.update(lexicalPath=str(lexical), path=str(board))
        data = _read_bytes(lexical, budget)
        db, tmp = lexical.with_name(wb.DB_NAME), lexical.with_name(wb.DB_NAME + ".tmp")
        stub = wb._is_stub(data)
        legacy = not stub and not db.exists()
        store = db if db.exists() else tmp if stub and tmp.exists() else None
        if legacy:  # Every core read migrates a legacy board, so doctor checks the JSON itself.
            result.update(_validate_document(_loads(data), lexical, board, report, budget, blob_cache))
            _finding(report, "legacy-storage", "pre-0.4 single-file board: the next WorkBoard command migrates "
                     f"it into {wb.DB_NAME}, keeping these bytes in {wb.BACKUP_DIR}", lexical, warning=True)
        elif not stub:
            _finding(report, "split-brain", wb._split_brain(lexical), lexical)
        elif store is None:
            _finding(report, "store-missing", f"board.json points at {wb.DB_NAME}, which is missing; restore the "
                     f"board folder from {wb.deleted_dir()} or a {wb.BACKUP_DIR} copy", db)
        if store is not None:
            result.update(_inspect_store(store, board, report, budget, blob_cache))
        if tmp.exists():
            _finding(report, "store-leftover", "the migration stopped after its commit point; the next WorkBoard "
                     "command finishes it" if store == tmp else "an interrupted migration left it; the next "
                     "migration replaces it" if legacy else "left over from a finished migration; WorkBoard "
                     "never reads it, safe to delete", tmp, warning=True)
        # Migration writes .backups and takes the board lock; a migrated board uses neither.
        for name in (wb.BACKUP_DIR, "attachments") if legacy else ("attachments",):
            directory = lexical.parent / name
            _safe_path(directory)
            if directory.exists() and not directory.is_dir():
                raise ValueError(f"managed storage is not a directory: {directory}")
        lock = lexical.parent / wb.LOCK_NAME
        if legacy and lock.exists() and not lock.is_file():
            raise ValueError(f"board lock is not a regular file: {lock}")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        _finding(report, "board-invalid", exc, value)
    result["ok"] = len(report["blockers"]) == start
    return result


_SYNCED = re.compile(r"onedrive( - .+)?|dropbox|google ?drive|my drive|icloud ?drive|com~apple~clouddocs"
                     r"|cloudstorage", re.IGNORECASE)
_NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "afs", "ceph", "glusterfs",
                         "fuse.sshfs", "fuse.rclone", "davfs"})


def _check_home(report) -> None:
    """SQLite's write-ahead log needs a local disk: warn when WorkBoard's home is on a network drive or in
    a synced folder (macOS mounts are not checked)."""
    home, where = wb.home(), None
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(str(home))[0] + "\\") == 4:  # DRIVE_REMOTE
            where = "a network drive"
    elif os.path.exists("/proc/mounts"):  # Linux: the filesystem of the deepest mount holding home.
        with open("/proc/mounts", encoding="utf-8", errors="replace") as stream:
            mounts = [fields[1:3] for fields in map(str.split, stream) if len(fields) > 2]
        held = [mount for mount in mounts if (str(home) + "/").startswith(mount[0].rstrip("/") + "/")]
        kind = max(held, key=lambda mount: len(mount[0]), default=(None, None))[1]
        if kind in _NETWORK_FS:
            where = f"a network filesystem ({kind})"
    if where is None and any(_SYNCED.fullmatch(part) for part in home.parts):
        where = "a synced folder"
    if where:
        _finding(report, "home-not-local", f"WorkBoard's home is on {where}; SQLite's write-ahead log needs a "
                 "local disk, so boards there can be corrupted or lose writes: set WORKBOARD_HOME to a local "
                 "folder", home, warning=True)


def _registry(path, report, budget):
    result = {"path": str(path), "exists": False, "ok": False}
    try:
        _safe_path(path)
        result["exists"] = Path(path).exists()
        if not result["exists"]:
            result["ok"] = True
            return result, {"version": 2, "boards": {}}
        raw = _json_read(path, budget)
        if not isinstance(raw, dict) or wb.registry_load(path, max_bytes=MAX_JSON_BYTES) != raw:
            raise ValueError("board registry is malformed or changed during inspection")
        result["ok"] = True
        return result, raw
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        _finding(report, "registry-invalid", exc, path, category="registry")
        result["error"] = str(exc)
        return result, None


def _check_data(report, board, all_registered):
    """The current board (or every registered board with --all), the registry, the store, and legacy files."""
    budget, cache, seen, directories = _Budget(), {}, set(), {}

    def inspect(value):
        result = _inspect_data(value, report, budget, cache)
        report["boards"].append(result)
        if result.get("path"):
            seen.add(os.path.normcase(result["path"]))
            directories[os.path.normcase(result["path"])] = Path(result["path"]).parent

    try:
        current = wb.resolve_board(board)[1]
    except (OSError, ValueError) as exc:
        current = None
        if board:  # Only an explicit --board is a failure; an unlinked folder just has no board.
            _finding(report, "board-invalid", exc, board)
    if current is not None:
        inspect(current)
    _check_home(report)
    report["registry"], registry = _registry(wb.registry_path(), report, budget)
    entries = report["registry"]["entries"] = []
    for name, value in (registry or {}).get("boards", {}).items():
        project, path = Path(value["project"]), wb._board_path(value)
        entry = {"name": name, "board": str(path), "project": str(project), "ok": False}
        entries.append(entry)
        try:
            budget.take()
            if not project.is_dir():
                _finding(report, "project-missing",
                         f"board '{name}' is linked to a folder that no longer exists; "
                         f"from the project's new location run: workboard link {name}",
                         project, category="registry", warning=True)
            legacy = project / "board" / "board.json"
            if legacy.is_file():
                _finding(report, "legacy-project-board",
                         f"left over from before boards moved to {wb.boards_dir()}; board '{name}' lives "
                         "there now and WorkBoard never reads this copy", legacy, category="registry", warning=True)
            canonical = _safe_path(path)
            if not path.is_file():
                entry["error"] = "registered board is missing"
                if path.with_name(wb.DB_NAME).exists():
                    _finding(report, "stub-missing", f"{wb.DB_NAME} holds board '{name}' but its board.json is "
                             "gone, so WorkBoard cannot open it: recreate board.json as " + json.dumps(
                                 {"schemaVersion": wb.STORE_SCHEMA, "storage": wb.DB_NAME, "name": name}),
                             path, category="registry")
                else:
                    _finding(report, "registered-board-missing",
                             f"board '{name}' is registered but its board.json is gone", path,
                             category="registry", warning=True)
                continue
            entry.update(ok=True, path=str(canonical))
            directories.setdefault(os.path.normcase(str(canonical)), canonical.parent)
            if all_registered and os.path.normcase(str(canonical)) not in seen:
                inspect(path)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            entry["error"] = str(exc)
            _finding(report, "registered-board-invalid", exc, path, category="registry")
            if isinstance(exc, _LimitError):
                break
    if registry is not None:
        used = {value["dir"] for value in registry["boards"].values()}
        store = wb.boards_dir()
        try:
            _safe_path(store)
            found = sorted(item.name for item in os.scandir(store) if item.is_dir(follow_symlinks=False))
        except FileNotFoundError:
            found = []
        except (OSError, ValueError) as exc:
            found = []
            _finding(report, "registry-invalid", exc, store, category="registry")
        for name in found:
            if name not in used:
                _finding(report, "unregistered-board-dir",
                         "no board in boards.json uses this folder, so WorkBoard never reads it; "
                         f"if it is not needed, move it to {wb.deleted_dir()}",
                         store / name, category="registry", warning=True)
    legacy = [wb.home() / "viewers.json", *(directory / ".viewer.port" for directory in directories.values())]
    for path in legacy:
        if path.exists():
            _finding(report, "legacy-viewer-file",
                     "left over from the retired per-board viewers; the single server ignores it, safe to delete",
                     path, category="install", warning=True)


def _check_path(report, installation) -> dict:
    """Does `workboard` on PATH run this same installation?"""
    import subprocess
    found = shutil.which("workboard")
    result = {"workboard": found, "sameInstall": False}
    if found is None:
        _finding(report, "not-on-path", "`workboard` is not on PATH, so agents cannot run it",
                 "PATH", category="install", warning=True)
        return result
    try:
        proc = subprocess.run([found, "version", "--json"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
        other = json.loads(proc.stdout.strip().splitlines()[-1])
        if not isinstance(other, dict):
            raise ValueError("`version --json` did not print an object")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        _finding(report, "path-unverified", f"cannot run `{found} version --json`: {exc}", found,
                 category="install", warning=True)
        return result
    result.update(version=other.get("version"), channel=other.get("channel"), executable=other.get("executable"))
    result["sameInstall"] = (
        other.get("channel") == installation["channel"] and isinstance(other.get("executable"), str)
        and os.path.normcase(os.path.realpath(other["executable"]))
        == os.path.normcase(os.path.realpath(sys.executable)))
    if not result["sameInstall"]:
        _finding(report, "path-other-install",
                 f"`workboard` on PATH is v{other.get('version')} ({other.get('channel')}) at "
                 f"{other.get('executable')}, not this installation", found, category="install", warning=True)
    return result


def _check_installation(report) -> None:
    from . import install, update
    info = report["installation"] = {
        "version": __version__, "channel": update.detect_channel(), "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False))}
    info["path"] = _check_path(report, info)
    try:
        info["skills"] = install.skills_status()
    except OSError as exc:
        info["skills"] = []
        _finding(report, "skill-bundle-missing", f"the bundled SKILL.md cannot be read: {exc}",
                 "workboard/skills/workboard/SKILL.md", category="install")
    for target in info["skills"]:
        if target["state"] == "stale":
            _finding(report, "skill-stale", "outdated agent skill; run: workboard skills install --refresh",
                     target["path"], category="install", warning=True)
        elif target["state"] == "foreign":
            _finding(report, "skill-foreign", "another skill occupies the workboard skill directory",
                     target["path"], category="install", warning=True)
    if info["skills"] and all(target["state"] == "missing" for target in info["skills"]):
        _finding(report, "skills-missing", "no agent skill is installed; run: workboard setup",
                 Path.home(), category="install", warning=True)
    codex = info["codex"] = install.codex_sandbox_status()
    if codex["state"] == "missing":
        _finding(report, "codex-sandbox",
                 f"Codex's sandbox cannot write to {wb.home()}, so Codex agents cannot update boards; "
                 "run: workboard setup", codex["config"], category="install", warning=True)
    try:
        service = info["service"] = install.service_status()
    except (OSError, wb.WorkflowError) as exc:
        info["service"] = {"error": str(exc)}
        _finding(report, "service-unknown", f"cannot read the service state: {exc}", "service",
                 category="install", warning=True)
        return
    if service["installed"] and not service["current"]:
        _finding(report, "service-stale", "the service starts a different command; run: workboard service install",
                 service["location"], category="install", warning=True)
    server = service.get("server")
    if server and server.get("version") != __version__:  # A 0.3.x server cannot read board.db stores.
        _finding(report, "server-schema-mismatch",
                 f"the running server is v{server.get('version')} but this CLI is v{__version__}, and a server "
                 "of another version may not read these boards; run: workboard service restart",
                 server.get("port"), category="install")


def diagnose(board=None, *, all_registered=False) -> dict:
    """Full doctor report; `ok` is False exactly when blockers exist."""
    report = {"ok": False, "version": __version__, "allRegistered": bool(all_registered),
              "blockers": [], "warnings": [], "installation": {}, "boards": [], "registry": {}}
    _check_installation(report)
    _check_data(report, board, all_registered)
    report["limits"] = {"maxFiles": MAX_FILES, "maxBytes": MAX_TOTAL_BYTES, "maxSeconds": MAX_SECONDS}
    report["ok"] = not report["blockers"]
    return report


def _summary(report) -> str:
    from . import install
    info = report["installation"]
    path = info.get("path") or {}
    service = info.get("service") or {}
    lines = [f"workboard {info['version']} ({info['channel']}) · {info['executable']}",
             "  PATH: " + ("this installation" if path.get("sameInstall")
                          else path.get("workboard") or "`workboard` not found"),
             "  skills: " + ", ".join(f"{target['state']} {target['path']}" for target in info.get("skills", []))]
    if "error" in service:
        lines.append(f"  service: unknown ({service['error']})")
    else:
        lines.append(f"  service: {'installed' if service['installed'] else 'not installed'} "
                     f"({service['kind']}) · {install._server_text(service.get('server'))}")
    for board in report["boards"]:
        facts = f"rev {board['rev']} · {board['cards']} cards · " if "rev" in board else ""
        lines.append(f"  board: {board.get('path') or board['requestedPath']} · {facts}"
                     + ("ok" if board["ok"] else "problems"))
    if not report["boards"]:
        lines.append("  board: none here (pass --board NAME, or --all for every registered board)")
    entries = report["registry"].get("entries", [])
    lines.append(f"  registry: {len(entries)} registered board{'s' if len(entries) != 1 else ''}")
    for kind in ("blockers", "warnings"):
        lines += [f"{kind[:-1]} [{item['code']}] {item['path']}: {item['message']}" for item in report[kind]]
    lines.append(f"doctor: {'ok' if report['ok'] else 'FAILED'} · {len(report['blockers'])} blockers, "
                 f"{len(report['warnings'])} warnings")
    return "\n".join(lines)


def cmd_doctor(args) -> None:
    report = diagnose(args.board, all_registered=args.all)
    print(json.dumps(report, ensure_ascii=False) if args.json else _summary(report))
    if report["blockers"]:
        raise SystemExit(1)


def register(add) -> None:
    p = add("doctor", cmd_doctor, "check the installation and the board data (read-only)")
    p.add_argument("--all", action="store_true", help="validate every registered board, not just this one")
