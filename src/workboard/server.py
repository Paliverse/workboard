# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""WorkBoard local server: one per-user process that serves every registered board.

Stdlib only, bound to 127.0.0.1. Root routes: ``/health``, ``/`` (board chooser),
``/api/boards``, ``POST /api/boards/delete`` and ``POST /api/shutdown``. Each
registered board lives under ``/b/<name>/`` with the board UI and its per-board
API; the board is resolved from the registry on every request, so boards
registered after start-up are served immediately. Change detection is a 500ms
stat-poll of each board that has at least one SSE subscriber; boards nobody
watches are never polled.
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from . import __version__
from . import core as wb

DEFAULT_PORT = 7891
LOG_ROTATE_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024

_BOARD_ROUTE = re.compile(r"/b/([^/]+)(/.*)?\Z")
_CARD_API = re.compile(r"/api/card/([^/]+)\Z")
_LIFECYCLE_API = re.compile(r"/api/card/([^/]+)/lifecycle\Z")
_COMMENTS_API = re.compile(r"/api/card/([^/]+)/comments\Z")
_CONTEXT_API = re.compile(r"/api/card/([^/]+)/context\Z")
_ATTACHMENTS_API = re.compile(r"/api/card/([^/]+)/attachments\Z")
_ATTACHMENT_API = re.compile(r"/api/card/([^/]+)/attachments/([^/]+)\Z")


# ===== ports, URLs and lifecycle helpers =====

def _configured_port() -> int:
    raw = os.environ.get("WORKBOARD_PORT") or str(DEFAULT_PORT)
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise wb.WorkflowError(f"WORKBOARD_PORT must be an integer 1..65535, not {raw!r}")
    return port


def server_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{_configured_port() if port is None else port}/"


def board_url(name: str, port: int | None = None) -> str:
    return f"{server_url(port)}b/{quote(name, safe='')}/"


def _state() -> dict:
    # FILE_SHARE_DELETE on Windows: a poller must never block the exiting server's delete.
    try:
        with wb.open_shared(wb.server_state_path()) as stream:
            state = json.loads(stream.read())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _request(port: int, method: str, path: str, timeout: float,
             headers: dict | None = None) -> tuple[int, bytes] | None:
    """Loopback HTTP without proxies; None when nothing answers."""
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _health_at(port: int, timeout: float = 1.0) -> dict | None:
    answer = _request(port, "GET", "/health", timeout)
    if answer is None or answer[0] != 200:
        return None
    try:
        info = json.loads(answer[1])
    except ValueError:
        return None
    return info if isinstance(info, dict) and info.get("app") == "workboard" else None


def server_info(timeout: float = 1.0) -> dict | None:
    """The running server's /health, found through server.json, else the configured port."""
    port = _state().get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        port = _configured_port()
    return _health_at(port, timeout)


def stop(timeout: float = 10.0) -> bool:
    """Ask the running server to exit with its token; True if it stopped or none ran."""
    info = server_info()
    if info is None:
        return True
    state = _state()
    token = state.get("token") if state.get("pid") == info.get("pid") else None
    if not isinstance(token, str):
        return False
    port = info["port"]
    _request(port, "POST", "/api/shutdown", timeout, {"X-WorkBoard-Token": token})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_at(port, 0.5) is None and _state().get("pid") != info["pid"]:
            return True
        time.sleep(0.1)
    return False


def start_background(timeout: float = 10.0) -> dict:
    """Return the running server's info, starting the installed service command if needed."""
    info = server_info()
    if info:
        return info
    from . import install
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": str(Path.home())}
    if os.name == "nt":
        options["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(install.server_command(), **options)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = server_info()
        if info:
            return info
        # Exit 0 is a hand-off (runtime-copy relaunch or an already-running server).
        if process.poll() not in (None, 0):
            break
        time.sleep(0.1)
    raise wb.WorkflowError(
        f"server did not start within {timeout:g}s; see {wb.logs_dir() / 'server.log'}", 503, "state")


def _local_board(args) -> Path | None:
    """The board named by --board (errors surface), else the one at/above cwd, else None."""
    if getattr(args, "board", None):
        return wb.find_board(args.board)
    try:
        return wb.find_board()
    except FileNotFoundError:
        return None


def _board_name(board: Path, register: bool) -> str | None:
    """Registered name for this board file; optionally register it under a free name."""
    target = os.path.normcase(str(board.resolve()))
    boards = wb.registry_load()["boards"]
    for name, value in sorted(boards.items()):
        if os.path.normcase(str(Path(value).resolve(strict=False))) == target:
            return name
    if not register:
        return None
    base = wb.load(board).get("name") or board.parent.parent.name
    name, suffix = base, 2
    while name in boards:
        name, suffix = f"{base}-{suffix}", suffix + 1
    wb.register_board(name, board.resolve())
    return name


def open_board(args) -> None:
    board = _local_board(args)
    name = _board_name(board, register=True) if board else None
    port = start_background()["port"]
    url = board_url(name, port) if name else server_url(port)
    import webbrowser
    webbrowser.open(url)
    if args.json:
        print(json.dumps({"ok": True, "url": url, "board": str(board) if board else None,
                          "name": name}, ensure_ascii=False))
    else:
        print(f"opened {url}")


# ===== per-board helpers =====

def _require_board_present(board: Path) -> None:
    if not board.is_file():
        raise FileNotFoundError(board)


def _doc(board: Path) -> dict:
    _require_board_present(board)
    return wb.load(board)


def _sig(board: Path) -> tuple:
    try:
        st = board.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return ()


class UnknownBoard(LookupError):
    pass


def _registered_board(name: str) -> Path:
    value = wb.registry_load()["boards"].get(name)
    if value is None:
        raise UnknownBoard(name)
    return wb.canonical_registered_board(value)


# ===== browser mutation endpoints =====
# Contract mirrors the preview's per-board server so board.html's fetch layer is
# unchanged: load -> baseRev check -> mutate -> wb.save (lock + atomic swap +
# backup). Multi-tab sync rides the per-board watcher broadcasts.


class BodyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _send_board_missing(handler):
    handler._json({"error": "board_missing", "board": str(handler.board)}, 410)


def _require_local(handler) -> None:
    port = int(handler.server.server_port)
    host = handler.headers.get("Host", "").lower()
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
        raise BodyError(403, "request Host is not this local server")
    origin = handler.headers.get("Origin")
    if origin is not None and origin.lower() not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
        raise BodyError(403, "request Origin is not this local server")


def _require_json(handler, *, beacon=False) -> None:
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
    _require_board_present(handler.board)
    with wb.board_transaction(handler.board, base_rev) as doc:
        result = apply_fn(doc)
        rev = wb.save(handler.board, doc, by=by)
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
        _require_json(handler, beacon=True)
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
        _require_json(handler)
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
        _require_json(handler)
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
        _require_json(handler)
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
        _require_json(handler)
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
        length = _content_length(handler, wb.MAX_ATTACHMENT_BYTES)
        base_rev = _base_rev({}, handler)
        by = wb.validate_actor(unquote(handler.headers.get("X-WorkBoard-Actor", "user")))
        names = parse_qs(query, keep_blank_values=True).get("name", [])
        if len(names) != 1 or not names[0]:
            raise BodyError(422, "one nonempty attachment name is required")
        _require_board_present(handler.board)
        data = _read_bytes(handler, length)
        doc, card, metadata = wb.attachment_add(
            handler.board, unquote(reference), names[0], data, by,
            mime=handler.headers.get("Content-Type"), expected_rev=base_rev, card_scoped=False)
    except (BodyError, wb.WorkflowError, wb.RefError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "document": doc, "card": card,
                   "attachment": metadata, "event": "card-updated"})


def _handle_attachment_get(handler, reference: str, attachment_id: str) -> None:
    try:
        _require_board_present(handler.board)
        doc, card, item, body = wb.attachment_read(
            handler.board, unquote(reference), unquote(attachment_id))
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
        _require_json(handler)
        payload = _read_body_handler(handler)
        base_rev = _base_rev(payload, handler)
        by = wb.validate_actor(payload.get("actor", "user"))
        _require_board_present(handler.board)
        doc, card, item = wb.attachment_detach(
            handler.board, unquote(reference), unquote(attachment_id), by, expected_rev=base_rev,
            card_scoped=False)
    except (BodyError, wb.WorkflowError, wb.RefError, SystemExit, wb.LockTimeout,
            OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"ok": True, **_saved_meta(doc), "document": doc, "card": card,
                   "event": "card-updated"})


def _handle_projection(handler, kind: str) -> None:
    try:
        doc = _doc(handler.board)
        data = {"cards": wb.ready_cards(doc)} if kind == "ready" else {"stats": wb.board_stats(doc)}
    except (wb.WorkflowError, SystemExit, OSError, ValueError, KeyError, TypeError) as e:
        return _send_error(handler, e)
    handler._json({"rev": doc["rev"], **data})


def _git_state(board: Path) -> dict:
    root = board.parent.parent.resolve()
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
    reference = unquote(reference)
    try:
        doc = _doc(handler.board)
        card = wb.resolve_ref(doc, reference)
    except FileNotFoundError:
        return _send_board_missing(handler)
    except SystemExit as e:
        return handler._json({"error": str(e)}, 500)
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        return handler._json({"error": f"board unavailable: {e}"}, 500)
    except wb.RefError:
        return handler._json({"error": f"no card matching '{reference}'"}, 404)
    handler._json({"card": card, "rev": doc.get("rev")}, 200)


def _handle_cards_page(handler, query: str) -> None:
    qs = parse_qs(query)
    column = (qs.get("column") or [""])[0]
    try:
        offset = max(0, int((qs.get("offset") or ["0"])[0]))
        limit = min(250, max(1, int((qs.get("limit") or ["50"])[0])))
    except ValueError:
        return handler._json({"error": "invalid offset/limit"}, 400)
    try:
        doc = _doc(handler.board)
    except FileNotFoundError:
        return _send_board_missing(handler)
    except SystemExit as e:
        return handler._json({"error": str(e)}, 500)
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        return handler._json({"error": f"board unavailable: {e}"}, 500)
    in_col = [c for c in doc["cards"] if c["column"] == column]
    handler._json({"column": column, "cards": in_col[offset:offset + limit],
                   "total": len(in_col), "rev": doc.get("rev")}, 200)


def _handle_board_get(handler, path: str, query: str) -> None:
    if path in ("/board.json", "/api/bootstrap", "/rev"):
        try:
            doc = _doc(handler.board)
        except (SystemExit, OSError, ValueError, KeyError, TypeError) as e:
            return _send_error(handler, e)
        if path == "/rev":
            return handler._send(200, str(doc.get("rev", 0)).encode(), "text/plain; charset=utf-8")
        return handler._json({"state": doc} if path == "/api/bootstrap" else doc)
    if path == "/events":
        return handler._sse()
    if path in ("/api/ready", "/api/stats"):
        return _handle_projection(handler, path.rsplit("/", 1)[-1])
    if path == "/api/git":
        if not handler.board.is_file():
            return _send_board_missing(handler)
        return handler._json(_git_state(handler.board))
    if path == "/api/cards":
        return _handle_cards_page(handler, query)
    match = _ATTACHMENT_API.match(path)
    if match:
        return _handle_attachment_get(handler, match.group(1), match.group(2))
    match = _CONTEXT_API.match(path)
    if match:
        try:
            _require_board_present(handler.board)
            return handler._json(wb.card_context(handler.board, unquote(match.group(1))))
        except (wb.RefError, OSError, ValueError, KeyError, TypeError) as exc:
            return _send_error(handler, exc)
    match = _CARD_API.match(path)
    if match:
        return _handle_card_get(handler, match.group(1))
    handler._json({"error": "not found"}, 404)


def _handle_board_write(handler, method: str, path: str, query: str) -> None:
    if method == "POST":
        if path == "/board.json":
            return _handle_board_post(handler)
        match = _ATTACHMENTS_API.match(path)
        if match:
            return _handle_attachment_upload(handler, match.group(1), query)
    elif method == "PATCH":
        if path == "/api/structure":
            return _handle_structure_patch(handler)
        for pattern, handle in ((_CARD_API, _handle_card_patch),
                                (_LIFECYCLE_API, _handle_lifecycle_patch),
                                (_COMMENTS_API, _handle_comments_patch)):
            match = pattern.match(path)
            if match:
                return handle(handler, match.group(1))
    elif method == "DELETE":
        match = _ATTACHMENT_API.match(path)
        if match:
            return _handle_attachment_delete(handler, match.group(1), match.group(2))
    handler._json({"error": "not found"}, 404)


# ===== server-level endpoints =====

def _health(server) -> dict:
    try:
        boards, registry_error = len(wb.registry_load()["boards"]), None
    except (OSError, ValueError) as exc:
        boards, registry_error = None, str(exc)
    return {**wb.runtime_info(), "ok": True, "app": "workboard", "pid": os.getpid(),
            "port": server.server_port, "startedAt": server.started_at, "boards": boards,
            "registryError": registry_error, "sseClients": server.sse_clients()}


def _handle_boards_list(handler) -> None:
    try:
        registered = wb.registry_load()["boards"]
    except (OSError, ValueError) as exc:
        return _send_error(handler, exc)
    boards = []
    for name in sorted(registered, key=lambda item: (item.casefold(), item)):
        entry = {"name": name, "board": registered[name], "url": f"/b/{quote(name, safe='')}/",
                 "exists": False, "rev": None, "cards": None, "error": None}
        try:
            path = wb.canonical_registered_board(registered[name])
            entry["board"] = str(path)
            if path.is_file():
                entry["exists"] = True
                doc = wb.load(path)
                entry.update(rev=int(doc.get("rev") or 0), cards=len(doc["cards"]))
        except (wb.UnsafeBoardPath, SystemExit, OSError, ValueError, KeyError, TypeError) as exc:
            entry["error"] = str(exc)
        boards.append(entry)
    handler._json({"boards": boards})


def _handle_boards_delete(handler) -> None:
    try:
        _require_json(handler)
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
        result = wb.delete_registered_board(name, expected_board, base_rev)
    except BodyError as e:
        return handler._json({"error": e.message}, e.status)
    except wb.RegistryNotFound:
        return handler._json({"error": f"no registered board '{name}'"}, 404)
    except (wb.RegistryConflict, wb.UnsafeBoardPath) as e:
        return handler._json({"ok": False, "conflict": True, "error": str(e)}, 409)
    except (wb.LockTimeout, OSError, ValueError, KeyError, TypeError) as e:
        return handler._json({"error": f"delete failed: {e}"}, 500)
    handler._json({"ok": True, "recoveryPath": result["recoveryPath"], "board": result["board"]})


def _handle_shutdown(handler) -> None:
    handler.close_connection = True  # Any request body stays unread.
    supplied = handler.headers.get("X-WorkBoard-Token", "")
    if not secrets.compare_digest(supplied.encode("utf-8"), handler.server.token.encode("utf-8")):
        return handler._json({"ok": False, "status": 403, "error": "invalid shutdown token"}, 403)
    handler._json({"ok": True})
    threading.Thread(target=handler.server.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    board: Path | None = None

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

    def _html(self):
        try:
            body = resources.files("workboard").joinpath("web/board.html").read_bytes()
        except OSError:
            return self._json({"error": "board.html missing"}, 500)
        self._send(200, body, "text/html; charset=utf-8")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str) -> None:
        try:
            _require_local(self)
        except BodyError as e:
            return _send_error(self, e)
        path, _, query = self.path.partition("?")
        root = {("GET", "/"): self._html,
                ("GET", "/health"): lambda: self._json(_health(self.server)),
                ("GET", "/api/boards"): lambda: _handle_boards_list(self),
                ("POST", "/api/boards/delete"): lambda: _handle_boards_delete(self),
                ("POST", "/api/shutdown"): lambda: _handle_shutdown(self)}.get((method, path))
        if root:
            return root()
        match = _BOARD_ROUTE.match(path)
        if not match:
            return self._json({"error": "not found"}, 404)
        encoded, rest = match.groups()
        if rest is None or rest == "/":
            if method != "GET":
                return self._json({"error": "not found"}, 404)
            if rest is None:
                location = f"/b/{encoded}/" + (f"?{query}" if query else "")
                return self._send(301, b"", "text/plain; charset=utf-8", {"Location": location})
            return self._html()
        name = unquote(encoded)
        try:
            self.board = _registered_board(name)
        except UnknownBoard:
            return self._json({"error": "unknown_board", "name": name}, 404)
        except wb.UnsafeBoardPath as e:
            return _send_error(self, BodyError(409, str(e)))
        except (OSError, ValueError) as e:
            return _send_error(self, e)
        if method == "GET":
            return _handle_board_get(self, rest, query)
        _handle_board_write(self, method, rest, query)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True
        board = self.board
        q = self.server.subscribe(board)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.server.closed.is_set():
                try:
                    data = q.get(timeout=15.0)
                except queue.Empty:
                    data = ": keepalive\n\n"
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.server.unsubscribe(board, q)


class Server(ThreadingHTTPServer):
    """Loopback HTTP server for every registered board, with per-board SSE watchers."""
    daemon_threads = True
    block_on_close = False  # SSE streams never finish on their own.
    # POSIX needs SO_REUSEADDR to rebind over TIME_WAIT; on Windows it would allow port theft.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False  # A second server must see the port as busy.

    def __init__(self, address):
        # State first: a failed bind calls server_close() from inside super().__init__.
        self.token = secrets.token_urlsafe(32)
        self.started_at = wb.now_iso()
        self.closed = threading.Event()
        self._lock = threading.Lock()
        self._watched: dict[Path, dict] = {}  # board -> {"sig": stat signature, "clients": [queue]}
        super().__init__(address, Handler)
        threading.Thread(target=self._watch, daemon=True).start()

    def server_close(self):
        self.closed.set()
        super().server_close()

    def subscribe(self, board: Path) -> queue.Queue:
        q = queue.Queue(maxsize=256)
        with self._lock:
            entry = self._watched.get(board)
            if entry is None:  # The first subscriber sets the change baseline.
                entry = self._watched[board] = {"sig": _sig(board), "clients": []}
            entry["clients"].append(q)
        return q

    def unsubscribe(self, board: Path, q: queue.Queue) -> None:
        with self._lock:
            entry = self._watched.get(board)
            if entry and q in entry["clients"]:
                entry["clients"].remove(q)
                if not entry["clients"]:
                    del self._watched[board]

    def sse_clients(self) -> int:
        with self._lock:
            return sum(len(entry["clients"]) for entry in self._watched.values())

    def _broadcast(self, board: Path, event: str, payload: dict) -> None:
        with self._lock:
            entry = self._watched.get(board)
            clients = list(entry["clients"]) if entry else []
        data = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def _watch(self) -> None:
        while not self.closed.wait(0.5):
            with self._lock:
                watched = [(board, entry["sig"]) for board, entry in self._watched.items()]
            for board, last in watched:
                current = _sig(board)
                if current == last:
                    continue
                with self._lock:
                    entry = self._watched.get(board)
                    if entry is None:
                        continue
                    entry["sig"] = current
                try:
                    rev = int(_doc(board).get("rev") or 0)
                except (SystemExit, OSError, ValueError, KeyError, TypeError):
                    self._broadcast(board, "board-missing", {"board": str(board)})
                else:
                    self._broadcast(board, "rev-bumped", {"rev": rev})
                self._broadcast(board, "resync-required", {})


# ===== serve command =====

def _log_to_file() -> None:
    """Service/pythonw mode: append stdout and stderr to logs/server.log (rotated at start)."""
    logs = wb.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "server.log"
    try:
        if log.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(log, logs / "server.log.1")
    except OSError:
        pass  # Missing log, or another process still holds it; append instead.
    stream = open(log, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream


def _emit(args, line: str, payload: dict) -> None:
    print(json.dumps(payload) if args.json else line, flush=True)


def _open_name(args) -> str | None:
    """Registered name of the --board/cwd board for `serve --open`; None opens the hub."""
    board = _local_board(args)
    return _board_name(board, register=False) if board else None


def _open_browser(name: str | None, port: int) -> None:
    import webbrowser
    webbrowser.open(board_url(name, port) if name else server_url(port))


def _remove_state() -> None:
    """Delete server.json only if it still describes this process."""
    if _state().get("pid") != os.getpid():
        return
    deadline = time.monotonic() + 3.0
    while True:
        try:
            wb.server_state_path().unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:  # A reader without delete sharing (e.g. antivirus) holds it briefly.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def serve(args) -> None:
    service = bool(getattr(args, "service", False))
    if service:
        from . import install
        if install.relaunch_from_runtime_copy():
            return
    if service or sys.stdout is None:
        _log_to_file()
    want_open = bool(getattr(args, "open", False)) and not service
    open_name = _open_name(args) if want_open else None
    count = len(wb.registry_load()["boards"])
    port = _configured_port() if args.port is None else args.port
    if not 0 <= port <= 65535:
        raise wb.WorkflowError("port must be an integer 0..65535")
    try:
        httpd = Server(("127.0.0.1", port))
    except OSError:
        info = _health_at(port) if port else None
        if info is None:
            raise wb.WorkflowError(
                f"port {port} is in use by another program; pass --port or set WORKBOARD_PORT",
                409, "state")
        url = server_url(port)
        _emit(args, f"already running: {url}", {"ok": True, "url": url, "pid": info.get("pid"),
                                                "port": port, "version": info.get("version"),
                                                "alreadyRunning": True})
        if want_open:
            _open_browser(open_name, port)
        return
    try:
        port = httpd.server_port
        url = server_url(port)
        wb._atomic_write_json(wb.server_state_path(), {
            "pid": os.getpid(), "port": port, "url": url, "version": __version__,
            "startedAt": httpd.started_at, "executable": sys.executable, "token": httpd.token})
        _emit(args, f"serving {count} board{'' if count == 1 else 's'} at {url} (Ctrl+C to stop)",
              {"ok": True, "url": url, "pid": os.getpid(), "port": port, "version": __version__})
        if want_open:
            _open_browser(open_name, port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            if not args.json:
                print("stopped.", flush=True)
    finally:
        httpd.server_close()
        _remove_state()
