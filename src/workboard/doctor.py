#!/usr/bin/env python3
"""Read-only release evidence and an explicit, copy-only data recovery rehearsal."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

from . import core as wb

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_FILES = 10000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_VIEWERS = 128
MAX_SECONDS = 60
OPERATOR_GATES = [
    "Inventory and quiesce all old CLI sessions/viewers and other writers together; unknown old binaries cannot be fenced retroactively.",
    "Obtain coordinated live-cutover approval, checkpoint runtime/data/blobs/recovery/registries/instructions/task definition, then refresh active sessions.",
    "Preserve post-cutover work in any rollback; data rehearsal is not live deployment or universal rollback proof.",
]


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
    _safe_path(lexical)
    if lexical.is_dir():
        lexical = lexical / "board" / "board.json"
    _safe_path(lexical)
    canonical = wb.canonical_registered_board(lexical)
    if not canonical.is_file():
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


def _json_read(path, budget):
    _safe_path(path)
    size = Path(path).stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds {MAX_JSON_BYTES} byte inspection limit")
    budget.take(size)
    with wb.open_shared(path, "rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON grew beyond inspection limit")
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _files(root, budget, depth=0, *, include_dirs=False):
    _safe_path(root)
    if depth > 64:
        raise ValueError(f"directory nesting exceeds inspection limit: {root}")
    with os.scandir(root) as entries:
        for entry in entries:
            budget.take()
            path = Path(entry.path)
            _safe_path(path)
            if entry.is_dir(follow_symlinks=False):
                if include_dirs:
                    yield path
                yield from _files(path, budget, depth + 1, include_dirs=include_dirs)
            else:
                yield path


def _normalization_changes(raw, normalized, path="$", depth=0):
    if depth > 64:
        raise ValueError("document nesting exceeds inspection limit")
    if isinstance(raw, dict) and isinstance(normalized, dict):
        for key, value in raw.items():
            target = {"linkedCards": "links", "lifecycleCycles": "cycles"}.get(key, key) if re.fullmatch(r"\$\.cards\[\d+\]", path) else key
            if target not in normalized:
                yield f"{path}.{key}: field would be removed"
            elif key == "schemaVersion" and path == "$" and value in (1, wb.SCHEMA_VERSION):
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


def _validate_document(raw, path, board, report, budget, blob_cache, *, archive=False):
    if archive and isinstance(raw, list):
        raw = {"cards": raw}
    if not isinstance(raw, dict):
        raise ValueError("document must be a JSON object (legacy archive arrays are also accepted)")
    if not archive or "schemaVersion" in raw:
        version = wb.validate_schema(raw)
        if version != wb.SCHEMA_VERSION:
            _finding(report, "legacy-schema", "Supported legacy schema will upgrade on the next mutation", path, warning=True)
    cards = raw.get("cards")
    card_ids, numbers = _identities(cards, "cards", nums=True)
    if not archive:
        columns, _ = _identities(raw.get("columns"), "columns")
        if columns != wb.CORE_COLUMN_ID_SET:
            raise ValueError("board must contain exactly the five core columns")
    if not archive or "rev" in raw:
        if type(raw.get("rev")) is not int or raw["rev"] < 0:
            raise ValueError("revision must be a nonnegative integer")
    if not archive or "nextNum" in raw:
        next_num = raw.get("nextNum")
        if type(next_num) is not int or next_num <= max(numbers, default=0):
            raise ValueError("nextNum must be an integer greater than every card number")
    for card in cards:
        budget.take()
        label = f"{path}#{card['id']}"
        if not isinstance(card.get("title", ""), str) or card.get("column") not in wb.CORE_COLUMN_ID_SET:
            raise ValueError(f"{label}: invalid title or column")
        for canonical, alias in (("links", "linkedCards"), ("cycles", "lifecycleCycles")):
            if alias in card:
                if canonical in card and card[canonical] != card[alias]:
                    raise ValueError(f"{label}: conflicting {canonical}/{alias} aliases")
                _finding(report, "legacy-alias", f"Unambiguous {alias} will become {canonical}", label, warning=True)
        for key in ("tags", "links", "linkedCards", "dependsOn", "history", "cycles", "lifecycleCycles",
                    "comments", "attachments", "subtasks", "verification", "reviews"):
            if key in card and card[key] is not None and not isinstance(card[key], list):
                raise ValueError(f"{label}: {key} must be an array")
        for key in ("tags", "links", "linkedCards", "dependsOn"):
            if any(not isinstance(value, str) for value in (card.get(key) or [])):
                raise ValueError(f"{label}: {key} must contain strings")
        _subtasks(card.get("subtasks") or [])
        comments = card.get("comments") or []
        _identities(comments, "comments")
        if any(not isinstance(comment.get("text"), str) or not isinstance(comment.get("by"), str) for comment in comments):
            raise ValueError(f"{label}: comment text and author must be strings")
        attachments = card.get("attachments") or []
        _identities(attachments, "attachments")
        for metadata in attachments:
            try:
                size = _attachment_metadata_size(metadata)
                blob = wb.attachment_path(board, metadata["id"])
                key = (os.path.normcase(str(blob.resolve(strict=False))), metadata["id"],
                       metadata.get("sha256"), size)
                # Recovery manifests may reuse IDs across boards, never their storage identity.
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
        if not archive:
            missing = sorted(set(card.get("dependsOn") or []) - card_ids)
            if missing:
                _finding(report, "missing-dependency", f"Unresolved dependencies: {', '.join(missing)}", label, warning=True)
    if archive:
        normalized = {**raw, "cards": [wb.normalize_card(card) for card in cards]}
    else:
        normalized = wb.normalize_doc(copy.deepcopy(raw))
    for change in _normalization_changes(raw, normalized):
        budget.take()
        _finding(report, "normalization-loss", change, path)
    return {"schemaVersion": raw.get("schemaVersion", 1), "rev": raw.get("rev"), "cards": len(cards)}


def _inspect_data(value, report, budget, blob_cache):
    result = {"requestedPath": os.fspath(value), "ok": False, "recovery": []}
    start = len(report["blockers"])
    try:
        lexical, board = _board_path(value)
        result.update(lexicalPath=str(lexical), path=str(board))
        raw = _json_read(lexical, budget)
        result.update(_validate_document(raw, lexical, board, report, budget, blob_cache))
        for name in (wb.BACKUP_DIR, wb.ARCHIVE_DIR, "attachments"):
            directory = lexical.parent / name
            _safe_path(directory)
            if directory.exists() and not directory.is_dir():
                raise ValueError(f"managed storage is not a directory: {directory}")
        lock = lexical.parent / wb.LOCK_NAME
        if lock.exists() and not lock.is_file():
            raise ValueError(f"board lock is not a regular file: {lock}")
        for path in _files(lexical.parent, budget):
            relative = path.relative_to(lexical.parent)
            if path == lexical:
                continue
            recovery = relative.parts[0] in (wb.BACKUP_DIR, wb.ARCHIVE_DIR) or path.name.startswith("board.deleted-")
            if not recovery or path.suffix.lower() != ".json":
                continue
            entry = {"path": str(path), "ok": False}
            before = len(report["blockers"])
            try:
                archive = relative.parts[0] == wb.ARCHIVE_DIR
                recovered = _json_read(path, budget)
                entry.update(_validate_document(recovered, path, board, report, budget, blob_cache, archive=archive))
                match = re.fullmatch(r"board-(\d+)\.json", path.name)
                if relative.parts[0] == wb.BACKUP_DIR and match and int(match[1]) != recovered.get("rev"):
                    raise ValueError("backup filename revision disagrees with its document")
                entry["ok"] = before == len(report["blockers"])
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
                _finding(report, "recovery-invalid", exc, path)
                if isinstance(exc, _LimitError):
                    raise
            result["recovery"].append(entry)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, wb.UnsafeBoardPath) as exc:
        _finding(report, "board-invalid", exc, value)
    result["ok"] = len(report["blockers"]) == start
    return result


def _registry(path, kind, report, budget):
    result = {"path": str(path), "exists": False, "ok": False}
    try:
        _safe_path(path)
        result["exists"] = Path(path).exists()
        if not result["exists"]:
            result["ok"] = True
            return result, {"boards": {}} if kind == "boards" else {}
        raw = _json_read(path, budget)
        strict = (wb.registry_load(path, max_bytes=MAX_JSON_BYTES) if kind == "boards"
                  else wb._viewers_load(path, max_bytes=MAX_JSON_BYTES))
        if not isinstance(raw, dict) or strict != raw:
            raise ValueError("registry is malformed or changed during inspection")
        if kind == "boards" and not isinstance(raw.get("boards"), dict):
            raise ValueError("board registry must contain a boards object")
        result["ok"] = True
        return result, raw
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        _finding(report, "registry-invalid", exc, path, category="runtime")
        result["error"] = str(exc)
        return result, None


def _windows_census():
    """Bounded read-only CIM inventory; inability to inspect Python is missing evidence."""
    if os.name != "nt":
        return {"available": False, "complete": False, "processes": [], "error": "Windows CIM census is unavailable on this platform"}
    script = r'''$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $rows=@(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name='py.exe'" | Select-Object -First 513 | ForEach-Object { $line=$_.CommandLine; [pscustomobject]@{pid=[int]$_.ProcessId;name=$_.Name;commandLine=if($line.Length -gt 8192){$line.Substring(0,8192)}else{$line};truncated=($line.Length -gt 8192)} }); ConvertTo-Json -InputObject $rows -Compress -Depth 3'''
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
                                   creationflags=0x08000000)
        if completed.returncode or len(completed.stdout) > 8 * 1024 * 1024:
            raise ValueError(f"CIM failed or exceeded output limit: {completed.stderr[:1000].decode(errors='replace')}")
        rows = json.loads(completed.stdout.decode("utf-8-sig"))
        if not isinstance(rows, list) or len(rows) > 512:
            raise ValueError("CIM process list is malformed or incomplete")
        complete = all(isinstance(row, dict) and type(row.get("pid")) is int and row["pid"] > 0
                       and isinstance(row.get("commandLine"), str) and row["commandLine"].strip()
                       and not row.get("truncated") for row in rows)
        return {"available": True, "complete": complete, "processes": rows,
                "error": None if complete else "One or more Python command lines are inaccessible/truncated"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "complete": False, "processes": [], "error": str(exc)}


def _command_args(line):
    if os.name != "nt":
        return [part.strip('"') for part in shlex.split(line, posix=False)]
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(argv)


def _viewer_command(row):
    if not isinstance(row, dict):
        raise ValueError("CIM process entry is not an object")
    line = row.get("commandLine")
    if not isinstance(line, str) or not line:
        return None
    args = _command_args(line)
    names = [arg.replace("\\", "/").rsplit("/", 1)[-1].lower() for arg in args]
    if "serve.py" not in names and not ("card.py" in names and "serve" in args):
        return None
    def option(name):
        for index, arg in enumerate(args):
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
            if arg == name:
                return args[index + 1] if index + 1 < len(args) else None
        return None
    return {**row, "boardArgument": option("--board"), "announce": option("--announce"),
            "portArgument": option("--port"),
            "runtimeScript": next(arg for arg, name in zip(args, names) if name in ("serve.py", "card.py"))}


def _inventory(report, boards, viewers, budget):
    candidates, seen = [], set()
    def add(board, entry, source):
        if len(candidates) >= MAX_VIEWERS:
            raise ValueError("viewer inventory limit reached; evidence is incomplete")
        if not isinstance(entry, dict):
            raise ValueError(f"malformed viewer entry: {source}")
        pid, port = entry.get("pid"), entry.get("port")
        if type(pid) is not int or pid <= 0 or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"invalid viewer PID/port: {source}")
        key = (os.path.normcase(str(board)), pid, port)
        if key in seen:
            for candidate in candidates:
                if candidate["key"] == key:
                    candidate["sources"].append(source)
                    return
        seen.add(key)
        candidates.append({"key": key, "board": str(board), "entry": entry, "sources": [source]})

    if viewers is not None:
        for directory, entry in viewers.items():
            try:
                budget.take()
                lexical = Path(directory) / "board.json"
                if not Path(directory).is_absolute():
                    raise ValueError("viewer registry board directory must be absolute")
                board = wb.canonical_registered_board(lexical)
                _safe_path(lexical)
                boards.setdefault(os.path.normcase(str(board)), board)
                add(board, entry, "registry")
            except (OSError, ValueError, TypeError, RuntimeError, wb.UnsafeBoardPath) as exc:
                _finding(report, "viewer-entry-invalid", exc, directory, category="runtime")
                if isinstance(exc, _LimitError):
                    raise
    census = _windows_census()
    report["census"] = census
    if not census.get("available") or not census.get("complete"):
        _finding(report, "census-incomplete", census.get("error") or "Process census is unavailable/incomplete", "Windows CIM", category="runtime")
    known = []
    runtime_root = Path(wb.runtime_info()["runtimeRoot"]).resolve(strict=False)
    census["runtimeRoot"] = str(runtime_root)
    census["scope"] = "selected/registered boards or the runtime under inspection"
    for row in census.get("processes", []):
        try:
            budget.take()
            command = _viewer_command(row)
            if command:
                known.append(command)
                argument = command["boardArgument"]
                script = Path(command["runtimeScript"])
                runtime_known = script.is_absolute()
                same_runtime = runtime_known and script.resolve(strict=False).parent == runtime_root
                board_known = bool(argument) and Path(argument).is_absolute()
                same_board = False
                if board_known:
                    candidate = Path(argument)
                    if candidate.name != "board.json":
                        candidate = candidate / "board" / "board.json"
                    same_board = os.path.normcase(str(candidate.resolve(strict=False))) in boards
                command["inScope"] = same_runtime or same_board or not (runtime_known and board_known)
                command["scopeReason"] = ("runtime-under-inspection" if same_runtime else
                                          "selected-or-registered-board" if same_board else
                                          "unrelated-runtime-and-board" if not command["inScope"] else
                                          "indeterminate-command-scope")
                if not command["inScope"]:
                    continue
                if argument and Path(argument).is_absolute():
                    _, board = _board_path(argument)
                    boards.setdefault(os.path.normcase(str(board)), board)
                    if command["announce"]:
                        announce = Path(command["announce"])
                        if not announce.is_absolute():
                            raise ValueError("CIM announcement is relative; process working directory is unknown")
                        _safe_path(announce)
                        if announce != board.parent / ".viewer.port":
                            add(board, _json_read(announce, budget), f"CIM announcement: {announce}")
                else:
                    _finding(report, "census-viewer-unresolved", "Known runtime viewer lacks an absolute, inspectable --board; cannot establish writer identity", command["runtimeScript"], category="runtime")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, wb.UnsafeBoardPath) as exc:
            _finding(report, "census-viewer-invalid", exc, row.get("pid") if isinstance(row, dict) else "CIM", category="runtime")
            if isinstance(exc, _LimitError):
                raise
    census["knownViewers"] = known
    for board in boards.values():
        announce = board.parent / ".viewer.port"
        try:
            budget.take()
            _safe_path(announce)
            if announce.exists():
                add(board, _json_read(announce, budget), str(announce))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            _finding(report, "announcement-invalid", exc, announce, category="runtime")
            if isinstance(exc, _LimitError):
                raise
    for command in known:
        if not command["inScope"]:
            continue
        matches = [candidate for candidate in candidates if candidate["entry"].get("pid") == command.get("pid")]
        if not matches:
            _finding(report, "unannounced-viewer", "Live known runtime viewer is absent from registry/local announcement; quiesce or establish its identity", command["runtimeScript"], category="runtime")
        else:
            for candidate in matches:
                if "registry" not in candidate["sources"]:
                    _finding(report, "unregistered-viewer", "Live runtime is announced but absent from the viewer registry", candidate["board"], category="runtime", warning=True)
                candidate.setdefault("processes", []).append(command)
                if command.get("boardArgument"):
                    try:
                        _, actual = _board_path(command["boardArgument"])
                        if os.path.normcase(str(actual)) != os.path.normcase(candidate["board"]):
                            raise ValueError("CIM command line disagrees with viewer board identity")
                        if command.get("portArgument") and int(command["portArgument"]) != candidate["entry"].get("port"):
                            raise ValueError("CIM command line disagrees with viewer port")
                    except (OSError, ValueError, TypeError, RuntimeError, wb.UnsafeBoardPath) as exc:
                        _finding(report, "census-identity-mismatch", exc, command["pid"], category="runtime")
    for candidate in candidates:
        candidate.pop("key")
        entry = candidate.pop("entry")
        candidate.update(pid=entry.get("pid"), port=entry.get("port"), registryEntry=entry)
        try:
            budget.take()
            status = wb.viewer_status(entry, Path(candidate["board"]))
            candidate.update(status)
            compatibility = status.get("compatibility")
            if compatibility not in ("compatible", "confirmed-dead"):
                _finding(report, "viewer-" + str(compatibility or "indeterminate"), status.get("reason") or "Viewer compatibility cannot be established", candidate["board"], category="runtime")
            elif compatibility == "confirmed-dead":
                if candidate.get("processes"):
                    candidate["compatibility"] = "indeterminate"
                    _finding(report, "census-liveness-conflict", "CIM saw this runtime process but its PID probe says dead; repeat inventory after quiescence", candidate["board"], category="runtime")
                else:
                    _finding(report, "stale-viewer", "Confirmed-dead viewer evidence retained; doctor never prunes it", candidate["board"], category="runtime", warning=True)
            health = status.get("health")
            if isinstance(health, dict) and (health.get("boardAvailable") is False or health.get("boardError") is not None):
                _finding(report, "viewer-board-unavailable", health.get("boardError") or "Viewer reports its board unavailable", candidate["board"])
            candidate["runtime"] = {key: health.get(key) for key in wb.runtime_info()} if isinstance(health, dict) else None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            candidate.update(compatibility="indeterminate", reason=str(exc))
            _finding(report, "viewer-indeterminate", exc, candidate["board"], category="runtime")
        report["viewers"].append(candidate)
    by_board = {}
    for candidate in candidates:
        if candidate.get("compatibility") != "confirmed-dead":
            by_board.setdefault(candidate["board"], set()).add((candidate.get("pid"), candidate.get("port")))
    for board, identities in by_board.items():
        if len(identities) > 1:
            _finding(report, "viewer-conflict", "Registry/announcement expose multiple potentially-live identities for one board", board, category="runtime")


def inspect_board(board_path, *, registry_home=None, all_registered=False):
    """Inspect explicit data and all known runtime evidence without creating even a lock."""
    report = {"ok": False, "ready": False, "dataReady": False, "runtimeReady": False,
              "blockers": [], "warnings": [], "boards": [], "registry": {}, "viewers": [],
              "allRegistered": bool(all_registered), "operatorGates": list(OPERATOR_GATES),
              "readOnly": True, "liveCutoverAuthorized": False}
    budget, cache = _Budget(), {}
    report["board"] = _inspect_data(board_path, report, budget, cache)
    report["boards"].append(report["board"])
    boards = {}
    if report["board"].get("path"):
        path = Path(report["board"]["path"])
        boards[os.path.normcase(str(path))] = path
    home = Path(registry_home).absolute() if registry_home is not None else None
    registry_path = home / ".workboard" / "boards.json" if home is not None else wb.REGISTRY_PATH
    viewers_path = home / ".workboard" / "viewers.json" if home is not None else wb.VIEWER_REGISTRY
    report["registry"]["boards"], registry = _registry(registry_path, "boards", report, budget)
    report["registry"]["viewers"], viewers = _registry(viewers_path, "viewers", report, budget)
    report["registry"]["entries"] = []
    if registry is not None:
        for name, value in registry.get("boards", {}).items():
            entry = {"name": name, "registeredPath": value, "ok": False}
            report["registry"]["entries"].append(entry)
            try:
                budget.take()
                if not isinstance(name, str) or not name.strip() or not isinstance(value, str):
                    raise ValueError("registry names and board paths must be nonempty strings")
                canonical = wb.canonical_registered_board(value)
                _safe_path(value)
                if not canonical.is_file():
                    raise FileNotFoundError("registered board is missing")
                key = os.path.normcase(str(canonical))
                unseen = key not in boards
                boards[key] = canonical
                entry.update(ok=True, path=str(canonical))
                if all_registered and unseen:
                    report["boards"].append(_inspect_data(value, report, budget, cache))
            except (OSError, ValueError, TypeError, RuntimeError, wb.UnsafeBoardPath) as exc:
                entry["error"] = str(exc)
                _finding(report, "registered-board-invalid", exc, value, category="runtime")
                if isinstance(exc, _LimitError):
                    break
    try:
        _inventory(report, boards, viewers, budget)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        _finding(report, "inventory-incomplete", exc, viewers_path, category="runtime")
    if all_registered:
        validated = {os.path.normcase(item["path"]) for item in report["boards"] if item.get("path")}
        for key, discovered in boards.items():
            if key in validated:
                continue
            try:
                budget.take()
            except _LimitError as exc:
                _finding(report, "board-inventory-incomplete", exc, discovered)
                break
            report["boards"].append(_inspect_data(discovered, report, budget, cache))
            validated.add(key)
    elif len(boards) > 1:
        _finding(report, "selected-data-only", "Other in-scope board paths are inventoried, not data-validated; use --all for replacement evidence", registry_path, warning=True)
    report["dataReady"] = not any(item["category"] == "data" for item in report["blockers"])
    report["runtimeReady"] = not any(item["category"] == "runtime" for item in report["blockers"])
    report["ok"] = report["ready"] = not report["blockers"]
    report["limits"] = {"maxFiles": MAX_FILES, "maxBytes": MAX_TOTAL_BYTES, "maxSeconds": MAX_SECONDS}
    return report


def _manifest(root):
    budget, result = _Budget(), {}
    for path in _files(root, budget, include_dirs=True):
        if path.is_dir():
            result[path.relative_to(root).as_posix()] = {"directory": True}
            continue
        before = path.stat()
        budget.take(before.st_size)
        digest, size = hashlib.sha256(), 0
        with wb.open_shared(path, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if time.monotonic() > budget.deadline:
                    raise ValueError("source hashing exceeded inspection time limit")
                digest.update(chunk)
                if size > before.st_size:
                    raise ValueError(f"source grew during checkpoint: {path}")
        after = path.stat()
        if size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError(f"source changed during checkpoint: {path}")
        result[path.relative_to(root).as_posix()] = {"size": size, "sha256": digest.hexdigest()}
    return result


def _copy_tree(source, destination, expected):
    wb.require_write_scope(destination)
    destination.mkdir()
    for relative, metadata in expected.items():
        original, target = source / relative, destination / relative
        _safe_path(original)
        wb.require_write_scope(target)
        if metadata.get("directory"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with wb.open_shared(original, "rb") as incoming, target.open("xb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                size += len(chunk)
                if size > metadata["size"]:
                    raise ValueError(f"source grew while copying: {original}")
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if {"size": size, "sha256": digest.hexdigest()} != metadata:
            raise ValueError(f"source changed while copying: {original}")


_REHEARSAL_SCRIPT = r'''
import contextlib, hashlib, io, json, sys
from pathlib import Path
import wbcore as wb
import card
board = Path(sys.argv[1])
by = "release-rehearsal"
with wb.board_lock(board):
    baseline = wb.load(board)
    wb.save(board, baseline, by=by)
baseline = wb.load(board)
checkpoint_rev = baseline["rev"]
with wb.board_lock(board):
    doc = wb.load(board)
    wb.check_revision(doc, checkpoint_rev)
    if not doc["cards"]:
        doc["cards"].append(wb.normalize_card({"id": wb.unique_id(doc, "rehearsal"), "num": wb.new_num(doc), "title": "Copy-only rehearsal", "column": "task"}))
    target = doc["cards"][0]
    ref = target["id"]
    comment = wb.comment_action(target, {"type": "add", "text": "Copy-only recovery evidence"}, by)
    wb.save(board, doc, by=by)
data = b"WorkBoard copy-only attachment proof\n"
doc, target, metadata = wb.attachment_add(board, ref, "rehearsal.txt", data, by, mime="text/plain", expected_rev=doc["rev"])
context = wb.card_context(board, ref)
assert any(item["id"] == comment["id"] for item in context["card"]["comments"])
assert any(item["id"] == metadata["id"] for item in context["card"]["attachments"])
_, _, verified, content = wb.attachment_read(board, ref, metadata["id"])
assert content == data and verified["sha256"] == hashlib.sha256(data).hexdigest()
before = board.read_bytes()
blobs_before = set((board.parent / "attachments").iterdir())
try:
    wb.attachment_add(board, ref, "stale.txt", b"must not commit", by, expected_rev=doc["rev"] - 1)
except wb.WorkflowError as exc:
    assert exc.status == 409
else:
    raise AssertionError("stale revision unexpectedly committed")
assert board.read_bytes() == before and set((board.parent / "attachments").iterdir()) == blobs_before
doc, _, _ = wb.attachment_detach(board, ref, metadata["id"], by, expected_rev=doc["rev"])
assert all(item["id"] != metadata["id"] for item in wb.card_context(board, ref)["card"]["attachments"])
assert wb.attachment_verified_bytes(board, metadata) == data
transcript = io.StringIO()
with contextlib.redirect_stdout(transcript):
    card.main(["--board", str(board), "recover", str(checkpoint_rev), "--apply", "--expected-rev", str(doc["rev"])])
restored = wb.load(board)
def logical(document):
    cards = [{key: value for key, value in card.items() if key != "changedRev"} for card in document["cards"]]
    return {**{key: value for key, value in document.items() if key not in ("rev", "savedAt", "savedBy", "nextNum")}, "cards": cards}
assert logical(restored) == logical(baseline), "recovery did not restore logical board state"
assert restored["nextNum"] >= baseline["nextNum"] and restored["rev"] > doc["rev"]
print(json.dumps({"ok": True, "baselineRev": checkpoint_rev, "restoredRev": restored["rev"], "baselineNextNum": baseline["nextNum"], "restoredNextNum": restored["nextNum"], "logicalRecovery": True, "attachmentSha256": verified["sha256"], "commentId": comment["id"], "recoveryOutput": transcript.getvalue(), "operations": ["load", "save/schema-upgrade", "comment", "context", "attachment-add/read/detach", "stale-revision-rejected", "CLI-recover"]}))
'''


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def rehearse(board_path, output_dir):
    """Checkpoint and mutate ONLY an independent copy. Never perform a live cutover."""
    report = {"ok": False, "mode": "copy-only-data-rehearsal", "liveCutoverAuthorized": False,
              "sourceUnchanged": False, "logicalRecovery": False, "blockers": [], "warnings": [],
              "operatorGates": list(OPERATOR_GATES)}
    output = None
    try:
        lexical, source_board = _board_path(board_path)
        requested = Path(output_dir).absolute()
        wb.require_write_scope(requested)
        output_path = _safe_path(requested)
        if os.path.lexists(requested):
            raise ValueError("rehearsal output must not already exist")
        if not output_path.parent.is_dir():
            raise ValueError("rehearsal output parent must already exist")
        source = lexical.parent
        if _overlap(output_path, source_board.parent):
            raise ValueError("rehearsal output overlaps source board storage")
        protected = {wb.REGISTRY_PATH.parent.resolve(), wb.VIEWER_REGISTRY.parent.resolve()}
        if any(_overlap(output_path, path) for path in protected):
            raise ValueError("rehearsal output overlaps managed registry storage")
        if any(part.casefold() in ("attachments", wb.BACKUP_DIR, wb.ARCHIVE_DIR, ".workboard", "board") for part in output_path.parts):
            raise ValueError("rehearsal output is inside managed board/recovery storage")
        evidence = {"blockers": [], "warnings": []}
        validation = _inspect_data(lexical, evidence, _Budget(), {})
        report["warnings"].extend(evidence["warnings"])
        if not validation["ok"]:
            report["blockers"].extend(evidence["blockers"])
            return report
        before = _manifest(source)
        report.update(source=str(source_board), output=str(output_path), sourceManifest=before,
                      sourceValidation=validation)
        wb.require_write_scope(requested)
        _safe_path(requested)
        requested.mkdir()  # Exclusive creation, after every safety gate above.
        output = output_path
        checkpoint = output / "checkpoint"
        copied = output / "copy"
        checkpoint.mkdir()
        copied.mkdir()
        report.update(checkpoint=str(checkpoint / "board"), copiedBoard=str(copied / "board" / source_board.name))
        _copy_tree(source, checkpoint / "board", before)
        if _manifest(source) != before:
            raise ValueError("source changed during checkpoint; stop writers before retrying with a new output")
        if _manifest(checkpoint / "board") != before:
            raise ValueError("checkpoint byte verification failed")
        report["checkpointVerified"] = True
        checkpoint_evidence = {"blockers": [], "warnings": []}
        report["checkpointValidation"] = _inspect_data(
            checkpoint / "board" / source_board.name, checkpoint_evidence, _Budget(), {})
        report["blockers"].extend(checkpoint_evidence["blockers"])
        report["warnings"].extend(checkpoint_evidence["warnings"])
        if not report["checkpointValidation"]["ok"]:
            raise ValueError("byte-verified checkpoint failed data validation; runtime exercise was not started")
        _copy_tree(checkpoint / "board", copied / "board", before)
        home = output / "home"
        home.mkdir()
        env = os.environ.copy()
        env.update(HOME=str(home), USERPROFILE=str(home), WORKBOARD_SCOPE_ROOT=str(output),
                   WORKBOARD_DEFAULT_BOARD=str(copied), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONUTF8="1", WORKBOARD_ACTOR="release-rehearsal", WB_VIEWER="0")
        env.pop("PYTHONOPTIMIZE", None)  # Rehearsal assertions are evidence, never optimized away.
        runtime = Path(__file__).resolve().parent
        completed = subprocess.run([sys.executable, "-B", "-c", _REHEARSAL_SCRIPT,
                                    str(copied / "board" / source_board.name)],
                                   cwd=runtime, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=45, creationflags=0x08000000 if os.name == "nt" else 0)
        report.update(isolatedHome=str(home), exerciseExitCode=completed.returncode,
                      exerciseStderr=completed.stderr.decode("utf-8", errors="replace")[:16000])
        if completed.returncode:
            raise ValueError(f"copy-only runtime exercise failed: {report['exerciseStderr']}")
        exercise = json.loads(completed.stdout.decode("utf-8"))
        report["exercise"] = exercise
        report["logicalRecovery"] = exercise.get("logicalRecovery") is True
        after_report = {"blockers": [], "warnings": []}
        report["copyValidation"] = _inspect_data(copied / "board" / source_board.name, after_report, _Budget(), {})
        report["blockers"].extend(after_report["blockers"])
        report["warnings"].extend(after_report["warnings"])
        report["sourceUnchanged"] = _manifest(source) == before
        report["checkpointVerified"] = _manifest(checkpoint / "board") == before
        if not report["sourceUnchanged"] or not report["checkpointVerified"] or not report["logicalRecovery"]:
            raise ValueError("source stability, retained checkpoint, or logical recovery proof failed")
        report["ok"] = not report["blockers"] and report["copyValidation"]["ok"]
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, wb.UnsafeBoardPath, subprocess.TimeoutExpired) as exc:
        _finding(report, "rehearsal-failed", exc, output_dir)
        if output is not None and report.get("sourceManifest") is not None:
            try:
                report["sourceUnchanged"] = _manifest(Path(report["source"]).parent) == report["sourceManifest"]
            except (OSError, ValueError, RuntimeError) as stability_error:
                _finding(report, "source-stability-unknown", stability_error, report["source"])
    finally:
        if output is not None:
            report["reportPath"] = str(output / "report.json")
            try:
                wb.require_write_scope(output / "report.json")
                _safe_path(output / "report.json")
                with (output / "report.json").open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except (OSError, ValueError) as exc:
                report["ok"] = False
                _finding(report, "report-write-failed", exc, output / "report.json")
    return report
