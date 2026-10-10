# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Portable board ZIPs behind core.export_bundle and core.import_bundle.

A bundle holds a `workboard-bundle.json` marker, `board.json`, every referenced
`attachments/<id>` blob and, optionally, `archive/*.json` and `.backups/board-N.json`.
Attachment sizes and SHA-256 digests come from the cards that reference them; the
ZIP CRC covers everything else. Imports treat the ZIP as hostile: a strict member
allowlist and byte, member and JSON-structure caps apply before anything is parsed
or written, and the new board is published only through core.create_board.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path

from . import core as wb

FORMAT, VERSION = "workboard-bundle", 1
MARKER = "workboard-bundle.json"
MAX_MEMBERS = 4096
MAX_TOTAL_BYTES = 512 * 1024 * 1024  # Both the ZIP file and the sum of its uncompressed members.
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_JSON_NODES = 1_000_000  # Upper bound on JSON values, counted before parsing.
MAX_JSON_DEPTH = 64
_NAME = re.compile(r"workboard-bundle\.json|board\.json|attachments/[0-9a-f]{32}"
                   r"|archive/(?!(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])\.)[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.json"
                   r"|\.backups/board-(0|[1-9][0-9]{0,17})\.json")


def _invalid(message, status=422):
    return wb.WorkflowError(f"invalid WorkBoard bundle: {message}", status, "invalid")


def _limit(name):
    return wb.MAX_ATTACHMENT_BYTES if name.startswith("attachments/") else MAX_JSON_BYTES


@contextlib.contextmanager
def _opened(path):
    # ponytail: zipfile parses the whole central directory before the member cap; the file-size cap bounds it.
    if os.path.getsize(path) > MAX_TOTAL_BYTES:
        raise _invalid(f"the ZIP exceeds {MAX_TOTAL_BYTES} bytes", 413)
    try:
        with zipfile.ZipFile(path) as archive:
            yield archive
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, UnicodeDecodeError) as exc:
        raise _invalid(str(exc)) from exc


def _read(archive, info, limit):
    try:
        with archive.open(info) as stream:
            data = stream.read(limit + 1)  # Bounded whatever the header claims; zipfile checks the CRC at EOF.
    except OSError as exc:  # A corrupt header offset seeks outside the file.
        raise _invalid(f"{info.orig_filename!r} is unreadable: {exc}") from exc
    if len(data) > limit:
        raise _invalid(f"{info.orig_filename!r} exceeds {limit} bytes", 413)
    return data


def _json(data, label):
    # Every value but the first in a container follows a comma: a small ZIP must not become a huge object graph.
    if len(data) > MAX_JSON_BYTES or data.count(b"{") + data.count(b"[") + data.count(b",") >= MAX_JSON_NODES:
        raise _invalid(f"{label} exceeds the JSON size limits", 413)
    try:
        value = json.loads(data.decode("utf-8"))
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")  # NaN, Infinity, lone surrogates
    except (ValueError, RecursionError) as exc:
        raise _invalid(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth >= MAX_JSON_DEPTH:
                raise _invalid(f"{label} nests deeper than {MAX_JSON_DEPTH} levels", 413)
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return value


def _cards(name, data):
    """(normalized doc or None, normalized cards) of a board, backup or archive document."""
    raw = _json(data, name)
    if name.startswith("archive/"):
        cards = raw.get("cards", []) if isinstance(raw, dict) and wb.validate_schema(raw) else raw
        if not isinstance(cards, list):
            raise _invalid(f"{name} must hold a card array")
        return None, [wb.normalize_card(card) for card in cards]
    doc = wb.normalize_doc(raw)
    return doc, doc["cards"]


def _scan(archive):
    """Validate every member of an open bundle: (board doc, members, SHA-256 digests, counts)."""
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise _invalid(f"more than {MAX_MEMBERS} members", 413)
    members, folded, total = {}, set(), 0
    for info in infos:
        name = info.orig_filename.replace("\\", "/")
        kind = (info.external_attr >> 16) & 0o170000
        if (not _NAME.fullmatch(name) or name.casefold() in folded
                or kind not in (0, stat.S_IFREG) or info.external_attr & 0x410  # links, dirs, reparse points
                or info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise _invalid(f"unsupported, duplicate, encrypted or special member {name!r}")
        total += info.file_size
        if info.file_size > _limit(name) or total > MAX_TOTAL_BYTES:
            raise _invalid(f"{name!r} exceeds the size limits", 413)
        folded.add(name.casefold())
        members[name] = info
    if MARKER not in members or "board.json" not in members:
        raise _invalid(f"{MARKER} and board.json are required")
    if _json(_read(archive, members[MARKER], MAX_JSON_BYTES), MARKER) != {"format": FORMAT, "version": VERSION}:
        raise _invalid("unsupported bundle format or version")
    doc, cards = _cards("board.json", _read(archive, members["board.json"], MAX_JSON_BYTES))
    references = [(item.get("id"), item.get("size"), item.get("sha256"))
                  for card in cards for item in card["attachments"]]
    blobs, digests = {}, {}
    for name, info in members.items():
        if name in (MARKER, "board.json"):
            continue
        data = _read(archive, info, _limit(name))
        digests[name] = hashlib.sha256(data).hexdigest()
        if name.startswith("attachments/"):
            blobs[name.removeprefix("attachments/")] = (len(data), digests[name])
            continue
        backup = _NAME.fullmatch(name)[1]
        if backup is not None and int(backup) > doc["rev"]:
            raise _invalid(f"{name} is newer than board.json")
        references += [(item.get("id"), item.get("size"), item.get("sha256"))
                       for card in _cards(name, data)[1] for item in card["attachments"]]
    for key, size, digest in references:
        if not isinstance(key, str) or type(size) is not int or blobs.get(key) != (size, digest):
            raise _invalid(f"attachment {key!r} is missing or does not match its recorded size and SHA-256")
    if {key for key, _, _ in references} != set(blobs):
        raise _invalid("it holds attachment bytes that no card references")
    counts = {"cards": len(doc["cards"]), "attachments": len(blobs),
              "archives": sum(name.startswith("archive/") for name in members),
              "backups": sum(name.startswith(".backups/") for name in members)}
    return doc, members, digests, counts


def import_bundle(source, *, name, project, apply=False):
    name, project = wb._validate_board_name(name), Path(project).resolve()
    with _opened(source) as archive:
        doc, members, digests, counts = _scan(archive)
        result = {"ok": True, "applied": apply, "name": name, "project": str(project),
                  "sourceName": doc["name"], "sourceRev": doc["rev"], **counts}
        if not apply:
            wb._require_new_board(wb.registry_load()["boards"], name, project)
            return result
        doc["name"] = name
        for key in ("changeEpoch", "changeJournal", "templates"):
            doc.pop(key, None)

        def populate(folder):
            for member, info in members.items():
                if member in (MARKER, "board.json"):
                    continue
                data = _read(archive, info, _limit(member))
                if hashlib.sha256(data).hexdigest() != digests[member]:
                    raise _invalid(f"{member!r} changed while importing")
                target = folder / member
                target.parent.mkdir(exist_ok=True)
                with open(target, "xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                wb._fsync_parent_directory(target)

        board = wb.create_board(name, project, doc, populate=populate)
    return {**result, "board": str(board), "rev": doc["rev"]}


def _publish(temporary, out):
    """Exclusively create `out` from the finished file; never replace a file that appeared meanwhile."""
    try:
        os.link(temporary, out)
        return
    except FileExistsError:
        raise
    except OSError:
        pass  # No hard links here (FAT, some network shares): copy into an exclusively created file.
    created = False
    try:
        with open(temporary, "rb") as source, open(out, "xb") as target:
            created = True
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        if created:
            out.unlink(missing_ok=True)
        raise


def export_bundle(board_path, destination, *, expected_rev=None, include_archives=True, include_backups=False):
    board_path = wb.require_write_scope(board_path)
    out = wb.require_export_destination(destination, board_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=out.parent, prefix=".bundle-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive, \
                    wb.board_transaction(board_path, expected_rev) as doc:
                archive.writestr(MARKER, json.dumps({"format": FORMAT, "version": VERSION}))
                with wb.open_shared(board_path, "rb") as source:
                    archive.writestr("board.json", source.read())
                files = []
                if include_archives:
                    files += [(f"archive/{path.name}", path)
                              for path in sorted((board_path.parent / wb.ARCHIVE_DIR).glob("*.json"))]
                if include_backups:
                    files += [(f".backups/{path.name}", path) for _, path in wb.list_backups(board_path)]
                attachments = {item.get("id"): item for card in doc["cards"] for item in card["attachments"]}
                for name, path in files:
                    with wb.open_shared(path, "rb") as source:
                        data = source.read()
                    for card in _cards(name, data)[1]:
                        for item in card["attachments"]:
                            attachments.setdefault(item.get("id"), item)
                    archive.writestr(name, data)
                for key, item in attachments.items():
                    archive.writestr(f"attachments/{key}", wb.attachment_verified_bytes(board_path, item))
            stream.flush()
            os.fsync(stream.fileno())
        with _opened(temporary) as archive:  # Never publish a bundle that import would refuse.
            doc, _, _, counts = _scan(archive)
        wb.require_export_destination(out, board_path)
        _publish(temporary, out)
        wb._fsync_parent_directory(out)
    finally:
        os.unlink(temporary)
    return {"ok": True, "board": str(board_path), "name": doc["name"], "rev": doc["rev"],
            "out": str(out), "bytes": out.stat().st_size, **counts}
