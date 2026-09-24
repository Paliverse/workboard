"""Integration tests for the per-user multi-board server (`workboard serve`).

Servers run as real `serve --port 0` subprocesses (or in-process on port 0)
inside a scratch home and are always shut down. Ported from the preview's
test_webui.py (per-board HTTP contract, now under /b/<name>/), test_switcher.py
(registry listing and recoverable delete) and test_smoke.py c26 (browser
changedRev is server-owned), plus the multi-board contract: isolation, SSE
per board, unknown/missing boards, Host checks, token shutdown, server.json.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from importlib import resources
from pathlib import Path
from unittest import mock

from tests.support import command, last_json, make_env, run, scratch

from workboard import __version__, core, server

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_ATTACHMENT_BYTES = core.MAX_ATTACHMENT_BYTES
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Loopback never uses a proxy.


def http_req(url, method="GET", payload=None, headers=None):
    request_headers = dict(headers or {})
    if payload is not None and not isinstance(payload, bytes):
        payload = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=payload, method=method, headers=request_headers)
    try:
        response = _OPENER.open(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = raw
        return response.status, body, dict(response.headers)


def board(ctx):
    status, doc, _ = http_req(ctx["url"] + "/board.json")
    assert status == 200, (status, doc)
    return doc


def mutate(ctx, path, body, *, method="PATCH", actor="Ada", rev=None, headers=None):
    payload = {**body, "baseRev": board(ctx)["rev"] if rev is None else rev, "actor": actor}
    return http_req(ctx["url"] + path, method, payload, headers)


def create(ctx, title, **fields):
    raw = {"id": str(uuid.uuid4()), "title": title, "column": "task", **fields}
    status, data, _ = mutate(ctx, "/api/structure", {
        "operation": {"type": "create-card", "card": raw}})
    assert status == 200, (status, data)
    return data["card"]


def life(ctx, card_id, action, details=None, **kwargs):
    return mutate(ctx, f"/api/card/{card_id}/lifecycle",
                  {"action": action, "details": details or {}}, **kwargs)


def current(ctx, card_id):
    status, data, _ = http_req(ctx["url"] + f"/api/card/{card_id}")
    assert status == 200, (status, data)
    return data["card"]


def full_post(ctx, doc, **kwargs):
    return mutate(ctx, "/board.json", doc, method="POST", **kwargs)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_state(path: Path, proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Exit 0 is a hand-off (frozen runtime-copy relaunch); anything else failed.
            assert proc.poll() in (None, 0), f"server exited with {proc.returncode}"
            time.sleep(0.1)
    raise AssertionError(f"{path} was not written within {timeout}s")


def _readline(stream, timeout: float) -> str:
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return lines[0] if lines else ""


class RunningServer:
    """`workboard serve --port 0 --json` confined to a scratch home; always stop() it."""

    def __init__(self, base: Path, env: dict):
        self.home = Path(env["WORKBOARD_HOME"])
        self.info: dict = {}
        self.root = None
        self.log_path = base / f"server-{uuid.uuid4().hex[:8]}.log"
        self._log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [*command(), "serve", "--port", "0", "--json"], cwd=str(base), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._log,
            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        line = _readline(self.proc.stdout, 30)
        try:
            self.info = json.loads(line)
        except ValueError:
            self.stop()
            raise AssertionError(f"server did not start: {line!r}; stderr: {self.log()}")
        self.port = self.info["port"]
        self.root = f"http://127.0.0.1:{self.port}"

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def token(self) -> str:
        return json.loads((self.home / "server.json").read_text(encoding="utf-8"))["token"]

    def stop(self) -> None:
        if self.proc.poll() is None:
            if self.root:
                try:
                    http_req(self.root + "/api/shutdown", "POST", b"", {"X-WorkBoard-Token": self.token()})
                except (OSError, ValueError, KeyError):
                    pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
                # A venv launcher's child is the real server; its pid came from its own JSON line.
                pid = self.info.get("pid")
                if pid and pid != self.proc.pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
        self.proc.stdout.close()
        self._log.close()


class SseStream:
    """A raw SSE subscription to one board's /events."""

    def __init__(self, ctx):
        self.connection = http.client.HTTPConnection("127.0.0.1", ctx["port"], timeout=10)
        self.connection.request("GET", ctx["prefix"] + "/events")
        self.sock = self.connection.sock
        self.response = self.connection.getresponse()
        assert self.response.status == 200, self.response.status
        assert self.response.readline() == b": connected\n"
        assert self.response.readline() == b"\n"

    def next_event(self, timeout: float) -> tuple[str, dict]:
        self.sock.settimeout(timeout)
        event = data = None
        while True:
            line = self.response.readline().decode("utf-8")
            if not line:
                raise EOFError("event stream closed")
            line = line.rstrip("\n")
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
            elif not line and event:
                return event, data

    def silent_for(self, seconds: float) -> bool:
        self.sock.settimeout(seconds)
        try:
            return not self.response.readline()
        except TimeoutError:
            return True

    def close(self) -> None:
        self.response.close()
        self.connection.close()


class MultiBoardApiTest(unittest.TestCase):
    """One server for many boards; every test registers its own fresh board."""

    @classmethod
    def setUpClass(cls):
        cls.base = cls.enterClassContext(scratch("wb-server-"))
        cls.env = make_env(cls.base / "home")
        cls.enterClassContext(mock.patch.dict(os.environ, cls.env, clear=True))
        cls.server = RunningServer(cls.base, cls.env)
        cls.addClassCleanup(cls.server.stop)
        cls.origin = cls.server.root

    def new_board(self, name: str | None = None) -> dict:
        name = name or f"board-{uuid.uuid4().hex[:8]}"
        proj = self.base / f"proj-{uuid.uuid4().hex[:8]}"
        created = run(["init", name, "--dir", proj], cwd=self.base, env=self.env)
        self.assertEqual(created.returncode, 0, (created.stdout, created.stderr))
        encoded = urllib.parse.quote(name, safe="")
        return {"name": name, "proj": proj, "path": proj / "board" / "board.json",
                "port": self.server.port, "origin": self.origin, "env": self.env,
                "prefix": f"/b/{encoded}", "url": f"{self.origin}/b/{encoded}"}

    def listing(self) -> dict:
        status, data, _ = http_req(self.origin + "/api/boards")
        self.assertEqual(status, 200, data)
        return {entry["name"]: entry for entry in data["boards"]}

    # ----- multi-board contract -----

    def test_two_boards_are_served_with_isolated_state(self):
        a, b = self.new_board(), self.new_board()
        card = create(a, "Only on A")
        before_b = b["path"].read_bytes()
        doc_a, doc_b = board(a), board(b)
        self.assertEqual((doc_a["name"], doc_b["name"]), (a["name"], b["name"]))
        self.assertEqual([c["id"] for c in doc_a["cards"]], [card["id"]])
        self.assertEqual(doc_b["cards"], [])
        self.assertEqual(http_req(a["url"] + "/rev")[1], doc_a["rev"])
        self.assertEqual(http_req(b["url"] + "/rev")[1], doc_b["rev"])
        self.assertEqual(http_req(a["url"] + f"/api/card/{card['id']}")[0], 200)
        self.assertEqual(http_req(b["url"] + f"/api/card/{card['id']}")[0], 404)
        self.assertEqual(http_req(b["url"] + "/api/bootstrap")[1], {"state": doc_b})
        self.assertEqual(b["path"].read_bytes(), before_b)

    def test_sse_notifies_only_the_changed_board(self):
        a, b = self.new_board(), self.new_board()
        stream_a, stream_b = SseStream(a), SseStream(b)
        try:
            self.assertGreaterEqual(http_req(self.origin + "/health")[1]["sseClients"], 2)
            added = run(["--board", a["proj"], "add", "--title", "From the CLI", "--json"],
                        cwd=self.base, env=self.env)
            self.assertEqual(added.returncode, 0, (added.stdout, added.stderr))
            rev = last_json(added)["rev"]
            events = []
            deadline = time.monotonic() + 15
            while ("rev-bumped", {"rev": rev}) not in events:
                self.assertLess(time.monotonic(), deadline, events)
                events.append(stream_a.next_event(timeout=max(0.1, deadline - time.monotonic())))
            self.assertEqual(stream_a.next_event(timeout=5), ("resync-required", {}))
            self.assertTrue(stream_b.silent_for(1.5), "board B's stream saw board A's change")
        finally:
            stream_a.close()
            stream_b.close()

    def test_unknown_board_404_and_missing_board_410(self):
        for route in ("/board.json", "/rev", "/events", "/api/stats"):
            status, body, _ = http_req(self.origin + "/b/no%20such%20board" + route)
            self.assertEqual((status, body), (404, {"error": "unknown_board", "name": "no such board"}), route)
        self.assertEqual(http_req(self.origin + "/b/no-such-board/board.json", "POST", {"baseRev": 0})[0], 404)
        missing = self.new_board()
        missing["path"].unlink()
        for route in ("/board.json", "/api/bootstrap", "/rev", "/api/ready", "/api/stats", "/api/git",
                      "/api/cards?column=task"):
            status, body, _ = http_req(missing["url"] + route)
            self.assertEqual((status, body.get("error")), (410, "board_missing"), route)
            self.assertEqual(Path(body["board"]), missing["path"].resolve())
        self.assertEqual(mutate(missing, "/api/structure", {"operation": {
            "type": "sort-cards", "mode": "created-desc"}}, rev=0)[0], 410)
        entry = self.listing()[missing["name"]]
        self.assertEqual((entry["exists"], entry["rev"], entry["cards"]), (False, None, None))
        status, _, headers = http_req(missing["url"] + "/")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))

    def test_pages_redirect_and_url_encoded_names(self):
        ctx = self.new_board(f"Gamma board {uuid.uuid4().hex[:6]}")
        html = resources.files("workboard").joinpath("web/board.html").read_bytes()
        for url in (self.origin + "/", ctx["url"] + "/"):
            status, body, headers = http_req(url)
            self.assertEqual((status, headers["Content-Type"], body), (200, "text/html; charset=utf-8", html))
        connection = http.client.HTTPConnection("127.0.0.1", ctx["port"], timeout=10)
        try:
            connection.request("GET", ctx["prefix"] + "?from=link")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        self.assertEqual((response.status, response.getheader("Location")),
                         (301, ctx["prefix"] + "/?from=link"))
        self.assertEqual(board(ctx)["name"], ctx["name"])
        self.assertEqual(http_req(self.origin + "/nope")[0], 404)

    def test_host_and_origin_checks_cover_every_route(self):
        ctx = self.new_board()
        port = ctx["port"]
        for path in ("/health", "/", "/api/boards", ctx["prefix"] + "/", ctx["prefix"] + "/board.json",
                     ctx["prefix"] + "/events", ctx["prefix"] + "/api/git"):
            for host in ("evil.example", f"127.0.0.1:{port + 1}", "localhost"):
                self.assertEqual(http_req(self.origin + path, headers={"Host": host})[0], 403, (path, host))
            self.assertEqual(http_req(self.origin + path, headers={"Origin": "http://evil.example"})[0], 403, path)
        for host in (f"localhost:{port}", f"[::1]:{port}", f"127.0.0.1:{port}"):
            self.assertEqual(http_req(self.origin + "/health", headers={"Host": host})[0], 200, host)
        for origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}"):
            self.assertEqual(mutate(ctx, "/api/structure", {"operation": {
                "type": "update-document", "changes": {"title": origin}}}, headers={"Origin": origin})[0], 200)

    def test_registry_listing_contract(self):
        tag = uuid.uuid4().hex[:6]
        apple, banana, gone = (self.new_board(f"apple-{tag}"), self.new_board(f"Banana-{tag}"),
                               self.new_board(f"cherry {tag}"))
        create(banana, "Counted")
        gone["path"].unlink()
        status, data, _ = http_req(self.origin + "/api/boards")
        self.assertEqual(status, 200)
        names = [entry["name"] for entry in data["boards"]]
        self.assertEqual(names, sorted(names, key=lambda name: (name.casefold(), name)))
        self.assertLess(names.index(apple["name"]), names.index(banana["name"]))
        entries = {entry["name"]: entry for entry in data["boards"]}
        for ctx in (apple, banana, gone):
            entry = entries[ctx["name"]]
            self.assertEqual(set(entry), {"name", "board", "url", "exists", "rev", "cards", "error"})
            self.assertEqual(entry["url"], ctx["prefix"] + "/")
            self.assertEqual(Path(entry["board"]), ctx["path"].resolve())
            self.assertTrue(Path(entry["board"]).is_absolute())
        self.assertEqual(entries[gone["name"]]["url"], f"/b/cherry%20{tag}/")
        self.assertEqual(
            {key: entries[banana["name"]][key] for key in ("exists", "rev", "cards", "error")},
            {"exists": True, "rev": board(banana)["rev"], "cards": 1, "error": None})
        self.assertEqual(
            {key: entries[gone["name"]][key] for key in ("exists", "rev", "cards", "error")},
            {"exists": False, "rev": None, "cards": None, "error": None})

    def test_recoverable_board_delete(self):
        keep, target = self.new_board(), self.new_board()
        (target["proj"] / "keep.txt").write_text("keep", encoding="utf-8")
        card = create(target, "Recovery file")
        status, uploaded, _ = http_req(
            target["url"] + f"/api/card/{card['id']}/attachments?name=recovery.bin", "POST",
            b"recovery attachment bytes", {"Content-Type": "application/octet-stream",
                                          "X-Board-Base-Rev": str(board(target)["rev"])})
        self.assertEqual(status, 200, uploaded)
        blob = target["path"].parent / "attachments" / uploaded["attachment"]["id"]
        attachment_url = target["url"] + f"/api/card/{card['id']}/attachments/{uploaded['attachment']['id']}"
        registry = json.loads(core.registry_path().read_text(encoding="utf-8"))
        alias = target["name"] + "-alias"
        registry["boards"][alias] = registry["boards"][target["name"]]
        core.registry_path().write_text(json.dumps(registry), encoding="utf-8")
        identity = self.listing()[target["name"]]["board"]
        original = target["path"].read_bytes()
        url = self.origin + "/api/boards/delete"
        payload = {"name": target["name"], "board": identity, "baseRev": uploaded["rev"]}
        self.assertEqual(http_req(url, "POST", payload, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(http_req(url, "POST", payload, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(http_req(url, "POST", payload, {"Host": "evil.example"})[0], 403)
        self.assertEqual(http_req(url, "POST", {**payload, "board": str(keep["path"].resolve())})[0], 409)
        self.assertEqual(http_req(url, "POST", {**payload, "baseRev": uploaded["rev"] - 1})[0], 409)
        self.assertEqual(http_req(url, "POST", {key: payload[key] for key in ("name", "board")})[0], 400)
        self.assertEqual(target["path"].read_bytes(), original)
        status, data, _ = http_req(url, "POST", payload)
        self.assertEqual(status, 200, data)
        self.assertEqual((data["ok"], data["board"]), (True, identity))
        self.assertEqual(Path(data["recoveryPath"]).read_bytes(), original)
        remaining = json.loads(core.registry_path().read_text(encoding="utf-8"))["boards"]
        self.assertNotIn(target["name"], remaining)
        self.assertNotIn(alias, remaining)
        self.assertIn(keep["name"], remaining)
        self.assertFalse(target["path"].exists())
        self.assertEqual((target["proj"] / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue((target["path"].parent / ".backups").is_dir())
        self.assertEqual(blob.read_bytes(), b"recovery attachment bytes")
        self.assertEqual(http_req(target["url"] + "/board.json")[1],
                         {"error": "unknown_board", "name": target["name"]})
        self.assertEqual(http_req(attachment_url)[0], 404)
        self.assertEqual(board(keep)["name"], keep["name"])
        # A registration whose file is already gone needs an explicit null base revision.
        stale = self.new_board()
        stale["path"].unlink()
        stale_identity = self.listing()[stale["name"]]["board"]
        stale_payload = {"name": stale["name"], "board": stale_identity}
        self.assertEqual(http_req(url, "POST", {**stale_payload, "baseRev": 1})[0], 409)
        status, data, _ = http_req(url, "POST", {**stale_payload, "baseRev": None})
        self.assertEqual((status, data["recoveryPath"]), (200, None), data)
        self.assertNotIn(stale["name"], self.listing())
        self.assertEqual(http_req(url, "POST", {"name": "zzz-nope", "board": identity, "baseRev": None})[0], 404)

    # ----- per-board endpoints (preview test_webui.py) -----

    def test_edit_order_and_beacon(self):
        ctx = self.new_board()
        first, second = create(ctx, "First"), create(ctx, "Second")
        status, edited, _ = mutate(ctx, f"/api/card/{second['id']}", {
            "card": {"id": second["id"], "title": "Second edited", "notes": "durable notes",
                     "subtasks": [{"id": "s1", "text": "parent", "collapsed": True,
                                   "children": [{"id": "s2", "text": "child", "done": True}]}]},
            "position": {"before": first["id"]}})
        assert status == 200 and edited["positioned"], edited
        assert edited["card"]["num"] == second["num"]
        assert edited["card"]["createdAt"] == second["createdAt"]
        assert edited["card"]["subtasks"][0]["collapsed"] is True
        assert edited["card"]["subtasks"][0]["children"][0]["done"] is True
        assert [c["id"] for c in board(ctx)["cards"]][-2:] == [second["id"], first["id"]]
        doc = board(ctx)
        doc["columns"][0], doc["columns"][1] = doc["columns"][1], doc["columns"][0]
        doc["columns"][1]["stackUnder"] = doc["columns"][0]["id"]
        status, saved, _ = full_post(ctx, doc)
        assert status == 200 and saved["document"]["columns"][1]["stackUnder"] == doc["columns"][0]["id"]
        columns = board(ctx)["columns"]
        columns[2]["stackUnder"] = columns[1]["id"]
        status, stacked, _ = mutate(ctx, "/api/structure", {"operation": {
            "type": "update-columns", "columns": columns, "columnMoves": {}}})
        assert status == 200 and stacked["document"]["columns"][2]["stackUnder"] == columns[0]["id"]
        status, titled, _ = mutate(ctx, "/api/structure", {"operation": {
            "type": "update-document", "changes": {"title": "Preview test board"}}})
        assert status == 200 and titled["document"]["title"] == "Preview test board"
        doc = board(ctx)
        doc["rev"] += 1
        status, saved, _ = http_req(ctx["url"] + "/board.json", "POST", doc,
                                    {"Content-Type": "text/plain;charset=UTF-8", "Origin": ctx["origin"]})
        assert status == 200 and saved["document"]["rev"] == doc["rev"], saved
        assert full_post(ctx, doc, rev=doc["rev"] - 1)[0] == 409
        status, sorted_doc, _ = mutate(ctx, "/api/structure", {
            "operation": {"type": "sort-cards", "mode": "created-desc"}})
        expected = sorted(board(ctx)["cards"], key=lambda c: (c["createdAt"], c["num"]), reverse=True)
        assert status == 200
        assert [c["id"] for c in sorted_doc["document"]["cards"]] == [c["id"] for c in expected]
        assert board(ctx)["cards"] == sorted_doc["document"]["cards"]
        status, page, _ = http_req(ctx["url"] + "/api/cards?column=task&offset=1&limit=1")
        assert status == 200 and page["cards"] == expected[1:2] and page["total"] == len(expected)
        bad_columns = board(ctx)
        bad_columns["columns"].append({"id": "sixth", "name": "Sixth"})
        assert full_post(ctx, bad_columns)[0] == 422
        claimed = create(ctx, "Add directly to active column", column="inprogress")
        assert claimed["activeOwner"] == "Ada" and claimed["claimedAt"]
        assert life(ctx, claimed["id"], "move", {"to": "task"})[0] == 200

    def test_ownership_dependencies_and_outcomes(self):
        ctx = self.new_board()
        predecessor, work = create(ctx, "Predecessor"), create(ctx, "Dependent", priority="critical")
        pid, wid = predecessor["id"], work["id"]
        assert life(ctx, wid, "dependencies", {"ids": [pid, pid]})[0] == 200
        assert current(ctx, wid)["dependsOn"] == [pid]
        assert life(ctx, pid, "dependencies", {"ids": [wid]})[0] == 422
        assert life(ctx, wid, "dependencies", {"ids": [str(uuid.uuid4())]})[0] == 422
        assert life(ctx, wid, "start")[0] == 409
        assert mutate(ctx, f"/api/card/{wid}", {"card": {"column": "inprogress"}})[0] == 409
        assert life(ctx, pid, "complete", {"writeup": "Cannot skip claim"})[0] == 409
        assert life(ctx, pid, "start")[0] == 200
        assert life(ctx, pid, "submit", {"summary": "Completed prerequisite"})[0] == 422
        assert life(ctx, pid, "submit", {"summary": "Completed prerequisite",
                                         "verification": "Observed result"})[0] == 200
        columns = board(ctx)["columns"]
        next(c for c in columns if c["id"] == "inprogress")["wipLimit"] = 1
        assert mutate(ctx, "/api/structure", {"operation": {
            "type": "update-columns", "columns": columns}})[0] == 200
        snapshot = board(ctx)
        status, ready, _ = http_req(ctx["url"] + "/api/ready")
        assert status == 200 and ready["cards"][0]["id"] == wid
        assert board(ctx) == snapshot
        shared_rev = snapshot["rev"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(lambda actor: life(ctx, wid, "start", actor=actor, rev=shared_rev),
                                     ("Ada", "Bob")))
        assert sorted(result[0] for result in attempts) == [200, 409], attempts
        owner = current(ctx, wid)["activeOwner"]
        other = "Bob" if owner == "Ada" else "Ada"
        capacity = create(ctx, "Wait for WIP capacity")
        assert life(ctx, capacity["id"], "start")[0] == 409
        assert mutate(ctx, "/api/structure", {"operation": {"type": "create-card", "card": {
            "id": str(uuid.uuid4()), "title": "Cannot bypass WIP by creation", "column": "inprogress"}}})[0] == 409
        assert life(ctx, wid, "start", actor=other)[0] == 409
        assert life(ctx, wid, "cancel", {"reason": "not mine"}, actor=other)[0] == 409
        assert mutate(ctx, "/api/structure", {"operation": {"type": "delete-card", "cardId": wid}},
                      actor=other)[0] == 409
        snapshot_move = board(ctx)
        next(c for c in snapshot_move["cards"] if c["id"] == wid)["column"] = "task"
        assert full_post(ctx, snapshot_move, actor=other)[0] == 409
        assert mutate(ctx, f"/api/card/{wid}", {"card": {"notes": "Collaborative notes"}}, actor=other)[0] == 200
        assert life(ctx, wid, "workpad", actor=other)[0] == 200
        notes = current(ctx, wid)["notes"]
        assert "Collaborative notes" in notes and "## Acceptance criteria" in notes and "## Verification" in notes
        assert life(ctx, wid, "workpad", actor=other)[1]["card"]["notes"] == notes
        assert life(ctx, wid, "takeover", actor=other)[0] == 422
        assert life(ctx, wid, "takeover", {"reason": "Explicit handoff"}, actor=other)[0] == 200
        assert current(ctx, wid)["activeOwner"] == other
        before_stats = http_req(ctx["url"] + "/api/stats")[1]["stats"]
        assert life(ctx, wid, "cancel", {"reason": "No longer required"}, actor=other)[0] == 200
        canceled = current(ctx, wid)
        assert canceled["column"] == "done" and canceled["outcome"] == "canceled"
        assert canceled["activeOwner"] is None and canceled["claimedAt"] is None and canceled["doneAt"]
        assert life(ctx, wid, "move", {"to": "done"})[1]["card"]["outcome"] == "canceled"
        after_stats = http_req(ctx["url"] + "/api/stats")[1]["stats"]
        assert after_stats["canceled"] == before_stats["canceled"] + 1
        assert after_stats["completed"] == before_stats["completed"]
        assert after_stats["completedLast7Days"] == before_stats["completedLast7Days"]
        child = create(ctx, "Depends on canceled")
        assert life(ctx, child["id"], "dependencies", {"ids": [wid]})[0] == 200
        assert life(ctx, child["id"], "start")[0] == 409
        assert life(ctx, wid, "cancel", {"reason": "again"})[0] == 409
        assert life(ctx, wid, "rework", {"reason": "Requirement returned"})[0] == 200
        reworked = current(ctx, wid)
        assert reworked["column"] == "task" and reworked["outcome"] is None and reworked["doneAt"] is None
        assert reworked["reworkReason"] == "Requirement returned" and reworked["cycles"][-1]["outcome"] == "canceled"
        assert life(ctx, wid, "start")[0] == 200
        assert life(ctx, wid, "block", {"reason": "Awaiting input", "until": "Input arrives"})[0] == 200
        assert current(ctx, wid)["activeOwner"] is None
        assert life(ctx, wid, "resume", {"note": "Input received", "to": "inprogress"})[0] == 200
        assert life(ctx, wid, "complete", {"writeup": "Verified final result"})[0] == 200
        assert current(ctx, wid)["reworkReason"] is None
        assert life(ctx, child["id"], "start")[0] == 200

    def test_raw_protection_and_followup(self):
        ctx = self.new_board()
        done = create(ctx, "Completed source")
        assert life(ctx, done["id"], "start")[0] == 200
        assert life(ctx, done["id"], "complete", {"writeup": "Shipped"})[0] == 200
        target = create(ctx, "Protected collaboration")
        tid = target["id"]
        stale_snapshot = board(ctx)
        status, added, _ = mutate(ctx, f"/api/card/{tid}/comments", {
            "operation": {"type": "add", "text": "Must survive undo"}})
        assert status == 200
        revision = board(ctx)["rev"]
        assert full_post(ctx, stale_snapshot, rev=revision)[0] == 422
        assert full_post(ctx, stale_snapshot, rev=revision - 1)[0] == 409
        assert current(ctx, tid)["comments"] == [added["comment"]]
        for forged in ({"outcome": "nonsense"}, {"activeOwner": "Mallory"},
                       {"lifecycleCycles": [{"writeup": "forged"}]}, {"history": []},
                       {"num": target["num"] + 99}, {"createdAt": "1900-01-01"}, {"updatedAt": "1900-01-01"}):
            assert mutate(ctx, f"/api/card/{tid}", {"card": forged})[0] == 422, forged
        stripped = board(ctx)
        for raw in stripped["cards"]:
            for key in ("comments", "attachments", "history", "updatedAt"):
                raw.pop(key, None)
        status, data, _ = full_post(ctx, stripped)
        assert status == 200 and current(ctx, tid)["comments"] == [added["comment"]], data
        dropped = board(ctx)
        dropped["cards"] = [c for c in dropped["cards"] if c["id"] != tid]
        assert full_post(ctx, dropped)[0] == 422
        for operation in (
            {"type": "create-card", "card": {"id": str(uuid.uuid4()), "title": "forged", "outcome": "canceled"}},
            {"type": "create-card", "card": {"id": str(uuid.uuid4()), "title": "premature completion",
                                             "column": "done"}},
            {"type": "delete-card", "cardId": tid, "card": {"comments": []}},
            {"type": "create-follow-up", "sourceCardId": done["id"],
             "sourceCard": {"id": done["id"], "outcome": "canceled"},
             "card": {"id": str(uuid.uuid4()), "title": "forged source"}},
        ):
            assert mutate(ctx, "/api/structure", {"operation": operation})[0] == 422, operation
        source_before = current(ctx, done["id"])
        follow_id = str(uuid.uuid4())
        status, follow, _ = mutate(ctx, "/api/structure", {"operation": {
            "type": "create-follow-up", "sourceCardId": source_before["id"],
            "card": {"id": follow_id, "title": "Real follow-up", "column": "task"}}})
        assert status == 200 and source_before["id"] in follow["card"]["links"], follow
        source_after = current(ctx, source_before["id"])
        assert follow_id in source_after["links"]
        assert (source_after["outcome"], source_after["writeup"], source_after["doneAt"]) == (
            source_before["outcome"], source_before["writeup"], source_before["doneAt"])
        dependent = create(ctx, "Unresolved after deletion")
        assert life(ctx, dependent["id"], "dependencies", {"ids": [tid]})[0] == 200
        assert mutate(ctx, "/api/structure", {"operation": {"type": "delete-card", "cardId": tid}})[0] == 200
        assert life(ctx, dependent["id"], "start")[0] == 409
        assert mutate(ctx, "/api/structure", {"operation": {
            "type": "create-card", "card": {"id": tid, "title": "Recycled identity"}}})[0] == 409

    def test_browser_note_adds_a_timeline_entry(self):
        ctx = self.new_board()
        cid = create(ctx, "Timeline")["id"]
        before = board(ctx)["rev"]
        stream = SseStream(ctx)
        try:
            status, data, _ = life(ctx, cid, "note", {"summary": "Parser shipped", "body": "- `abc1234`\n- 12 tests"})
            self.assertEqual(status, 200, data)
            self.assertEqual((data["event"], data["action"], data["rev"]), ("card-updated", "note", before + 1))
            events = []
            deadline = time.monotonic() + 15
            while ("rev-bumped", {"rev": data["rev"]}) not in events:
                self.assertLess(time.monotonic(), deadline, events)
                events.append(stream.next_event(timeout=max(0.1, deadline - time.monotonic())))
        finally:
            stream.close()
        saved = json.loads(ctx["path"].read_text(encoding="utf-8"))
        entry = next(card for card in saved["cards"] if card["id"] == cid)["log"][-1]
        self.assertEqual((saved["rev"], entry["summary"], entry["body"], entry["by"]),
                         (before + 1, "Parser shipped", "- `abc1234`\n- 12 tests", "Ada"))
        self.assertEqual(data["card"]["log"], [entry])

        self.assertEqual(life(ctx, cid, "note", {"summary": "Late"}, rev=before)[0], 409)
        self.assertEqual(life(ctx, cid, "note", {"summary": "two\nlines"})[0], 422)
        on_disk = ctx["path"].read_bytes()
        status, error, _ = mutate(ctx, f"/api/card/{cid}", {"card": {"log": [{**entry, "summary": "Rewritten"}]}})
        self.assertEqual(status, 422, error)
        self.assertIn("'log' is server-owned", error["error"])
        self.assertEqual(ctx["path"].read_bytes(), on_disk)

    def test_legacy_board_is_served_as_a_timeline_without_writing(self):
        ctx = self.new_board()
        create(ctx, "Legacy notes")
        raw = json.loads(ctx["path"].read_text(encoding="utf-8"))
        raw["schemaVersion"] = 2
        raw["cards"][0].pop("log", None)
        raw["cards"][0]["notes"] = ("Pinned context\n\n"
                                    "[2026-09-01] Shipped the parser. Tests pass.\n[2026-09-02 ada] Fixed nits")
        ctx["path"].write_bytes(json.dumps(raw, indent=2).encode("utf-8"))
        before = ctx["path"].read_bytes()
        doc = board(ctx)
        served = doc["cards"][0]
        self.assertEqual((doc["schemaVersion"], served["notes"]), (3, "Pinned context"))
        self.assertEqual([(item["at"], item["by"], item["summary"], item["body"]) for item in served["log"]],
                         [("2026-09-01", None, "Shipped the parser.", "Tests pass."),
                          ("2026-09-02", "ada", "Fixed nits", "")])
        self.assertEqual(board(ctx)["cards"][0]["log"], served["log"], "migrated IDs are deterministic")
        self.assertEqual(ctx["path"].read_bytes(), before)

    def test_comments_and_attachments(self):
        ctx = self.new_board()
        card, other = create(ctx, "File roundtrip"), create(ctx, "Not file owner")
        cid = card["id"]
        path = f"/api/card/{cid}/comments"
        assert mutate(ctx, path, {"operation": {"type": "add", "text": " "}})[0] == 422
        assert mutate(ctx, path, {"operation": {"type": "add", "text": "x" * 16001}})[0] == 422
        status, added, _ = mutate(ctx, path, {"operation": {"type": "add", "text": "First comment"}})
        assert status == 200
        comment = added["comment"]
        status, edited, _ = mutate(ctx, path, {"operation": {
            "type": "edit", "id": comment["id"], "text": "Collaboratively edited"}}, actor="Bob")
        assert status == 200 and edited["comment"]["text"] == "Collaboratively edited"
        assert all(edited["comment"][key] == comment[key] for key in ("id", "by", "at"))
        assert edited["comment"]["updatedAt"]
        assert mutate(ctx, path, {"operation": {"type": "delete", "id": "absent"}})[0] == 404
        assert mutate(ctx, path, {"operation": {"type": "delete", "id": comment["id"]}}, actor="Bob")[0] == 200
        assert current(ctx, cid)["comments"] == []
        content = b"\x00\xffActual binary bytes\r\n<script>not inline</script>"
        name = '../report "caf\u00e9".html'
        upload = ctx["url"] + f"/api/card/{cid}/attachments?name=" + urllib.parse.quote(name, safe="")
        upload_rev = board(ctx)["rev"]
        headers = {"Content-Type": "text/html", "X-Board-Base-Rev": str(upload_rev),
                   "X-WorkBoard-Actor": urllib.parse.quote("Zo\u00eb"), "Origin": ctx["origin"]}
        status, uploaded, _ = http_req(upload, "POST", content, headers)
        assert status == 200, uploaded
        attachment = uploaded["attachment"]
        assert attachment["size"] == len(content) and attachment["sha256"] == hashlib.sha256(content).hexdigest()
        assert attachment["name"] == name and attachment["by"] == "Zo\u00eb"
        url = ctx["url"] + f"/api/card/{cid}/attachments/{attachment['id']}"
        status, downloaded, response_headers = http_req(url)
        assert status == 200 and downloaded == content
        assert response_headers["Content-Type"] == "application/octet-stream"
        assert response_headers["X-Content-Type-Options"] == "nosniff"
        assert response_headers["Content-Disposition"].startswith("attachment;")
        assert "filename*=UTF-8''" + urllib.parse.quote(name, safe="") in response_headers["Content-Disposition"]
        blob = ctx["proj"] / "board" / "attachments" / attachment["id"]
        assert blob.read_bytes() == content
        assert not (ctx["proj"] / 'report "caf\u00e9".html').exists()
        assert http_req(ctx["url"] + f"/api/card/{other['id']}/attachments/{attachment['id']}")[0] == 404
        assert http_req(ctx["url"] + f"/api/card/{cid}/attachments/%2e%2e%2fboard.json")[0] == 404
        assert http_req(upload, "POST", content, headers)[0] == 409
        assert len(list(blob.parent.iterdir())) == 1
        blob.write_bytes(b"corrupted")
        assert http_req(url)[0] == 409
        blob.unlink()
        assert http_req(url)[0] == 404
        blob.write_bytes(content)
        oversized = {**headers, "X-Board-Base-Rev": str(board(ctx)["rev"]),
                     "Content-Length": str(MAX_ATTACHMENT_BYTES + 1)}
        assert http_req(upload, "POST", b"", oversized)[0] == 413
        for framing in ([], [("Content-Length", "0"), ("Transfer-Encoding", "chunked")],
                        [("Content-Length", "0"), ("Content-Length", "0")]):
            connection = http.client.HTTPConnection("127.0.0.1", ctx["port"], timeout=5)
            connection.putrequest("POST", ctx["prefix"] + f"/api/card/{cid}/attachments?name=empty.bin")
            connection.putheader("X-Board-Base-Rev", str(board(ctx)["rev"]))
            for key, value in framing:
                connection.putheader(key, value)
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 400, framing
            response.read()
            connection.close()
        assert mutate(ctx, f"/api/card/{cid}/attachments/{attachment['id']}", {}, method="DELETE",
                      rev=upload_rev)[0] == 409
        assert mutate(ctx, f"/api/card/{cid}/attachments/{attachment['id']}", {}, method="DELETE")[0] == 200
        assert http_req(url)[0] == 404 and current(ctx, cid)["attachments"] == []
        assert blob.read_bytes() == content  # Recovery snapshots still need detached bytes.
        limit_content = b"x" * MAX_ATTACHMENT_BYTES
        status, limit_upload, _ = http_req(upload, "POST", limit_content, {
            **headers, "X-Board-Base-Rev": str(board(ctx)["rev"])})
        assert status == 200 and limit_upload["attachment"]["size"] == MAX_ATTACHMENT_BYTES
        limit_url = ctx["url"] + f"/api/card/{cid}/attachments/{limit_upload['attachment']['id']}"
        assert http_req(limit_url)[1] == limit_content

    def test_write_origin_and_actor(self):
        ctx = self.new_board()
        target = create(ctx, "Origin protected")
        cid = target["id"]
        before = board(ctx)
        operations = [
            ("/board.json", "POST", before),
            (f"/api/card/{cid}", "PATCH", {"card": {"title": "forged"}}),
            (f"/api/card/{cid}/lifecycle", "PATCH", {"action": "start"}),
            (f"/api/card/{cid}/comments", "PATCH", {"operation": {"type": "add", "text": "forged"}}),
            ("/api/structure", "PATCH", {"operation": {"type": "delete-card", "cardId": cid}}),
            (f"/api/card/{cid}/attachments/{uuid.uuid4().hex}", "DELETE", {}),
        ]
        for path, method, body in operations:
            assert mutate(ctx, path, body, method=method, headers={"Origin": "http://evil.example"})[0] == 403
        upload = ctx["url"] + f"/api/card/{cid}/attachments?name=foreign.bin"
        assert http_req(upload, "POST", b"evil", {
            "Origin": "http://evil.example", "X-Board-Base-Rev": str(before["rev"]),
            "Content-Type": "application/octet-stream"})[0] == 403
        assert board(ctx) == before
        for path in ("/board.json", f"/api/card/{cid}", "/api/ready", "/api/stats", "/api/git",
                     f"/api/card/{cid}/attachments/{uuid.uuid4().hex}"):
            assert http_req(ctx["url"] + path, headers={"Host": "evil.example"})[0] == 403
        assert mutate(ctx, f"/api/card/{cid}", {"card": {"title": "bad host"}},
                      headers={"Host": "evil.example"})[0] == 403
        assert board(ctx) == before
        assert life(ctx, cid, "start", actor=" ")[0] == 422
        assert life(ctx, cid, "start", actor="x" * 81)[0] == 422
        assert mutate(ctx, f"/api/card/{cid}", {"card": {"title": "Valid collaborator"}},
                      actor="Bob")[1]["savedBy"] == "Bob"

    def test_readonly_git_is_per_board(self):
        ctx, other = self.new_board(), self.new_board()
        before = board(ctx)
        status, absent, _ = http_req(ctx["url"] + "/api/git?path=C:/ignored")
        assert status == 200 and absent["state"] in ("not_repo", "unavailable"), absent
        assert Path(absent["root"]) == ctx["proj"].resolve()
        assert Path(http_req(other["url"] + "/api/git")[1]["root"]) == other["proj"].resolve()
        if shutil.which("git", path=ctx["env"].get("PATH")) is None:
            assert absent["state"] == "unavailable"
            return
        env = {k: v for k, v in ctx["env"].items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")

        def git(*args, success=True):
            completed = subprocess.run(["git", "-c", "user.name=Preview Test", "-c", "user.email=test@example.invalid",
                                        *args], cwd=ctx["proj"], env=env, capture_output=True, text=True, timeout=15)
            assert not success or completed.returncode == 0, (args, completed.stdout, completed.stderr)
            return completed.stdout.strip()

        git("init", "--initial-branch=preview-tests")
        unborn = http_req(ctx["url"] + "/api/git")[1]
        assert unborn["head"] is None and unborn["branch"] == "preview-tests"
        assert http_req(other["url"] + "/api/git")[1]["state"] == "not_repo"
        (ctx["proj"] / ".gitignore").write_text("board/\n", encoding="utf-8")
        (ctx["proj"] / "tracked file.txt").write_text("base\n", encoding="utf-8")
        git("add", ".gitignore", "tracked file.txt")
        git("commit", "-m", "Fixture initial commit")
        status, clean, _ = http_req(ctx["url"] + "/api/git")
        assert status == 200 and clean["state"] == "clean", clean
        assert clean["branch"] == "preview-tests" and clean["head"] == git("rev-parse", "HEAD")
        assert clean["subject"] == "Fixture initial commit" and clean["ahead"] is None and clean["behind"] is None
        filter_script = ctx["proj"] / "sentinel-filter.py"
        filter_script.write_text(
            "import pathlib, sys\n"
            "pathlib.Path(__file__).with_name('filter-ran.marker').write_text('unsafe')\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        attributes = ctx["proj"] / ".gitattributes"
        attributes.write_text('"tracked file.txt" filter=preview\n', encoding="utf-8")
        filter_command = f'"{Path(sys.executable).as_posix()}" "{filter_script.as_posix()}"'
        git("config", "filter.preview.clean", filter_command)
        included = ctx["proj"] / ".git" / "included-filter-config"
        git("config", "--file", str(included), "filter.preview.process", filter_command)
        git("config", "include.path", str(included))
        (ctx["proj"] / "tracked file.txt").write_text("requires content inspection\n", encoding="utf-8")
        unsafe = http_req(ctx["url"] + "/api/git")[1]
        assert unsafe["state"] == "error" and unsafe["error"] and unsafe["staged"] is None
        assert not (ctx["proj"] / "filter-ran.marker").exists()
        git("config", "--unset", "filter.preview.clean")
        included_only = http_req(ctx["url"] + "/api/git")[1]
        assert included_only["state"] == "error" and not (ctx["proj"] / "filter-ran.marker").exists()
        git("config", "--unset", "include.path")
        git("config", "extensions.worktreeConfig", "true")
        git("config", "--worktree", "filter.preview.clean", filter_command)
        worktree_only = http_req(ctx["url"] + "/api/git")[1]
        assert worktree_only["state"] == "error" and not (ctx["proj"] / "filter-ran.marker").exists()
        git("config", "--worktree", "--unset", "filter.preview.clean")
        git("config", "--unset", "extensions.worktreeConfig")
        filter_script.unlink()
        attributes.unlink()
        (ctx["proj"] / "tracked file.txt").write_text("base\n", encoding="utf-8")
        git("branch", "local-upstream")
        git("branch", "--set-upstream-to=local-upstream")
        (ctx["proj"] / "staged name.txt").write_text("staged\n", encoding="utf-8")
        git("add", "staged name.txt")
        (ctx["proj"] / "tracked file.txt").write_text("worktree\n", encoding="utf-8")
        (ctx["proj"] / "untracked name.txt").write_text("untracked\n", encoding="utf-8")
        index = ctx["proj"] / ".git" / "index"
        index_before = (index.read_bytes(), index.stat().st_mtime_ns)
        status, dirty, _ = http_req(ctx["url"] + "/api/git")
        assert status == 200 and dirty["state"] == "dirty", dirty
        assert (dirty["staged"], dirty["unstaged"], dirty["untracked"], dirty["conflicted"]) == (1, 1, 1, 0), dirty
        assert {item["path"] for item in dirty["files"]} == {"staged name.txt", "tracked file.txt",
                                                             "untracked name.txt"}
        assert dirty["ahead"] == dirty["behind"] == 0
        assert (index.read_bytes(), index.stat().st_mtime_ns) == index_before
        assert not (ctx["proj"] / ".git" / "index.lock").exists()
        git("add", "tracked file.txt")
        git("commit", "-m", "One ahead")
        assert http_req(ctx["url"] + "/api/git")[1]["ahead"] == 1
        git("checkout", "--detach")
        detached = http_req(ctx["url"] + "/api/git")[1]
        assert detached["branch"] is None and detached["head"] == git("rev-parse", "HEAD")
        git("checkout", "local-upstream")
        (ctx["proj"] / "tracked file.txt").write_text("upstream alternative\n", encoding="utf-8")
        git("add", "tracked file.txt")
        git("commit", "-m", "Upstream diverges")
        git("checkout", "preview-tests")
        divergent = http_req(ctx["url"] + "/api/git")[1]
        assert divergent["ahead"] == divergent["behind"] == 1
        git("mv", "staged name.txt", "renamed with spaces.txt")
        renamed = http_req(ctx["url"] + "/api/git")[1]
        assert renamed["staged"] == 1
        assert any(item["path"] == "renamed with spaces.txt" and item["index"] == "R"
                   for item in renamed["files"])
        assert all(item["path"] != "staged name.txt" for item in renamed["files"])
        git("commit", "-m", "Rename fixture")
        git("merge", "--no-edit", "local-upstream", success=False)
        conflicted = http_req(ctx["url"] + "/api/git")[1]
        assert conflicted["conflicted"] == 1 and conflicted["state"] == "dirty", conflicted
        assert any(item["path"] == "tracked file.txt" and item["index"] == item["worktree"] == "U"
                   for item in conflicted["files"])
        for number in range(205):
            (ctx["proj"] / f"untracked-{number:03}.txt").write_text("untracked\n", encoding="utf-8")
        bounded = http_req(ctx["url"] + "/api/git")[1]
        assert bounded["truncated"] is True and len(bounded["files"]) == 200
        assert bounded["untracked"] == 206 and bounded["conflicted"] == 1
        assert board(ctx) == before

    def test_agent_http_context_and_extension_roundtrips(self):
        ctx = self.new_board()
        path = ctx["path"]
        card = create(ctx, "Extensions survive every writer")
        cid = card["id"]
        with core.board_transaction(path) as doc:
            target = core.resolve_ref(doc, cid)
            doc["vendorDocument"] = {"retain": True}
            doc["columns"][0]["vendorColumn"] = {"retain": True}
            target["vendorCard"] = {"retain": True}
            target["subtasks"] = [{"id": "extension-step", "text": "Before", "done": False,
                                   "vendorSubtask": {"retain": True}, "children": []}]
            core.save(path, doc, by="Ada")
        # An older browser omits extensions; arbitrary client additions remain uneditable.
        snapshot = board(ctx)
        snapshot.pop("vendorDocument")
        snapshot["columns"][0].pop("vendorColumn")
        raw = next(item for item in snapshot["cards"] if item["id"] == cid)
        raw.pop("vendorCard")
        raw["subtasks"][0].pop("vendorSubtask")
        raw["subtasks"][0]["text"] = "Edited through an older browser"
        raw["forgedExtension"] = {"untrusted": True}
        status, saved, _ = full_post(ctx, snapshot)
        assert status == 200, saved
        document = saved["document"]
        assert document["vendorDocument"] == {"retain": True}
        assert document["columns"][0]["vendorColumn"] == {"retain": True}
        target = next(item for item in document["cards"] if item["id"] == cid)
        assert target["vendorCard"] == {"retain": True} and "forgedExtension" not in target
        assert target["subtasks"][0]["vendorSubtask"] == {"retain": True}
        assert target["subtasks"][0]["text"] == "Edited through an older browser"
        status, patched, _ = mutate(ctx, f"/api/card/{cid}", {
            "card": {"title": "Card scoped", "vendorCard": "forged",
                     "subtasks": [{"id": "extension-step", "text": "Patched", "done": True, "children": []}]}})
        assert status == 200 and patched["card"]["vendorCard"] == {"retain": True}
        assert patched["card"]["subtasks"][0]["vendorSubtask"] == {"retain": True}
        columns = [{key: value for key, value in column.items() if key != "vendorColumn"}
                   for column in board(ctx)["columns"]]
        status, structured, _ = mutate(ctx, "/api/structure", {"operation": {
            "type": "update-columns", "columns": columns}})
        assert status == 200 and structured["document"]["columns"][0]["vendorColumn"] == {"retain": True}
        source = ctx["proj"] / "shared-source.bin"
        content = b"CLI upload, HTTP download\x00\xff"
        source.write_bytes(content)
        revision = board(ctx)["rev"]
        uploaded = run(["--board", ctx["proj"], "attachment", cid, "add", "--file", source,
                        "--expected-rev", revision, "--json"], cwd=self.base, env=self.env)
        assert uploaded.returncode == 0, (uploaded.stdout, uploaded.stderr)
        metadata = last_json(uploaded)["item"]
        download = ctx["url"] + f"/api/card/{cid}/attachments/{metadata['id']}"
        assert http_req(download)[1] == content
        stale, conflict, _ = mutate(ctx, f"/api/card/{cid}/comments",
                                    {"operation": {"type": "add", "text": "Stale"}}, rev=revision)
        assert stale == 409 and conflict["rev"] == revision + 1
        assert mutate(ctx, f"/api/card/{cid}/comments",
                      {"operation": {"type": "add", "text": "Browser discussion"}})[0] == 200
        context = run(["--board", ctx["proj"], "context", cid, "--json"], cwd=self.base, env=self.env)
        assert context.returncode == 0, (context.stdout, context.stderr)
        payload = last_json(context)
        assert payload["card"]["comments"][-1]["text"] == "Browser discussion"
        assert payload["card"]["attachments"] == [metadata]
        status, http_context, _ = http_req(ctx["url"] + f"/api/card/{cid}/context")
        assert status == 200, {"status": status, "http": http_context}
        cli_board, http_board = Path(payload["board"]), Path(http_context["board"])
        assert cli_board.is_absolute() and http_board.is_absolute() and cli_board.resolve() == http_board.resolve()
        assert http_context == {**payload, "board": http_context["board"]}
        assert mutate(ctx, f"/api/card/{cid}/attachments/{metadata['id']}", {}, method="DELETE")[0] == 200
        assert core.attachment_path(path, metadata["id"]).read_bytes() == content
        assert http_req(download)[0] == 404

    def test_health_and_corrupt_documents(self):
        ctx = self.new_board()
        path = ctx["path"]
        healthy = path.read_bytes()
        registry = core.registry_path()
        registry_bytes = registry.read_bytes()
        try:
            status, health, _ = http_req(self.origin + "/health")
            assert status == 200 and health["ok"] is True and health["app"] == "workboard", health
            assert health["version"] == __version__ and health["apiVersion"] == core.API_VERSION
            assert health["schemaVersion"] == core.SCHEMA_VERSION
            assert health["port"] == ctx["port"] and health["pid"] == self.server.info["pid"]
            assert health["boards"] == len(json.loads(registry_bytes)["boards"]) and health["startedAt"]
            assert health["registryError"] is None and isinstance(health["sseClients"], int)
            assert set(core.CAPABILITIES) <= set(health["capabilities"])
            raw = json.loads(healthy)
            raw["schemaVersion"] = core.SCHEMA_VERSION + 1
            path.write_text(json.dumps(raw), encoding="utf-8")
            before = path.read_bytes()
            assert http_req(ctx["url"] + "/board.json")[0] == 409
            response = http_req(ctx["url"] + "/api/structure", "PATCH", {
                "baseRev": raw["rev"], "operation": {"type": "sort-cards", "mode": "title"}})
            assert response[0] == 409 and path.read_bytes() == before
            entry = self.listing()[ctx["name"]]
            assert entry["exists"] is True and entry["rev"] is None and entry["error"]
            assert http_req(self.origin + "/health")[0] == 200
            path.write_bytes(healthy)
            registry.write_text("{broken", encoding="utf-8")
            assert http_req(self.origin + "/api/boards")[0] == 500
            assert http_req(ctx["url"] + "/board.json")[0] == 500
            status, health, _ = http_req(self.origin + "/health")
            assert status == 200 and health["boards"] is None and health["registryError"]
            assert registry.read_text(encoding="utf-8") == "{broken"
        finally:
            path.write_bytes(healthy)
            registry.write_bytes(registry_bytes)


class ServerLifecycleTest(unittest.TestCase):
    """server.json, token shutdown, port conflicts and the in-process helpers."""

    def setUp(self):
        self.base = self.enterContext(scratch("wb-server-life-"))
        self.env = make_env(self.base / "home")
        self.state = self.base / "home" / ".workboard" / "server.json"

    def start(self) -> RunningServer:
        running = RunningServer(self.base, self.env)
        self.addCleanup(running.stop)
        return running

    def test_server_json_and_token_shutdown(self):
        running = self.start()
        self.assertEqual(set(running.info), {"ok", "url", "pid", "port", "version"})
        self.assertEqual((running.info["url"], running.info["version"]),
                         (f"http://127.0.0.1:{running.port}/", __version__))
        self.assertNotEqual(running.port, 0)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(set(state), {"pid", "port", "url", "version", "startedAt", "executable", "token"})
        self.assertEqual((state["pid"], state["port"], state["url"], state["version"]),
                         (running.info["pid"], running.port, running.info["url"], __version__))
        self.assertGreaterEqual(len(state["token"]), 32)
        health = http_req(running.root + "/health")[1]
        self.assertEqual((health["pid"], health["startedAt"]), (state["pid"], state["startedAt"]))
        shutdown = running.root + "/api/shutdown"
        for headers in ({}, {"X-WorkBoard-Token": "wrong"}, {"X-WorkBoard-Token": state["token"][:-1]},
                        {"X-WorkBoard-Token": state["token"], "Host": "evil.example"},
                        {"X-WorkBoard-Token": state["token"], "Origin": "http://evil.example"}):
            self.assertEqual(http_req(shutdown, "POST", b"", headers)[0], 403, headers)
        self.assertEqual(http_req(shutdown, headers={"X-WorkBoard-Token": state["token"]})[0], 404)
        self.assertEqual(http_req(running.root + "/health")[0], 200)
        self.assertTrue(self.state.exists())
        status, body, _ = http_req(shutdown, "POST", b"", {"X-WorkBoard-Token": state["token"]})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(running.proc.wait(timeout=15), 0)
        self.assertFalse(self.state.exists())

    def test_second_serve_reports_already_running(self):
        running = self.start()
        again = run(["serve", "--port", running.port], cwd=self.base, env=self.env, timeout=30)
        self.assertEqual(again.returncode, 0, (again.stdout, again.stderr))
        self.assertEqual(again.stdout.strip(), f"already running: http://127.0.0.1:{running.port}/")
        again = run(["serve", "--port", running.port, "--json"], cwd=self.base, env=self.env, timeout=30)
        self.assertEqual(again.returncode, 0, (again.stdout, again.stderr))
        reported = last_json(again)
        self.assertEqual((reported["ok"], reported["alreadyRunning"], reported["pid"], reported["port"]),
                         (True, True, running.info["pid"], running.port))
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["pid"], running.info["pid"])
        self.assertEqual(http_req(running.root + "/health")[0], 200)

    def test_port_owned_by_another_program_is_a_state_error(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            port = blocker.getsockname()[1]
            result = run(["serve", "--port", port, "--json"], cwd=self.base, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        error = last_json(result)
        self.assertEqual((error["ok"], error["status"], error["code"]), (False, 409, "state"))
        self.assertIn(f"port {port} is in use by another program", error["error"])
        self.assertFalse(self.state.exists())

    def test_server_info_and_stop_in_process(self):
        idle_port = free_port()
        with mock.patch.dict(os.environ, {**self.env, "WORKBOARD_PORT": str(idle_port)}, clear=True):
            self.assertEqual(server.server_url(), f"http://127.0.0.1:{idle_port}/")
            self.assertEqual(server.board_url("Gamma board/2", 1234), "http://127.0.0.1:1234/b/Gamma%20board%2F2/")
            self.assertIsNone(server.server_info())
            self.assertTrue(server.stop())
            running = self.start()
            info = server.server_info()
            self.assertIsNotNone(info)
            self.assertEqual((info["app"], info["port"], info["pid"]),
                             ("workboard", running.port, running.info["pid"]))
            self.assertTrue(server.stop())
            self.assertIsNone(server.server_info())
            self.assertEqual(running.proc.wait(timeout=15), 0)
            self.assertFalse(self.state.exists())
            self.assertTrue(server.stop())

    def test_service_mode_logs_to_rotated_file(self):
        log = self.base / "home" / ".workboard" / "logs" / "server.log"
        log.parent.mkdir(parents=True)
        oversized = b"x" * (5 * 1024 * 1024 + 1)
        log.write_bytes(oversized)
        proc = subprocess.Popen(
            [*command(), "serve", "--service", "--port", "0"], cwd=str(self.base), env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW)
        state = {}
        try:
            state = wait_for_state(self.state, proc)
            self.assertEqual(http_req(f"http://127.0.0.1:{state['port']}/health")[1]["pid"], state["pid"])
        finally:
            if state:
                try:
                    http_req(f"http://127.0.0.1:{state['port']}/api/shutdown", "POST", b"",
                             {"X-WorkBoard-Token": state["token"]})
                except OSError:
                    pass
            try:
                stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual((stdout, stderr), (b"", b""))
        self.assertEqual(log.with_name("server.log.1").read_bytes(), oversized)
        self.assertRegex(log.read_text(encoding="utf-8"),
                         rf"serving 0 boards at http://127\.0\.0\.1:{state['port']}/ \(Ctrl\+C to stop\)")
        deadline = time.monotonic() + 15
        while self.state.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.state.exists())


class BrowserStampTest(unittest.TestCase):
    """Preview smoke c26: changedRev is server-owned and browser guards stay board-scoped."""

    def test_browser_changed_rev_is_server_owned(self):
        base = self.enterContext(scratch("wb-server-c26-"))
        self.enterContext(mock.patch.dict(os.environ, make_env(base / "home"), clear=True))
        path = base / "browser-stamps" / "board" / "board.json"
        path.parent.mkdir(parents=True)
        doc = core.normalize_doc({"name": "Browser stamps",
                                  "cards": [{"num": 1, "id": "x", "title": "X"},
                                            {"num": 2, "id": "y", "title": "Y"}]})
        core.save(path, doc, by="tester")
        core.register_board("stamps", path)
        before = path.read_bytes()
        httpd = server.Server(("127.0.0.1", 0))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def close():
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)

        self.addCleanup(close)
        prefix = f"http://127.0.0.1:{httpd.server_port}/b/stamps"

        def request(endpoint, payload, method="PATCH", headers=None):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            status, body, _ = http_req(prefix + endpoint, method, raw, {
                "Content-Type": "application/json", **(headers or {})})
            return status, body

        rev = doc["rev"]
        status, _ = request("/api/card/x", {"baseRev": rev, "card": {"changedRev": 0, "title": "forged"}})
        assert status == 422 and path.read_bytes() == before
        snapshot = json.loads(before)
        snapshot["cards"][0]["changedRev"] = rev + 100
        status, _ = request("/board.json", {**snapshot, "baseRev": rev}, method="POST")
        assert status == 422 and path.read_bytes() == before
        status, _ = request("/api/structure", {"baseRev": rev, "operation": {
            "type": "create-card", "card": {"id": "forged", "title": "Forged", "changedRev": rev + 100}}})
        assert status == 422 and path.read_bytes() == before
        status, saved = request("/api/card/x", {"baseRev": rev, "card": {"title": "Older browser"}})
        assert status == 200 and saved["card"]["changedRev"] == saved["rev"] == rev + 1
        reviewed = saved["rev"]
        status, other = request("/api/card/y", {"baseRev": reviewed, "card": {"title": "Y changed"}})
        assert status == 200
        before = path.read_bytes()
        status, conflict = request("/api/card/x", {"baseRev": reviewed, "card": {"title": "Stale browser"}})
        assert status == 409 and conflict["rev"] == other["rev"] and path.read_bytes() == before
        status, _ = request("/api/card/x/attachments?name=stale.txt", b"stale", method="POST",
                            headers={"Content-Type": "text/plain", "X-Board-Base-Rev": str(reviewed)})
        assert status == 409 and path.read_bytes() == before
        latest, _, metadata = core.attachment_add(path, "x", "safe.txt", b"safe", "tester",
                                                  expected_rev=reviewed)
        status, _ = request("/api/card/y", {"baseRev": latest["rev"], "card": {"title": "Y again"}})
        assert status == 200
        before = path.read_bytes()
        status, _ = request(f"/api/card/x/attachments/{metadata['id']}",
                            {"baseRev": latest["rev"]}, method="DELETE")
        assert status == 409 and path.read_bytes() == before


if __name__ == "__main__":
    unittest.main()
