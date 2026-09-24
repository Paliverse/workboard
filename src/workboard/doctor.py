"""`workboard doctor`: installation health plus read-only board data integrity.

Data checks never create files, locks, or directories. They validate the board
document, recovery snapshots, attachment blobs, and the board registry within
fixed file/byte/time budgets. Blockers exit 1; warnings are advice.
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


def _files(root, budget, depth=0):
    _safe_path(root)
    if depth > 64:
        raise ValueError(f"directory nesting exceeds inspection limit: {root}")
    with os.scandir(root) as entries:
        for entry in entries:
            budget.take()
            path = Path(entry.path)
            _safe_path(path)
            if entry.is_dir(follow_symlinks=False):
                yield from _files(path, budget, depth + 1)
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


def _registry(path, report, budget):
    result = {"path": str(path), "exists": False, "ok": False}
    try:
        _safe_path(path)
        result["exists"] = Path(path).exists()
        if not result["exists"]:
            result["ok"] = True
            return result, {"boards": {}}
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
    """The current board (or every registered board with --all), the registry, and legacy files."""
    budget, cache, seen, directories = _Budget(), {}, set(), {}

    def inspect(value):
        result = _inspect_data(value, report, budget, cache)
        report["boards"].append(result)
        if result.get("path"):
            seen.add(os.path.normcase(result["path"]))
            directories[os.path.normcase(result["path"])] = Path(result["path"]).parent

    explicit = board or os.environ.get("WORKBOARD_DEFAULT_BOARD")
    if explicit:
        inspect(explicit)
    else:
        try:
            found = wb.find_board()
        except FileNotFoundError:
            found = None
        if found is not None:
            inspect(found)
    report["registry"], registry = _registry(wb.registry_path(), report, budget)
    entries = report["registry"]["entries"] = []
    for name, value in (registry or {}).get("boards", {}).items():
        entry = {"name": name, "board": value, "ok": False}
        entries.append(entry)
        try:
            budget.take()
            canonical = wb.canonical_registered_board(value)
            _safe_path(value)
            if not canonical.is_file():
                entry["error"] = "registered board is missing"
                _finding(report, "registered-board-missing",
                         f"board '{name}' is registered but its board.json is gone", value,
                         category="registry", warning=True)
                continue
            entry.update(ok=True, path=str(canonical))
            directories.setdefault(os.path.normcase(str(canonical)), canonical.parent)
            if all_registered and os.path.normcase(str(canonical)) not in seen:
                inspect(value)
        except (OSError, ValueError, TypeError, RuntimeError, wb.UnsafeBoardPath) as exc:
            entry["error"] = str(exc)
            _finding(report, "registered-board-invalid", exc, value, category="registry")
            if isinstance(exc, _LimitError):
                break
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
    if server and server.get("version") != __version__:
        _finding(report, "server-version-mismatch",
                 f"the running server is v{server.get('version')} but this CLI is v{__version__}; "
                 "run: workboard service restart", server.get("port"), category="install", warning=True)


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
        lines.append("  board: none here (pass --board PATH, or --all for every registered board)")
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
