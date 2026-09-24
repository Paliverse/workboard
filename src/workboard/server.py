#!/usr/bin/env python3
"""WorkBoard viewer server — stdlib-only, on-demand.

Serves the animated board UI plus the live SSE stream, and accepts browser
mutations (board POST, granular card PATCH, lifecycle PATCH, structure PATCH)
so the web UI can drag, stack, edit, and ship cards. Change detection is a
500ms stat-poll of board.json while at least one browser is connected (one
stat() syscall per tick; nothing runs when nobody is watching).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import queue
import re
import sys
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote


from . import core as wb

ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "web" / "board.html"

_state_lock = threading.Lock()
_clients: list = []
_serve_board: Path | None = None
_serve_name = ""
_discovery_health_cache: dict[str, tuple[tuple, dict]] = {}


def _require_board_present() -> None:
    if not _serve_board.is_file():
        raise FileNotFoundError(_serve_board)


def _doc() -> dict:
    _require_board_present()
    return wb.load(_serve_board)


def _sig() -> tuple:
    try:
        st = _serve_board.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return ()


def _broadcast(event: str, payload: dict) -> None:
    with _state_lock:
        clients = list(_clients)
    data = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
    for q in clients:
        try:
            q.put_nowait(data)
        except Exception:
            pass


def _watcher():
    last = _sig()
    idle_waited = False
    while True:
        with _state_lock:
            active = bool(_clients)
        if not active:
            idle_waited = False
            time.sleep(1.0)
            continue
        if not idle_waited:
            idle_waited = True
            last = _sig()
        time.sleep(0.5)
        cur = _sig()
        if cur != last:
            last = cur
            if not cur:
                _broadcast("board-missing", {"board": str(_serve_board)})
            else:
                try:
                    rev = int(_doc().get("rev") or 0)
                except (SystemExit, OSError, ValueError):
                    _broadcast("board-missing", {"board": str(_serve_board)})
                else:
                    _broadcast("rev-bumped", {"rev": rev})
            _broadcast("resync-required", {})




def _board_health(board_path: Path) -> dict:
    try:
        file_stat = board_path.stat()
        signature = (file_stat.st_mtime_ns, file_stat.st_size)
    except OSError as exc:
        return {"attention": 0, "blocked": 0, "rev": None, "boardError": str(exc)}
    key = str(board_path.resolve())
    cached = _discovery_health_cache.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    try:
        doc = wb.load(board_path)
        health = {
            "attention": sum(1 for card in doc["cards"] if wb.stage_attention(card)),
            "blocked": sum(1 for card in doc["cards"] if card["column"] == "blocked"),
            "rev": int(doc.get("rev") or 0),
        }
    except (SystemExit, OSError, ValueError, KeyError, TypeError) as exc:
        health = {"attention": 0, "blocked": 0, "rev": None, "boardError": str(exc)}
    _discovery_health_cache[key] = (signature, health)
    return health


def _discovery() -> list:
    names = wb.registry_load().get("boards", {})
    viewers = wb._viewers_load()
    out = []
    for name, pathstr in sorted(names.items()):
        try:
            bpath = wb.canonical_registered_board(pathstr)
        except (wb.UnsafeBoardPath, OSError, RuntimeError) as exc:
            out.append({"name": name, "board": pathstr, "live": False, "url": None,
                        "dir": str(Path(pathstr).parent), "boardError": str(exc),
                        "attention": 0, "blocked": 0, "rev": None, "viewerEvidence": []})
            continue
        board_dir = str(bpath.parent.resolve())
        evidence = wb.viewer_evidence(bpath, viewers)
        compatible = [item for item in evidence if item["compatibility"] == "compatible"]
        unsafe = [item for item in evidence if item["compatibility"] in ("incompatible", "indeterminate")]
        entry = compatible[0]["entry"] if compatible else {}
        identities = {(item["entry"]["pid"], item["entry"]["port"]) for item in compatible}
        live = bool(compatible) and not unsafe and len(identities) == 1
        health = _board_health(bpath)
        out.append({"name": name, "board": str(bpath), "live": live,
                    "url": f"http://127.0.0.1:{entry['port']}/"
                           if live and entry.get("port") else None,
                    "dir": board_dir, "viewerEvidence": evidence, **health})
    return out


# ===== browser mutation endpoints =====
# Contract mirrors the pre-reset board_server.py (preserved at git d32b6b4) so
# board.html's fetch layer works unmodified, reimplemented on wbcore's JSON
# document model: load -> baseRev check -> mutate -> wb.save (lock + atomic
# swap + backup). Multi-tab sync rides the existing watcher broadcasts.

MAX_BODY_BYTES = 32 * 1024 * 1024

_WRITE_LOCK = threading.Lock()

_CARD_API = re.compile(r"/api/card/([^/]+)\Z")
_LIFECYCLE_API = re.compile(r"/api/card/([^/]+)/lifecycle\Z")
_COMMENTS_API = re.compile(r"/api/card/([^/]+)/comments\Z")
_ATTACHMENTS_API = re.compile(r"/api/card/([^/]+)/attachments\Z")
_ATTACHMENT_API = re.compile(r"/api/card/([^/]+)/attachments/([^/]+)\Z")


class BodyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message
def _send_board_missing(handler):
    handler._json({"error": "board_missing", "board": str(_serve_board)}, 410)


def _require_local(handler) -> None:
    port = int(handler.server.server_port)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    host = handler.headers.get("Host", "").lower()
    if host not in allowed_hosts:
        raise BodyError(403, "request Host is not this local viewer")
    origin = handler.headers.get("Origin")
    if origin is not None and origin.lower() != f"http://{host}":
        raise BodyError(403, "request Origin is not this local viewer")




def _require_local_json(handler, *, beacon=False) -> None:
    _require_local(handler)
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" and not (beacon and content_type == "text/plain"):
        raise BodyError(415, "Content-Type must be application/json")


def _content_length(handler, maximum: int) -> int:
    values = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or len(values) != 1:
        raise BodyError(400, "one explicit Content-Length and no Transfer-Encoding required")
    if not re.fullmatch(r"[0-9]+", values[0]):
        raise BodyError(400, "invalid content length")
    length = int(values[0])
    if length > maximum:
        raise BodyError(413, f"payload exceeds {maximum} bytes")
    return length


def _read_bytes(handler, length: int) -> bytes:
    previous = handler.connection.gettimeout()
    handler.connection.settimeout(15)
    try:
        raw = handler.rfile.read(length)
    except TimeoutError:
        raise BodyError(408, "request body timed out")
    finally:
        handler.connection.settimeout(previous)
    if len(raw) != length:
        raise BodyError(400, "incomplete request body")
    return raw


def _read_body_handler(handler) -> dict:
    length = _content_length(handler, MAX_BODY_BYTES)
    if length <= 0:
        raise BodyError(400, "empty payload")
    raw = _read_bytes(handler, length)
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError):
        raise BodyError(400, "invalid JSON payload")
    if not isinstance(payload, dict):
        raise BodyError(400, "payload must be a JSON object")
    return payload


def _base_rev(payload: dict, handler) -> int:
    header = handler.headers.get("X-Board-Base-Rev")
    if header is not None:
        try:
            rev = int(header)
            if rev < 0:
                raise ValueError
            return rev
        except ValueError:
            raise BodyError(400, "invalid X-Board-Base-Rev")
    rev = payload.get("baseRev", payload.get("rev"))
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        raise BodyError(400, "invalid baseRev")
    return rev - 1 if "baseRev" not in payload and "rev" in payload else rev




def _saved_meta(doc: dict) -> dict:
    return {"rev": doc["rev"], "savedAt": doc.get("savedAt"),
            "savedBy": doc.get("savedBy")}


def _counts(doc: dict) -> tuple:
    counts = {c["id"]: 0 for c in doc["columns"]}
    for card in doc["cards"]:
        counts[card["column"]] = counts.get(card["column"], 0) + 1
    return counts, len(doc["cards"])


def _apply_position(doc: dict, card: dict, position: dict | None) -> bool:
    """position = {before: cardId|null, after: cardId|null} within card.column."""
    if not isinstance(position, dict):
        return False
    before = position.get("before")
    after = position.get("after")
    if not before and not after:
        return False
    ids = [c["id"] for c in doc["cards"]]
    doc["cards"] = [c for c in doc["cards"] if c["id"] != card["id"]]
    idx = None
    if before:
        idx = next((i for i, c in enumerate(doc["cards"]) if c["id"] == before), None)
    elif after:
        idx = next((i for i, c in enumerate(doc["cards"]) if c["id"] == after), None)
        idx = None if idx is None else idx + 1
    if idx is None:
        col_ids = [c["id"] for c in doc["columns"]]
        last = None
        for i, c in enumerate(doc["cards"]):
            if c["column"] == card["column"]:
                last = i
            elif last is not None and c["column"] in col_ids and c["column"] != card["column"]:
                break
        idx = len(doc["cards"]) if last is None else last + 1
    doc["cards"].insert(idx, card)
    return True


def _apply_document_changes(doc: dict, changes: dict) -> None:
    if not isinstance(changes, dict):
        raise BodyError(400, "changes must be an object")
    if "title" in changes:
        doc["title"] = str(changes["title"])
    if "name" in changes:
        doc["name"] = str(changes["name"])
    for key in ("tagTaxonomy", "activeWork", "activeWorkId"):
        if key in changes:
            doc[key] = changes[key]


def _guard_protected(raw: dict, current: dict | None) -> None:
    """Inspect raw values, before normalization could discard a forged value."""
    defaults = current
    if defaults is None:
        defaults = wb.normalize_card({})
        defaults.update(num=None, createdAt=None, updatedAt=None, history=[])
    for supplied, canonical in [
        *((key, key) for key in wb.PROTECTED_CARD_FIELDS),
        ("lifecycleCycles", "cycles"),
    ]:
        if supplied not in raw or (current is None and canonical == "id"):
            continue  # validate_new_identity owns creation identity validation.
        value, expected = raw[supplied], defaults.get(canonical)
        equal = type(value) is type(expected) and value == expected
        if equal and isinstance(value, (list, dict)):
            equal = json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)
        if not equal:
            raise BodyError(422, f"'{supplied}' is server-owned; use its dedicated action")


def _find_card(doc: dict, reference: str) -> dict:
    try:
        return wb.resolve_ref(doc, unquote(reference))
    except wb.RefError:
        raise BodyError(404, f"no card matching '{reference}'")


def _guard_delete(card: dict, by: str) -> None:
    if card.get("activeOwner") and card["activeOwner"] != by:
        raise BodyError(409, f"card is owned by {card['activeOwner']}; take over before deleting")



_CARD_EDITABLE = frozenset({
    "code", "title", "column", "priority", "tags", "origin", "notes", "writeup",
    "subtasks", "links", "lastTouchedSubtask", "meta", "agentRuns",
})
_SUBTASK_EDITABLE = frozenset({"id", "text", "done", "createdAt", "doneAt", "collapsed"})


def _editable_card(raw: dict, current: dict | None = None) -> dict:
    wb._validate_aliases(raw)
    edits = {key: value for key, value in raw.items() if key in _CARD_EDITABLE}
    if "links" not in raw and "linkedCards" in raw:
        edits["links"] = raw["linkedCards"]
    if "subtasks" in edits:
        by_id = {item["id"]: item for item, _ in wb.iter_subtasks((current or {}).get("subtasks", []))}

        def merge(items):
            if not isinstance(items, list):
                raise BodyError(422, "subtasks must be an array")
            result = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise BodyError(422, "every subtask needs an id")
                retained = by_id.get(item["id"], {})
                result.append({**retained, **{key: value for key, value in item.items()
                                            if key in _SUBTASK_EDITABLE},
                               "children": merge(item.get("children", retained.get("children", [])))})
            return result

        edits["subtasks"] = merge(edits["subtasks"])
    return edits

def _update_card(doc: dict, current: dict, raw: dict, by: str) -> dict:
    _guard_protected(raw, current)
    destination = raw.get("column", current["column"])
    if not isinstance(destination, str) or destination not in wb.column_ids(doc):
        raise BodyError(422, f"unknown column '{destination}'")
    merged = {**current, **_editable_card(raw, current), "column": current["column"]}
    for key in wb.PROTECTED_CARD_FIELDS:
        if key in current:
            merged[key] = current[key]
    merged.pop("lifecycleCycles", None)
    updated = wb.normalize_card(merged)
    current.clear()
    current.update(updated)
    if destination != current["column"]:
        wb.workflow_action(doc, current, "move", {"to": destination}, by)
    wb.touch(current)
    return current


def _op_create_card(doc: dict, op: dict, by: str) -> tuple:
    raw = op.get("card")
    if not isinstance(raw, dict):
        raise BodyError(422, "created card must be an object")
    _guard_protected(raw, None)
    wb.validate_new_identity(doc, raw.get("id"))
    if "column" in raw and (
        not isinstance(raw["column"], str) or raw["column"] not in ("backlog", "task", "inprogress")
    ):
        raise BodyError(422, "create in Backlog, Task, or In Progress; close/block via lifecycle actions")
    card = wb.normalize_card({**_editable_card(raw), "id": raw["id"]})
    if not card["title"].strip():
        raise BodyError(422, "created card needs a title")
    destination = card["column"]
    card["num"] = wb.new_num(doc)
    card["column"] = "task"
    card["outcome"] = None
    card["createdAt"] = card["updatedAt"] = wb.now_iso()
    wb.hist(card, "created", by=by)
    doc["cards"].append(card)
    if destination != "task":
        wb.workflow_action(doc, card, "move", {"to": destination}, by)
    return card, None


def _op_sort_cards(doc: dict, op: dict) -> None:
    mode = op.get("mode")
    if mode != "created-desc":
        raise BodyError(422, f"unsupported sort mode '{mode}'")

    def created_ts(card: dict) -> float:
        value = card.get("createdAt") or card.get("created_at")
        if not value:
            return 0
        try:
            parsed = datetime.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.timestamp()
        except (OSError, OverflowError, ValueError):
            return 0

    column_order = {c["id"]: i for i, c in enumerate(doc["columns"])}
    doc["cards"].sort(key=lambda c: (
        column_order.get(c["column"], 999),
        -created_ts(c),
        -(c.get("num") or 0),
    ))


def _op_update_columns(doc: dict, op: dict) -> None:
    cols = op.get("columns")
    if not isinstance(cols, list) or not cols:
        raise BodyError(422, "columns must be a non-empty array")
    seen = set()
    norm = []
    current = {column["id"]: column for column in doc["columns"]}
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or c["id"] in seen:
            raise BodyError(422, "invalid or duplicate column")
        seen.add(c["id"])
        col = {**current.get(c["id"], {}), "id": str(c["id"]),
               "name": str(c.get("name") or c["id"]), "kind": str(c.get("kind") or "custom")}
        if c.get("wipLimit") is not None:
            if type(c["wipLimit"]) is not int or c["wipLimit"] < 0:
                raise BodyError(422, "wipLimit must be a nonnegative integer or null")
            col["wipLimit"] = c["wipLimit"]
        elif "wipLimit" in c:
            col.pop("wipLimit", None)
        col["stackUnder"] = (
            str(c["stackUnder"]) if c.get("stackUnder") is not None else None
        )
        norm.append(col)
    if seen != wb.CORE_COLUMN_ID_SET:
        raise BodyError(
            422, "columns must be exactly: " + ", ".join(wb.CORE_COLUMN_IDS)
        )
    if op.get("columnMoves"):
        raise BodyError(422, "core columns cannot be removed or remapped")
    wb.flatten_column_stacks(norm)
    doc["columns"] = norm


def _op_lifecycle(doc: dict, card: dict, action: str, details: dict, by: str) -> None:
    if action == "submit":
        if card["column"] != "inprogress":
            raise BodyError(422, "complete requires the card to be In Progress")
        summary = details.get("summary")
        verification = details.get("verification")
        if not isinstance(summary, str) or not isinstance(verification, str) \
                or not summary.strip() or not verification.strip():
            raise BodyError(422, "completion requires summary and verification")
        action = "complete"
        details = {"writeup": f"{summary.strip()}\n\nVerification: {verification.strip()}"}
    wb.workflow_action(doc, card, action, details, by)


def _mutate(handler, payload: dict, base_rev: int, apply_fn):
    """Shared mutation shell: rev check under the board lock, apply, save."""
    by = wb.validate_actor(payload.get("actor", "user"))
    _require_board_present()
    with wb.board_transaction(_serve_board, base_rev) as doc:
        result = apply_fn(doc)
        rev = wb.save(_serve_board, doc, by=by)
    return doc, rev, result


def _send_error(handler, error) -> None:
    if isinstance(error, wb.RevisionConflict):
        return handler._json({"ok": False, "status": 409, "conflict": True,
                              "error": str(error) or "board revision changed", "rev": error.rev}, 409)
    if isinstance(error, (BodyError, wb.WorkflowError)):
        return handler._json({"ok": False, "status": error.status, "error": str(error),
                              "conflict": error.status == 409}, error.status)
    if isinstance(error, wb.RefError):
        return handler._json({"ok": False, "status": 404, "error": str(error)}, 404)
    if isinstance(error, FileNotFoundError):
        return _send_board_missing(handler)
    return handler._json({"error": f"board unavailable: {error}"}, 500)


def _handle_board_post(handler) -> None:
    try:
        _require_local_json(handler, beacon=True)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        if not isinstance(payload.get("cards"), list) or "columns" not in payload:
            raise BodyError(400, "payload must include cards and columns")
        wb.validate_schema(payload)

        def apply_fn(doc):
            current = {card["id"]: card for card in doc["cards"]}
            incoming = {}
            for raw in payload["cards"]:
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise BodyError(422, "every card needs a valid id")
                if raw["id"] in incoming:
                    raise BodyError(422, "duplicate card id")
                incoming[raw["id"]] = raw
                _guard_protected(raw, current.get(raw["id"]))
            if current.keys() - incoming.keys():
                raise BodyError(422, "snapshot cannot delete cards; use delete-card explicitly")
            _op_update_columns(doc, {"columns": payload["columns"]})
            ordered = []
            for card_id, raw in incoming.items():
                if card_id in current:
                    card = _update_card(doc, current[card_id], raw, by)
                else:
                    card, _ = _op_create_card(doc, {"card": raw}, by)
                ordered.append(card)
            doc["cards"] = ordered
            _apply_document_changes(doc, payload)

        doc, rev, _ = _mutate(handler, payload, base_rev, apply_fn)
    except (BodyError, wb.WorkflowError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "document": doc}, 200)


def _handle_card_patch(handler, reference: str) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        card_payload = payload.get("card")
        document = payload.get("document")
        position = payload.get("position")
        if not isinstance(card_payload, dict):
            raise BodyError(400, "invalid card patch")
        if document is not None and (not isinstance(document, dict) or "cards" in document):
            raise BodyError(400, "invalid document")
        if position is not None and not isinstance(position, dict):
            raise BodyError(400, "invalid position")

        def apply_fn(doc):
            card = _update_card(doc, _find_card(doc, reference), card_payload, by)
            positioned = _apply_position(doc, card, position)
            if document:
                _apply_document_changes(doc, document)
            return card, positioned

        doc, rev, (card, positioned) = _mutate(handler, payload, base_rev, apply_fn)
    except (BodyError, wb.WorkflowError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "card": card, "document": doc,
                   "positioned": positioned, "event": "card-updated"}, 200)


def _handle_lifecycle_patch(handler, reference: str) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        action = payload.get("action")
        details = payload.get("details", {})
        if not isinstance(action, str) or not isinstance(details, dict):
            raise BodyError(400, "invalid lifecycle patch")

        def apply_fn(doc):
            card = _find_card(doc, reference)
            _op_lifecycle(doc, card, action, details, by)
            return card

        doc, rev, card = _mutate(handler, payload, base_rev, apply_fn)
    except (BodyError, wb.WorkflowError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "card": card,
                   "document": doc, "event": "card-updated", "action": action}, 200)


def _handle_structure_patch(handler) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        operation = payload.get("operation")
        if not isinstance(operation, dict) or not isinstance(operation.get("type"), str):
            raise BodyError(400, "invalid structural patch")

        def apply_fn(doc):
            op_type = operation["type"]
            card = source_card = None
            if op_type == "create-card":
                card, _ = _op_create_card(doc, operation, by)
            elif op_type == "create-follow-up":
                if "sourceCard" in operation:
                    raise BodyError(422, "use sourceCardId, not a source card snapshot")
                source_id = operation.get("sourceCardId")
                if not isinstance(source_id, str) or not source_id:
                    raise BodyError(422, "create-follow-up needs sourceCardId")
                source_card = _find_card(doc, source_id)
                if source_card["column"] != "done":
                    raise BodyError(422, "follow-up source must be Done")
                card, _ = _op_create_card(doc, operation, by)
                if source_card["id"] not in card["links"]:
                    card["links"].append(source_card["id"])
                if card["id"] not in source_card["links"]:
                    source_card["links"].append(card["id"])
                wb.hist(source_card, "follow-up", by=by, note=f"Created #{card['num']}")
                wb.touch(source_card)
            elif op_type == "delete-card":
                card_id = operation.get("cardId")
                if not isinstance(card_id, str):
                    raise BodyError(422, "delete-card needs cardId")
                card = _find_card(doc, card_id)
                _guard_protected(operation, card)
                if "card" in operation:
                    if not isinstance(operation["card"], dict):
                        raise BodyError(422, "invalid deletion card")
                    _guard_protected(operation["card"], card)
                _guard_delete(card, by)
                doc["cards"].remove(card)
                # Incoming dependencies and recovery attachment bytes deliberately remain.
            elif op_type == "sort-cards":
                _op_sort_cards(doc, operation)
            elif op_type == "update-columns":
                _op_update_columns(doc, operation)
            elif op_type == "update-document":
                _apply_document_changes(doc, operation.get("changes") or {})
            else:
                raise BodyError(422, f"unsupported structure operation '{op_type}'")
            return op_type, card, source_card

        doc, rev, (op_type, card, source_card) = _mutate(handler, payload, base_rev, apply_fn)
    except (BodyError, wb.WorkflowError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    counts, total = _counts(doc)
    handler._json({"ok": True, **_saved_meta(doc), "operation": operation,
                   "card": card, "sourceCard": source_card, "document": doc,
                   "cardCounts": counts, "totalCards": total}, 200)


def _handle_comments_patch(handler, reference: str) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        operation = payload.get("operation")
        if not isinstance(operation, dict):
            raise BodyError(422, "comment operation must be an object")

        def apply_fn(doc):
            card = _find_card(doc, reference)
            comment = wb.comment_action(card, operation, by)
            return card, comment

        doc, rev, (card, comment) = _mutate(handler, payload, base_rev, apply_fn)
    except (BodyError, wb.WorkflowError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "document": doc, "card": card,
                   "comment": comment, "event": "card-updated"})


def _handle_attachment_upload(handler, reference: str, query: str) -> None:
    try:
        _require_local(handler)
        length = _content_length(handler, wb.MAX_ATTACHMENT_BYTES)
        base_rev = _base_rev({}, handler)
        by = wb.validate_actor(unquote(handler.headers.get("X-WorkBoard-Actor", "user")))
        names = parse_qs(query, keep_blank_values=True).get("name", [])
        if len(names) != 1 or not names[0]:
            raise BodyError(422, "one nonempty attachment name is required")
        _require_board_present()
        data = _read_bytes(handler, length)
        doc, card, metadata = wb.attachment_add(
            _serve_board, unquote(reference), names[0], data, by,
            mime=handler.headers.get("Content-Type"), expected_rev=base_rev, card_scoped=False)
    except (BodyError, wb.WorkflowError, wb.RefError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "document": doc, "card": card,
                   "attachment": metadata, "event": "card-updated"})




def _handle_attachment_get(handler, reference: str, attachment_id: str) -> None:
    try:
        _require_board_present()
        doc, card, item, body = wb.attachment_read(
            _serve_board, unquote(reference), unquote(attachment_id))
        name = item["name"]
        fallback = re.sub(r'[^A-Za-z0-9._ -]', "_", name).strip(" .") or "download"
        disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
    except (BodyError, wb.WorkflowError, wb.RefError, SystemExit, OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._send(200, body, "application/octet-stream", {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    })


def _handle_attachment_delete(handler, reference: str, attachment_id: str) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        _require_board_present()

        doc, card, item = wb.attachment_detach(
            _serve_board, unquote(reference), unquote(attachment_id), by, expected_rev=base_rev,
            card_scoped=False)
    except (BodyError, wb.WorkflowError, wb.RefError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "document": doc, "card": card,
                   "event": "card-updated"})


def _handle_projection(handler, kind: str) -> None:
    try:
        doc = _doc()
        data = {"cards": wb.ready_cards(doc)} if kind == "ready" else {"stats": wb.board_stats(doc)}
    except (wb.WorkflowError, SystemExit, OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"rev": doc["rev"], **data})


def _git_state() -> dict:
    root = _serve_board.parent.parent.resolve()
    result = {
        "state": "error", "root": str(root), "branch": None, "head": None,
        "subject": None, "ahead": None, "behind": None, "staged": None, "unstaged": None,
        "untracked": None, "conflicted": None, "files": [], "truncated": False, "error": None,
        "warning": "Submodule worktree contents are not inspected; staged gitlink changes may still appear.",
    }
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C",
               GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
    deadline = time.monotonic() + 8

    def run(*args):
        return subprocess.run(
            ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", "-c", "log.showSignature=false", *args],
            cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=max(0.1, deadline - time.monotonic()), shell=False,
            creationflags=0x08000000 if os.name == "nt" else 0)

    try:
        probe = run("rev-parse", "--show-toplevel")
        if probe.returncode:
            message = probe.stderr.decode("utf-8", "replace").strip()
            result["state"] = "not_repo" if "not a git repository" in message.lower() else "error"
            result["error"] = message[:2000] or "Git could not inspect this project"
            return result
        git_root = Path(os.fsdecode(probe.stdout.rstrip(b"\r\n"))).resolve()
        if os.path.normcase(str(git_root)) != os.path.normcase(str(root)):
            result.update(state="not_repo", error="This project has no repository of its own")
            return result
        filters = run("config", "--includes", "--null", "--get-regexp",
                      r"^filter\..*\.(clean|process)$")
        if filters.returncode not in (0, 1):
            result["error"] = filters.stderr.decode("utf-8", "replace").strip()[:2000]
            return result
        if any(record.partition(b"\n")[2].strip() for record in filters.stdout.split(b"\0")):
            result["error"] = (
                "Git status cannot be computed read-only: this repository configures external "
                "clean/process filters. No filter commands were executed."
            )
            return result
        status = run("status", "--porcelain=v2", "-z", "--branch",
                     "--untracked-files=all", "--ignore-submodules=all")
        if status.returncode:
            result["error"] = status.stderr.decode("utf-8", "replace").strip()[:2000]
            return result
        result.update(staged=0, unstaged=0, untracked=0, conflicted=0)
        records = iter(status.stdout.split(b"\0"))
        for record in records:
            if not record:
                continue
            if record.startswith(b"# branch.oid "):
                head = record[13:].decode("ascii", "replace")
                result["head"] = None if head == "(initial)" else head
            elif record.startswith(b"# branch.head "):
                branch = record[14:].decode("utf-8", "replace")
                result["branch"] = None if branch == "(detached)" else branch
            elif record.startswith(b"# branch.ab "):
                ahead, behind = record[12:].split()
                result["ahead"], result["behind"] = int(ahead), -int(behind)
            elif record.startswith(b"# "):
                continue
            else:
                kind = record[:1]
                if kind == b"?":
                    index = worktree = "?"
                    path = record[2:]
                    result["untracked"] += 1
                elif kind in (b"1", b"2", b"u"):
                    fields = record.split(b" ", {b"1": 8, b"2": 9, b"u": 10}[kind])
                    index, worktree = fields[1].decode("ascii")
                    path = fields[-1]
                    result["staged"] += index != "."
                    result["unstaged"] += worktree != "."
                    result["conflicted"] += kind == b"u"
                    if kind == b"2":
                        next(records)  # Rename/copy source path, not a second changed file.
                else:
                    continue
                if len(result["files"]) < 200:
                    result["files"].append({
                        "path": path.decode("utf-8", "replace"), "index": index, "worktree": worktree})
                else:
                    result["truncated"] = True
        if result["head"]:
            commit = run("log", "-1", "--format=%s", result["head"], "--")
            if commit.returncode:
                result["error"] = commit.stderr.decode("utf-8", "replace").strip()[:2000]
                return result
            result["subject"] = commit.stdout.decode("utf-8", "replace").rstrip("\r\n")
        result["state"] = "dirty" if any(result[k] for k in (
            "staged", "unstaged", "untracked", "conflicted")) else "clean"
    except FileNotFoundError:
        result.update(state="unavailable", error="Git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        result.update(state="error", error="Git inspection timed out")
    except (OSError, ValueError, IndexError, StopIteration) as e:
        result.update(state="error", error=f"Git inspection failed: {e}")
    return result


def _handle_card_get(handler, reference: str) -> None:
    try:
        from urllib.parse import unquote
        reference = unquote(reference)
        doc = _doc()
        card = wb.resolve_ref(doc, reference)
    except FileNotFoundError:
        return _send_board_missing(handler)
    except SystemExit as e:
        handler._json({"error": str(e)}, 500)
        return
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        handler._json({"error": f"board unavailable: {e}"}, 500)
        return
    except wb.RefError:
        handler._json({"error": f"no card matching '{reference}'"}, 404)
        return
    handler._json({"card": card, "rev": doc.get("rev")}, 200)


def _handle_cards_page(handler, query: str) -> None:
    from urllib.parse import parse_qs
    qs = parse_qs(query)
    column = (qs.get("column") or [""])[0]
    try:
        offset = max(0, int((qs.get("offset") or ["0"])[0]))
        limit = min(250, max(1, int((qs.get("limit") or ["50"])[0])))
    except ValueError:
        handler._json({"error": "invalid offset/limit"}, 400)
        return
    try:
        doc = _doc()
    except FileNotFoundError:
        return _send_board_missing(handler)
    except SystemExit as e:
        handler._json({"error": str(e)}, 500)
        return
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        handler._json({"error": f"board unavailable: {e}"}, 500)
        return
    in_col = [c for c in doc["cards"] if c["column"] == column]
    handler._json({"column": column, "cards": in_col[offset:offset + limit],
                   "total": len(in_col), "rev": doc.get("rev")}, 200)
def _handle_viewer_delete(handler) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        name = payload.get("name")
        expected_board = payload.get("board")
        if not isinstance(name, str) or not name:
            raise BodyError(400, "invalid board name")
        if not isinstance(expected_board, str) or not expected_board:
            raise BodyError(400, "invalid board identity")
        if "baseRev" not in payload:
            raise BodyError(400, "baseRev is required")
        base_rev = payload["baseRev"]
        if (base_rev is not None
                and (isinstance(base_rev, bool) or not isinstance(base_rev, int)
                     or base_rev < 0)):
            raise BodyError(400, "invalid baseRev")
        with _WRITE_LOCK:
            result = wb.delete_registered_board(name, expected_board, base_rev)
    except BodyError as e:
        return handler._json({"error": e.message}, e.status)
    except wb.RegistryNotFound:
        return handler._json({"error": f"no registered board '{name}'"}, 404)
    except (wb.RegistryConflict, wb.UnsafeBoardPath) as e:
        return handler._json(
            {"ok": False, "conflict": True, "error": str(e)}, 409)
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        return handler._json({"error": f"delete failed: {e}"}, 500)
    current = (
        os.path.normcase(str(_serve_board.resolve(strict=False)))
        == os.path.normcase(result["board"])
    )
    handler._json({"ok": True, "deleted": name,
                   "recoveryPath": result["recoveryPath"], "current": current})




def _handle_viewer_start(handler) -> None:
    try:
        _require_local_json(handler)
        payload = _read_body_handler(handler)
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise BodyError(400, "board name is required")
        with _WRITE_LOCK:
            entry = wb.ensure_registered_viewer(name)
    except BodyError as e:
        return _send_error(handler, e)
    except wb.RegistryNotFound:
        return handler._json({"error": f"no registered board '{name}'"}, 404)
    except wb.WorkflowError as e:
        return _send_error(handler, e)
    except FileNotFoundError:
        return handler._json({"error": "board_missing"}, 410)
    except (wb.RegistryConflict, wb.UnsafeBoardPath) as e:
        return handler._json({"error": str(e), "conflict": True}, 409)
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        return handler._json({"error": f"viewer start failed: {e}"}, 503)
    if entry and entry.get("port"):
        return handler._json({"ok": True, "url": f"http://127.0.0.1:{entry['port']}/"})
    return handler._json({"error": "viewer did not start"}, 503)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if code >= 400:
            self.close_connection = True  # Rejected bodies must not become the next request.
            self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/board.json":
            return _handle_board_post(self)
        if path == "/viewers/delete":
            return _handle_viewer_delete(self)
        if path == "/viewers/start":
            return _handle_viewer_start(self)
        match = _ATTACHMENTS_API.match(path)
        if match:
            return _handle_attachment_upload(self, match.group(1), query)
        self._json({"error": "not found"}, 404)

    def do_PATCH(self):
        path = self.path.split("?")[0]
        if path == "/api/structure":
            return _handle_structure_patch(self)
        match = _CARD_API.match(path)
        if match:
            return _handle_card_patch(self, match.group(1))
        match = _LIFECYCLE_API.match(path)
        if match:
            return _handle_lifecycle_patch(self, match.group(1))
        match = _COMMENTS_API.match(path)
        if match:
            return _handle_comments_patch(self, match.group(1))
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        match = _ATTACHMENT_API.match(self.path.split("?")[0])
        if match:
            return _handle_attachment_delete(self, match.group(1), match.group(2))
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        try:
            _require_local(self)
        except BodyError as e:
            return _send_error(self, e)
        path, _, query = self.path.partition("?")
        path = path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                body = HTML_PATH.read_bytes()
            except OSError:
                return self._json({"error": "board.html missing"}, 500)
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/board.json" or path == "/api/bootstrap":
            try:
                doc = _doc()
            except FileNotFoundError:
                return _send_board_missing(self)
            except (SystemExit, OSError, ValueError, KeyError, TypeError) as e:
                return _send_error(self, e)
            if path == "/api/bootstrap":
                return self._json({"state": doc})
            return self._json(doc)
        if path == "/rev":
            try:
                doc = _doc()
            except FileNotFoundError:
                return _send_board_missing(self)
            except (SystemExit, OSError, ValueError, KeyError, TypeError) as e:
                return _send_error(self, e)
            return self._send(200, str(doc.get("rev", 0)).encode(),
                              "text/plain; charset=utf-8")
        if path == "/health":
            board_error = None
            try:
                doc = _doc()
            except (SystemExit, OSError, ValueError, KeyError, TypeError) as exc:
                doc = None
                board_error = {"error": str(exc), "status": getattr(exc, "status", 500)}
            registry_errors = {}
            for label, reader in (("boards", wb.registry_load), ("viewers", wb._viewers_load)):
                try:
                    reader()
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    registry_errors[label] = str(exc)
            with _state_lock:
                n = len(_clients)
            name = doc.get("name") if doc else _serve_name
            return self._json({
                **wb.runtime_info(), "boardError": board_error, "registryErrors": registry_errors,
                "ok": True, "pid": os.getpid(), "projectId": name, "name": name,
                "board": str(_serve_board), "boardAvailable": doc is not None,
                "rev": doc.get("rev") if doc else None,
                "cards": len(doc["cards"]) if doc else None,
                "sseClients": n, "nowMs": int(time.time() * 1000),
            })
        if path == "/api/projects":
            try:
                reg = wb.registry_load()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _send_error(self, exc)
            boards = [{"id": k, "name": k} for k in sorted(reg.get("boards", {}))]
            return self._json({"projects": boards})
        if path == "/viewers":
            current_dir = str(_serve_board.parent.resolve(strict=False))
            boards = []
            current_name = ""
            try:
                discovered = _discovery()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _send_error(self, exc)
            for b in discovered:
                is_cur = b.pop("dir") == current_dir
                b["current"] = is_cur
                if is_cur:
                    current_name = b["name"]
                boards.append(b)
            return self._json({"current": current_name, "boards": boards,
                               "boardAvailable": _serve_board.is_file()})
        if path == "/viewers/start":
            return self._json({"error": "use same-origin JSON POST to start a viewer"}, 405)
        if path == "/events":
            return self._sse()
        if path in ("/api/ready", "/api/stats"):
            return _handle_projection(self, path.rsplit("/", 1)[-1])
        if path == "/api/git":
            if not _serve_board.is_file():
                return _send_board_missing(self)
            return self._json(_git_state())
        match = _ATTACHMENT_API.match(path)
        if match:
            return _handle_attachment_get(self, match.group(1), match.group(2))
        context_match = re.fullmatch(r"/api/card/([^/]+)/context", path)
        if context_match:
            try:
                _require_board_present()
                return self._json(wb.card_context(_serve_board, unquote(context_match.group(1))))
            except (wb.RefError, OSError, ValueError, KeyError, TypeError) as exc:
                return _send_error(self, exc)
        match = _CARD_API.match(path)
        if match:
            return _handle_card_get(self, match.group(1))
        if path == "/api/cards":
            return _handle_cards_page(self, query)
        self._json({"error": "not found"}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = queue.Queue(maxsize=256)
        with _state_lock:
            _clients.append(q)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15.0)
                except Exception:
                    data = ": keepalive\n\n"
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _state_lock:
                if q in _clients:
                    _clients.remove(q)




def _start_server(port):
    """An explicit port is an exact contract, never a hint."""
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise wb.WorkflowError("port must be an integer 1..65535")
        return Server(("127.0.0.1", port), Handler), port
    for candidate in range(7891, 7941):
        try:
            return Server(("127.0.0.1", candidate), Handler), candidate
        except OSError:
            continue
    raise wb.WorkflowError("no free viewer port in 7891..7940", 503)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def main(args):
    global _serve_board, _serve_name
    explicit = args.board or os.environ.get("WORKBOARD_DEFAULT_BOARD")
    if explicit:
        selected = Path(explicit).absolute()
        _serve_board = selected / "board" / "board.json" if selected.is_dir() else selected
    else:
        _serve_board = wb.find_board()
    _serve_board = wb.require_write_scope(_serve_board)
    if not _serve_board.parent.is_dir():
        raise FileNotFoundError(f"no board directory at {_serve_board.parent}")
    board_announce = wb.require_write_scope(_serve_board.parent / ".viewer.port")
    requested_announce = getattr(args, "announce", None)
    if requested_announce:
        wb.require_write_scope(requested_announce)
    wb.require_write_scope(wb.REGISTRY_PATH)
    wb.require_write_scope(wb.VIEWER_REGISTRY)
    wb.registry_load()
    wb._viewers_load()
    if _serve_board.is_file():
        wb.load(_serve_board)
    httpd = None

    def start():
        nonlocal httpd
        with wb.board_lock(_serve_board):
            existing = wb.viewer_start_gate(_serve_board, own_pid=os.getpid())
            if existing:
                raise wb.WorkflowError(
                    f"viewer already running at http://127.0.0.1:{existing['port']}/; reuse it instead", 409)
            doc = wb.load(_serve_board) if _serve_board.is_file() else None
            httpd, port = _start_server(args.port)
            info = {"pid": os.getpid(), "port": port, **wb.runtime_info()}
            wb._atomic_write_json(board_announce, info)
            if requested_announce:
                if Path(requested_announce).absolute() != board_announce:
                    wb._atomic_write_json(Path(requested_announce), info)
            else:
                wb.register_viewer(_serve_board, info["pid"], info["port"])
            return doc, port

    try:
        if requested_announce:
            # The spawning parent holds the lifecycle lock; the board lock also
            # serializes independently launched --announce viewers.
            doc, port = start()
        else:
            wb.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with wb.board_lock(wb.REGISTRY_PATH):
                doc, port = start()
    except BaseException:
        if httpd is not None:
            httpd.server_close()
        raise
    url = f"http://127.0.0.1:{port}/"
    _serve_name = doc.get("name") if doc else _serve_board.parent.parent.name
    if sys.stdout is not None:
        if getattr(args, "json", False):
            message = json.dumps({"ok": True, "board": str(_serve_board), "url": url,
                                  "pid": os.getpid(), "port": port, "rev": doc["rev"] if doc else None,
                                  **wb.runtime_info()})
        elif doc:
            message = f"serving '{_serve_name}' — {url}  ({len(doc['cards'])} cards, Ctrl+C to stop)"
        else:
            message = f"serving board chooser — {url}  (board missing, Ctrl+C to stop)"
        print(message, flush=True)
    threading.Thread(target=_watcher, daemon=True).start()
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if sys.stdout is not None and not getattr(args, "json", False):
            print("\nstopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="serve.py", allow_abbrev=False)
    ap.add_argument("--board")
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--announce")
    try:
        main(ap.parse_args())
    except (wb.WorkflowError, wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"error: {exc}")
