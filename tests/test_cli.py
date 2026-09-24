"""End-to-end CLI contract tests (ported from the preview smoke checks c01-c28).

Every product interaction runs the real CLI (``python -m workboard`` or
$WORKBOARD_TEST_COMMAND) in throwaway projects under one scratch home; the
in-process checks import ``workboard.core`` with os.environ pointed at the same
scratch home. The browser changedRev check (c26) lives in test_server.py.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import last_json, make_env, run, scratch

from workboard import __version__, core

CORE_COLUMNS = ["backlog", "task", "inprogress", "done", "blocked"]
BASE = HOME = Path()
ENV: dict = {}


def setUpModule():
    global BASE, HOME, ENV
    BASE = unittest.enterModuleContext(scratch("wb-cli-"))
    HOME = BASE / "home"
    HOME.mkdir()
    ENV = make_env(HOME)
    unittest.enterModuleContext(mock.patch.dict(os.environ, ENV, clear=True))


def wb(args, cwd, env=None, **kwargs) -> subprocess.CompletedProcess:
    return run(args, cwd=cwd, env=ENV if env is None else env, **kwargs)


def project(name: str) -> Path:
    path = BASE / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def detail(proc) -> str:
    return f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}"


def read_board(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def has_ref(text: str, num: int) -> bool:
    return re.search(rf"#{num}(?!\d)", text) is not None


def utc_today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


class BoardLifecycle(unittest.TestCase):
    """c01-c14 share one board; methods run in name order and build on each other."""

    @classmethod
    def setUpClass(cls):
        cls.proj = project("proj-main")
        cls.board = cls.proj / "board" / "board.json"

    def show(self, ref, *extra) -> dict:
        proc = wb(["show", ref, *extra], self.proj)
        self.assertEqual(proc.returncode, 0, detail(proc))
        return json.loads(proc.stdout)

    def test_c01_init_registers_board_and_starts_no_server(self):
        r = wb(["init", "smoke-board"], self.proj)
        self.assertEqual(r.returncode, 0, detail(r))
        self.assertTrue(self.board.is_file())
        self.assertTrue(r.stdout.startswith("board created:"), r.stdout)
        self.assertIn("workboard open", r.stdout)
        doc = read_board(self.board)
        self.assertEqual(doc["name"], "smoke-board")
        self.assertIsInstance(doc["rev"], int)
        self.assertEqual([c["id"] for c in doc["columns"]], CORE_COLUMNS)

        ordinary_env = dict(ENV)
        ordinary_env.pop("WORKBOARD_ACTOR")
        for args in (["digest"], ["query"], ["list"], ["next"], ["which"], ["boards"]):
            proc = wb(args, self.proj, ordinary_env)
            self.assertEqual(proc.returncode, 0, (args, detail(proc)))
        state = HOME / ".workboard"
        self.assertFalse((state / "server.json").exists(), "an ordinary command started a server")
        self.assertFalse((state / "logs").exists(), "an ordinary command started a server")

        registry = json.loads((state / "boards.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(registry["boards"]["smoke-board"]), self.board.resolve())
        listed = wb(["boards"], self.proj)
        self.assertEqual(listed.returncode, 0, detail(listed))
        self.assertIn("smoke-board", listed.stdout)
        self.assertIn("http://127.0.0.1:7891/b/smoke-board/", listed.stdout)

    def test_c02_add_flags_and_json_postconditions(self):
        r = wb(["add", "--title", "first task", "--tag", "alpha", "--priority", "critical",
                "--origin", "smoke-origin-1"], self.proj)
        self.assertEqual(r.returncode, 0, detail(r))
        self.assertRegex(r.stdout, r"#1 .* added → task")
        r2 = wb(["add", "--title", "update me", "--tag", "keepme"], self.proj)
        self.assertEqual(r2.returncode, 0, detail(r2))
        self.assertRegex(r2.stdout, r"#2 .* added → task")

        pj = project("proj-json")
        created = wb(["init", "jsoncheck"], pj)
        self.assertEqual(created.returncode, 0, detail(created))
        added = wb(["add", "--title", "json probe", "--json"], pj)
        payload = last_json(added)
        self.assertEqual(added.returncode, 0, detail(added))
        self.assertIs(payload["ok"], True)
        self.assertEqual((payload["num"], payload["column"], payload["actor"]), (1, "task", "tester"))
        self.assertTrue(payload["id"])
        self.assertIsInstance(payload["rev"], int)

        results = [last_json(wb(args, pj)) for args in (
            ["update", "1", "--notes", "json update", "--json"],
            ["note", "1", "--text", "json note", "--json"],
            ["subtask", "1", "add", "json subtask", "--json"])]
        for result in results:
            self.assertIs(result["ok"], True, result)
            self.assertEqual((result["id"], result["num"], result["actor"]),
                             (payload["id"], 1, "tester"))
        revs = [payload["rev"], *(result["rev"] for result in results)]
        self.assertTrue(all(isinstance(rev, int) for rev in revs), revs)
        self.assertEqual(revs, sorted(revs))
        self.assertEqual(len(set(revs)), 4)
        item = results[2]["item"]
        self.assertEqual(results[2]["items"], [item])
        self.assertEqual((item["text"], item["by"]), ("json subtask", "tester"))

    def test_c03_start_and_digest(self):
        r = wb(["start", "1"], self.proj)
        self.assertEqual(r.returncode, 0, detail(r))
        card = self.show("1")
        self.assertEqual((card["column"], card["activeOwner"]), ("inprogress", "tester"))
        self.assertTrue(card["claimedAt"])
        digest = wb(["digest"], self.proj)
        self.assertEqual(digest.returncode, 0, detail(digest))
        self.assertRegex(digest.stdout, r"IN PROGRESS: #1 @tester\b")
        self.assertIn("MINE @tester:", digest.stdout)

    def test_c04_subtasks(self):
        a1 = wb(["subtask", "1", "add", "step one"], self.proj)
        a2 = wb(["subtask", "1", "add", "step two"], self.proj)
        self.assertEqual((a1.returncode, a2.returncode), (0, 0), (detail(a1), detail(a2)))
        self.assertIn("[s-1]", a1.stdout)
        self.assertIn("[s-2]", a2.stdout)
        done = wb(["subtask", "1", "done", "s-1"], self.proj)
        self.assertEqual(done.returncode, 0, detail(done))
        self.assertIn("1/2 done", done.stdout)
        nest = wb(["subtask", "1", "add", "nested leaf", "--parent", "s-2"], self.proj)
        self.assertEqual(nest.returncode, 0, detail(nest))
        self.assertIn("[s-3]", nest.stdout)
        s2 = next(s for s in self.show("1", "--full")["subtasks"] if s["id"] == "s-2")
        self.assertIn("s-3", [child["id"] for child in s2.get("children", [])])
        removed = wb(["subtask", "1", "rm", "s-1"], self.proj)
        self.assertEqual(removed.returncode, 0, detail(removed))
        self.assertIn("[s-1]", removed.stdout)
        self.assertIn("removed", removed.stdout)
        self.assertEqual([s["id"] for s in self.show("1", "--full")["subtasks"]], ["s-2"])
        for sid in ("s-2", "s-3"):
            proc = wb(["subtask", "1", "done", sid], self.proj)
            self.assertEqual(proc.returncode, 0, detail(proc))
        attention = wb(["digest"], self.proj)
        self.assertIn("IN PROGRESS: #1", attention.stdout)
        self.assertIn("READY TO CLOSE", attention.stdout)
        pulse = last_json(wb(["digest", "--json"], self.proj))
        self.assertEqual(next(c for c in pulse["attention"] if c["num"] == 1)["owner"], "tester")

    def test_c05_update_fields(self):
        u = wb(["update", "2", "--title", "Renamed Card", "--priority", "low",
                "--add-tag", "extratag", "--rm-tag", "keepme"], self.proj)
        self.assertEqual(u.returncode, 0, detail(u))
        self.assertIn("updated:", u.stdout)
        for token in ("title", "priority", "+tag", "-tag"):
            self.assertIn(token, u.stdout)
        card = self.show("2")
        self.assertEqual((card["title"], card["priority"], card["tags"]),
                         ("Renamed Card", "low", ["extratag"]))

    def test_c06_note_appends_timestamped_text(self):
        n = wb(["note", "2", "--text", "hello note"], self.proj)
        self.assertEqual(n.returncode, 0, detail(n))
        self.assertIn("note added", n.stdout)
        self.assertIn(f"[{utc_today()} tester] hello note", self.show("2", "--full")["notes"])

    def test_c07_done_requires_writeup_and_active_work(self):
        before = self.board.read_bytes()
        no_writeup = wb(["done", "2"], self.proj)
        no_claim = wb(["done", "2", "--writeup", "not started"], self.proj)
        self.assertNotEqual(no_writeup.returncode, 0)
        self.assertNotEqual(no_claim.returncode, 0)
        self.assertEqual(self.board.read_bytes(), before)
        start = wb(["start", "2"], self.proj)
        good = wb(["done", "2", "--writeup", "shipped it"], self.proj)
        self.assertEqual((start.returncode, good.returncode), (0, 0), (detail(start), detail(good)))
        card = next(c for c in read_board(self.board)["cards"] if c["num"] == 2)
        self.assertEqual((card["column"], card["outcome"], card["writeup"]),
                         ("done", "completed", "shipped it"))
        self.assertTrue(card["doneAt"])
        self.assertIsNone(card["activeOwner"])
        self.assertIsNone(card["claimedAt"])

    def test_c08_bug_cycle(self):
        steps = [wb(args, self.proj) for args in (
            ["add", "--title", "buggy feature"], ["start", "3"],
            ["done", "3", "--writeup", "v1 shipped"], ["bug", "3", "--reason", "crash on load"])]
        for proc in steps:
            self.assertEqual(proc.returncode, 0, detail(proc))
        card = self.show("3")
        self.assertEqual((card["column"], card["activeOwner"]), ("inprogress", "tester"))
        self.assertIn("bug", card["tags"])
        self.assertTrue(any(st["text"] == "fix bug: crash on load" and not st["done"]
                            for st in card["subtasks"]), card["subtasks"])
        self.assertEqual(card["cycles"][-1]["writeup"], "v1 shipped")
        self.assertEqual((card["writeup"], card["reworkReason"]), ("", "crash on load"))
        fixed = wb(["done", "3", "--writeup", "fix applied"], self.proj)
        self.assertEqual(fixed.returncode, 0, detail(fixed))
        card = self.show("3")
        self.assertEqual(card["outcome"], "completed")
        self.assertIsNone(card["reworkReason"])
        self.assertIsNone(card["activeOwner"])

    def test_c09_improve_and_reopen(self):
        imp = wb(["improve", "3", "make it faster"], self.proj)
        self.assertEqual(imp.returncode, 0, detail(imp))
        improved = self.show("3")
        self.assertEqual((improved["column"], improved["activeOwner"]), ("inprogress", "tester"))
        self.assertEqual(improved["cycles"][-1]["writeup"], "fix applied")
        self.assertTrue(any(st["text"] == "make it faster" and not st["done"]
                            for st in improved["subtasks"]))
        reopened = wb(["reopen", "3", "--reason", "needs more testing"], self.proj)
        self.assertEqual(reopened.returncode, 0, detail(reopened))
        card = self.show("3")
        self.assertEqual((card["column"], card["writeup"]), ("task", ""))
        self.assertEqual(card["reopenReason"], "needs more testing")
        self.assertEqual(card["reworkReason"], "needs more testing")
        self.assertIsNone(card["activeOwner"])
        self.assertEqual(card["cycles"], improved["cycles"])

    def test_c10_core_columns_and_actionable_blocked_transitions(self):
        added = wb(["add", "--title", "waiting on api"], self.proj)
        custom = wb(["fly", "4", "waiting", "--note", "external dep"], self.proj)
        direct = wb(["fly", "4", "blocked", "--note", "external dep"], self.proj)
        incomplete = wb(["block", "4", "--reason", "API unavailable"], self.proj)
        blocked = wb(["block", "4", "--reason", "API unavailable",
                      "--until", "credentials arrive"], self.proj)
        digest = wb(["digest"], self.proj)
        leave = wb(["fly", "4", "task"], self.proj)
        resume = wb(["resume", "4", "--note", "credentials received", "--to", "task"], self.proj)
        self.assertEqual(added.returncode, 0, detail(added))
        self.assertNotEqual(leave.returncode, 0, "fly must not bypass resume out of Blocked")
        self.assertNotEqual(custom.returncode, 0)
        self.assertIn("core column", custom.stdout + custom.stderr)
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn("use block", direct.stdout + direct.stderr)
        self.assertNotEqual(incomplete.returncode, 0)
        self.assertEqual(blocked.returncode, 0, detail(blocked))
        self.assertIn("API unavailable; until credentials arrive", digest.stdout)
        self.assertEqual(resume.returncode, 0, detail(resume))
        card = self.show("4")
        self.assertEqual(card["column"], "task")
        self.assertIsNone(card["blockedReason"])
        self.assertIsNone(card["unblockWhen"])

    def test_c11_query_search_list_and_show_truncation(self):
        a5 = wb(["add", "--title", "alpha beta payload", "--origin", "deep origin story"], self.proj)
        self.assertEqual(a5.returncode, 0, detail(a5))
        self.assertIn("#5", a5.stdout)
        q = wb(["query", "--fields", "num,title,column"], self.proj)
        self.assertEqual(q.returncode, 0, detail(q))
        payload = json.loads(q.stdout)
        rows = payload["cards"]
        self.assertIs(payload["ok"], True)
        self.assertIsInstance(payload["rev"], int)
        self.assertGreaterEqual(len(rows), 5)
        self.assertTrue(all(set(row) == {"num", "title", "column"} for row in rows), rows)
        self.assertEqual(next(row for row in rows if row["num"] == 1)["column"], "inprogress")

        hit = wb(["search", "alpha", "payload"], self.proj)
        miss = wb(["search", "alpha", "zzzznomatchxyz"], self.proj)
        self.assertEqual((hit.returncode, miss.returncode), (0, 0))
        self.assertTrue(has_ref(hit.stdout, 5), hit.stdout)
        self.assertIn("(no matches)", miss.stdout)

        listed = wb(["list", "--column", "inprogress"], self.proj)
        self.assertEqual(listed.returncode, 0, detail(listed))
        self.assertTrue(has_ref(listed.stdout, 1))
        self.assertFalse(has_ref(listed.stdout, 4) or has_ref(listed.stdout, 5), listed.stdout)

        long_notes = "N" * 500
        self.assertEqual(wb(["update", "5", "--notes", long_notes], self.proj).returncode, 0)
        self.assertNotEqual(self.show("5")["notes"], long_notes)
        full = last_json(wb(["show", "5", "--full", "--json"], self.proj))
        self.assertIs(full["ok"], True)
        self.assertEqual(full["card"]["notes"], long_notes)
        self.assertEqual(full["rev"], read_board(self.board)["rev"])

    def test_c12_ref_resolution(self):
        a6 = wb(["add", "--title", "alpha other"], self.proj)
        self.assertEqual(a6.returncode, 0, detail(a6))
        self.assertIn("#6", a6.stdout)
        ids = {row["num"]: row["id"] for row in json.loads(
            wb(["query", "--fields", "num,id"], self.proj).stdout)["cards"]}
        id5, id6 = ids[5], ids[6]
        prefix = next(id5[:i] for i in range(1, len(id5) + 1)
                      if sum(value.startswith(id5[:i]) for value in ids.values()) == 1)
        for ref in ("5", "#5", id5, prefix):
            self.assertEqual(self.show(ref)["num"], 5, ref)
        ambiguous = wb(["show", "alpha"], self.proj)
        output = ambiguous.stdout + ambiguous.stderr
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("ambiguous", output)
        self.assertIn(id5, output)
        self.assertIn(id6, output)
        self.assertNotEqual(wb(["show", "zzz-no-such-card"], self.proj).returncode, 0)

    def test_c13_sweep_archive_and_recover(self):
        month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        archive = self.proj / "board" / "archive" / f"board-{month}.json"
        rev_before = read_board(self.board)["rev"]
        dry = wb(["sweep", "--days", "0"], self.proj)
        self.assertEqual(dry.returncode, 0, detail(dry))
        self.assertEqual(read_board(self.board)["rev"], rev_before)
        applied = wb(["sweep", "--days", "0", "--apply", "--json"], self.proj)
        self.assertEqual(applied.returncode, 0, detail(applied))
        self.assertEqual(last_json(applied)["actor"], "tester")
        self.assertEqual([c["num"] for c in read_board(archive)["cards"]], [2])
        after = read_board(self.board)
        self.assertEqual(len(after["cards"]), 5)

        listed = wb(["recover", "--json"], self.proj)
        self.assertEqual(listed.returncode, 0, detail(listed))
        self.assertIn(rev_before, [item["rev"] for item in last_json(listed)["backups"]])
        restore = wb(["recover", str(rev_before), "--apply", "--json"], self.proj)
        self.assertEqual(restore.returncode, 0, detail(restore))
        self.assertEqual(last_json(restore)["actor"], "tester")
        restored = read_board(self.board)
        self.assertEqual({c["num"] for c in restored["cards"]}, {1, 2, 3, 4, 5, 6})
        self.assertGreater(restored["rev"], after["rev"])

        repeat = wb(["sweep", "--days", "0", "--apply"], self.proj)
        self.assertEqual(repeat.returncode, 0, detail(repeat))
        self.assertEqual([c["num"] for c in read_board(archive)["cards"]], [2])
        self.assertEqual(len(read_board(self.board)["cards"]), 5)

    def test_c14_utf8_emoji_roundtrip(self):
        title = "rocket ship 🚀 launch"
        added = wb(["add", "--title", title, "--json"], self.proj)
        self.assertEqual(added.returncode, 0, detail(added))
        num = last_json(added)["num"]
        started = wb(["start", num], self.proj)  # digest lists active cards only
        self.assertEqual(started.returncode, 0, detail(started))
        shown = wb(["show", num], self.proj)
        digest = wb(["digest"], self.proj)
        self.assertEqual(json.loads(shown.stdout)["title"], title)
        self.assertIn("🚀", digest.stdout)
        self.assertNotIn("\ufffd", added.stdout + added.stderr + shown.stdout + digest.stdout)


class Contracts(unittest.TestCase):
    """Independent checks; each builds its own project under the scratch home."""

    def init(self, name: str, directory: str | None = None) -> Path:
        root = project(directory or name)
        created = wb(["init", name, "--dir", root], BASE)
        self.assertEqual(created.returncode, 0, detail(created))
        return root

    def test_version_flag(self):
        proc = wb(["--version"], BASE)
        self.assertEqual(proc.returncode, 0, detail(proc))
        self.assertEqual(proc.stdout.strip(), f"workboard {__version__}")

    def test_boards_lists_server_urls(self):
        env = make_env(BASE / "urls-home")
        root = project("urls")
        self.assertEqual(wb(["init", "team/α b", "--dir", root], BASE, env).returncode, 0)
        listed = last_json(wb(["boards", "--json"], BASE, env))["boards"]
        self.assertEqual([(item["name"], item["url"], item["exists"]) for item in listed],
                         [("team/α b", "http://127.0.0.1:7891/b/team%2F%CE%B1%20b/", True)])
        human = wb(["boards"], BASE, {**env, "WORKBOARD_PORT": "45678"})
        self.assertIn("http://127.0.0.1:45678/b/team%2F%CE%B1%20b/", human.stdout)

    @unittest.skipIf(os.environ.get("WORKBOARD_TEST_COMMAND"),
                     "import-time evidence needs python -m workboard")
    def test_ordinary_verbs_never_import_server_or_browser(self):
        forbidden = {"workboard.server", "http.server", "webbrowser", "urllib.request"}
        root = project("import-cost")
        for args in (["init", "imports"], ["add", "--title", "probe"], ["start", "1"],
                     ["done", "1", "--writeup", "w"], ["digest"], ["query", "--json"], ["boards"]):
            proc = subprocess.run(
                [sys.executable, "-X", "importtime", "-m", "workboard", *args], cwd=root, env=ENV,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(proc.returncode, 0, (args, proc.stdout, proc.stderr[-2000:]))
            imported = {line.rsplit("|", 1)[-1].strip() for line in proc.stderr.splitlines()
                        if line.startswith("import time:")}
            self.assertIn("workboard.core", imported)
            self.assertFalse(imported & forbidden, (args, sorted(imported & forbidden)))
        self.assertFalse((HOME / ".workboard" / "server.json").exists())

    def test_c15_concurrent_adds_lose_nothing(self):
        root = self.init("conc-board")
        board = root / "board" / "board.json"
        initial = read_board(board)["rev"]
        results: list = []

        def adder(tag):
            for i in range(10):
                results.append(wb(["add", "--title", f"conc {tag}-{i}", "--json"], root))

        threads = [threading.Thread(target=adder, args=(tag,)) for tag in "AB"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        failures = [detail(proc) for proc in results if proc.returncode != 0
                    or last_json(proc).get("ok") is not True]
        self.assertEqual((len(results), failures), (20, []))
        doc = read_board(board)
        self.assertEqual(sorted(c["num"] for c in doc["cards"]), list(range(1, 21)))
        self.assertEqual(doc["rev"], initial + 20)

    def test_c16_lock_timeout_is_visible(self):
        root = self.init("lock-board")
        board = root / "board" / "board.json"
        before = board.read_bytes()
        with core.board_lock(board, timeout=8.0):
            proc = wb(["add", "--title", "lock victim"], root, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lock", (proc.stderr + proc.stdout).lower())
        self.assertEqual(board.read_bytes(), before)

    def test_c17_consolidates_legacy_columns(self):
        root = project("proj-migrate")
        board = root / "board" / "board.json"
        board.parent.mkdir(parents=True)
        now = "2026-08-20T00:00:00Z"
        columns = [
            {"id": "notes", "name": "Notes", "kind": "intake"},
            {"id": "ideas", "name": "Ideas", "kind": "intake"},
            {"id": "bugs", "name": "Bugs", "kind": "todo"},
            {"id": "review", "name": "In Review", "kind": "active"},
            {"id": "deployed", "name": "Deployed", "kind": "done"},
            {"id": "waiting", "name": "Waiting", "kind": "blocked"},
            {"id": "discarded", "name": "Discarded", "kind": "custom"},
        ]
        cards = [{"num": num, "id": f"legacy-{column}", "code": "", "title": f"legacy {column}",
                  "column": column, "priority": None, "tags": [], "origin": "", "notes": "",
                  "writeup": "", "subtasks": [], "links": [], "history": [], "cycles": [],
                  "createdAt": now, "updatedAt": now, "doneAt": None, "reopenReason": None}
                 for num, column in enumerate((c["id"] for c in columns), 1)]
        board.write_text(json.dumps({"schemaVersion": 2, "name": "legacy-migration", "rev": 0,
                                     "nextNum": 8, "columns": columns, "cards": cards}, indent=2),
                         encoding="utf-8")
        dry = wb(["columns-core", "--json"], root)
        applied = wb(["columns-core", "--apply", "--json"], root)
        self.assertEqual((dry.returncode, applied.returncode), (0, 0), (detail(dry), detail(applied)))
        self.assertIs(last_json(dry)["applied"], False)
        payload = last_json(applied)
        self.assertEqual(payload["columns"], CORE_COLUMNS)
        self.assertEqual(payload["actor"], "tester")
        doc = read_board(board)
        self.assertEqual([c["id"] for c in doc["columns"]], CORE_COLUMNS)
        by_id = {card["id"]: card for card in doc["cards"]}
        expected = {"legacy-notes": "backlog", "legacy-ideas": "backlog", "legacy-bugs": "task",
                    "legacy-review": "inprogress", "legacy-deployed": "done",
                    "legacy-waiting": "blocked"}
        self.assertEqual({key: card["column"] for key, card in by_id.items()}, expected)
        self.assertIn("note", by_id["legacy-notes"]["tags"])
        self.assertIn("idea", by_id["legacy-ideas"]["tags"])
        archived = read_board(Path(payload["archive"]))["cards"]
        self.assertEqual([card["id"] for card in archived], ["legacy-discarded"])

    def test_c18_wip_limit(self):
        root = self.init("wip-board")
        limit = wb(["wip", "1", "--json"], root)
        steps = [wb(args, root) for args in (["add", "--title", "first active"],
                                             ["add", "--title", "second active"], ["start", "1"])]
        full = wb(["start", "2"], root)
        blocked = wb(["block", "1", "--reason", "waiting", "--until", "dependency clears"], root)
        second = wb(["start", "2"], root)
        payload = last_json(limit)
        self.assertEqual((payload["wipLimit"], payload["actor"]), (1, "tester"))
        for proc in (*steps, blocked, second):
            self.assertEqual(proc.returncode, 0, detail(proc))
        self.assertNotEqual(full.returncode, 0)
        self.assertIn("WIP limit is 1", full.stdout + full.stderr)

    def test_c19_dependencies_readiness_and_outcomes(self):
        root = project("dependency-contract")
        (root / "board").mkdir()
        path = root / "board" / "board.json"
        doc = core.normalize_doc({"cards": [
            {"num": 1, "id": "pre", "column": "done", "writeup": "verified",
             "doneAt": "2020-01-01T00:00:00Z"},
            {"num": 2, "id": "dependent", "priority": "critical", "dependsOn": ["pre"],
             "createdAt": "2020-02-01T00:00:00Z"},
            {"num": 3, "id": "older-low", "priority": "low", "createdAt": "2019-01-01T00:00:00Z"},
            {"num": 4, "id": "mid", "priority": "mid", "createdAt": "2020-01-01T00:00:00Z"},
            {"num": 5, "id": "critical-old", "priority": "critical", "createdAt": "2020-01-01T00:00:00Z"},
            {"num": 6, "id": "missing", "dependsOn": ["deleted-predecessor"]},
            {"num": 7, "id": "canceled", "column": "done", "outcome": "canceled",
             "cancelReason": "not needed", "doneAt": "2020-01-01T00:00:00Z"},
            {"num": 8, "id": "canceled-child", "dependsOn": ["canceled"]},
        ]})
        by_id = {c["id"]: c for c in doc["cards"]}
        before = json.dumps(doc, sort_keys=True)
        ready = core.ready_cards(doc)
        stats = core.board_stats(doc)
        self.assertEqual([c["id"] for c in ready], ["critical-old", "dependent", "mid", "older-low"])
        self.assertEqual(json.dumps(doc, sort_keys=True), before)
        self.assertEqual((stats["ready"], stats["completed"], stats["canceled"], stats["open"],
                          stats["completedLast7Days"]), (4, 1, 1, 6, 0))

        def rejected(card_id, action, details, status):
            snapshot = json.dumps(doc, sort_keys=True)
            try:
                core.workflow_action(doc, by_id[card_id], action, details, "tester")
            except core.WorkflowError as exc:
                return exc.status == status and json.dumps(doc, sort_keys=True) == snapshot
            return False

        self.assertTrue(rejected("pre", "dependencies", {"ids": ["dependent"]}, 422))
        self.assertTrue(rejected("mid", "dependencies", {"ids": ["mid"]}, 422))
        self.assertTrue(rejected("mid", "dependencies", {"ids": ["ghost"]}, 422))
        self.assertTrue(rejected("missing", "start", {}, 409))
        self.assertTrue(rejected("canceled-child", "move", {"to": "inprogress"}, 409))
        self.assertTrue(rejected("missing", "bug", {"reason": "cannot skip prerequisite"}, 409))
        self.assertEqual(core._sweep_candidates(doc, 14), [])
        core.workflow_action(doc, by_id["canceled"], "move", {"to": "done"}, "tester")
        self.assertEqual(by_id["canceled"]["outcome"], "canceled")
        self.assertTrue(rejected("canceled", "complete", {"writeup": "bypass"}, 409))
        self.assertTrue(rejected("canceled", "cancel", {"reason": "again"}, 409))
        self.assertTrue(rejected("pre", "move", {"to": "task"}, 409))
        core.workflow_action(doc, by_id["pre"], "rework", {"reason": "evidence invalid"}, "tester")
        pre = by_id["pre"]
        self.assertEqual((pre["column"], pre["outcome"], pre["doneAt"], pre["writeup"]),
                         ("task", None, None, ""))
        self.assertEqual((pre["cycles"][-1]["writeup"], pre["cycles"][-1]["reason"]),
                         ("verified", "evidence invalid"))
        self.assertNotIn("dependent", [c["id"] for c in core.ready_cards(doc)])
        self.assertTrue(rejected("dependent", "start", {}, 409))
        core.workflow_action(doc, pre, "start", {}, "tester")
        core.workflow_action(doc, pre, "complete", {"writeup": "reverified"}, "tester")
        core.workflow_action(doc, by_id["mid"], "cancel", {"reason": "superseded"}, "tester")
        core.workflow_action(doc, by_id["canceled"], "rework", {"reason": "needed again"}, "tester")
        stats = core.board_stats(doc)
        self.assertEqual((stats["completed"], stats["canceled"], stats["rework"],
                          stats["completedLast7Days"]), (1, 1, 1, 1))
        self.assertIsNone(pre["reworkReason"])
        self.assertEqual(by_id["mid"]["cancelReason"], "superseded")
        self.assertEqual(by_id["canceled"]["cycles"][-1]["outcome"], "canceled")
        self.assertIsNone(by_id["canceled"]["cancelReason"])

        with core.board_lock(path):
            core.save(path, doc, by="tester")
        before = path.read_bytes()
        backups = sorted(p.name for p in (path.parent / core.BACKUP_DIR).iterdir())
        result = wb(["next", "--limit", "2", "--json"], root)
        payload = last_json(result)
        self.assertEqual(result.returncode, 0, detail(result))
        self.assertEqual(payload["rev"], doc["rev"])
        self.assertEqual([c["id"] for c in payload["cards"]], ["critical-old", "dependent"])
        self.assertTrue(all(set(c) == {"num", "id", "title", "priority", "tags", "createdAt",
                                       "dependsOn"} for c in payload["cards"]))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (path.parent / core.BACKUP_DIR).iterdir()), backups)
        remove = wb(["depends", "missing", "--remove", "deleted-predecessor", "--json"], root)
        add = wb(["depends", "missing", "--on", "#1", "--on", "pre", "--json"], root)
        clear = wb(["depends", "missing", "--clear", "--json"], root)
        for proc in (remove, add, clear):
            self.assertEqual(proc.returncode, 0, detail(proc))
        self.assertEqual([last_json(p)["dependsOn"] for p in (remove, add, clear)], [[], ["pre"], []])

    def test_c20_owner_race_workpad_and_collaboration(self):
        root = project("owner-contract")
        (root / "board").mkdir()
        path = root / "board" / "board.json"
        doc = core.normalize_doc({"cards": [{"id": "shared", "num": 1, "title": "Shared task",
                                             "notes": "Existing notes\n\n## Acceptance criteria\nKeep this."}]})
        with core.board_lock(path):
            core.save(path, doc, by="tester")
        envs = {label: dict(ENV, WORKBOARD_ACTOR=label) for label in ("Ada", "Grace")}
        results = {}
        barrier = threading.Barrier(2)

        def claim(label):
            barrier.wait()
            results[label] = wb(["start", "1", "--json"], root, envs[label])

        threads = [threading.Thread(target=claim, args=(label,)) for label in envs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        current = core.load(path)
        card = current["cards"][0]
        winner = card["activeOwner"]
        loser = next(label for label in envs if label != winner)
        self.assertEqual(results[winner].returncode, 0, detail(results[winner]))
        self.assertNotEqual(results[loser].returncode, 0)
        self.assertTrue(card["claimedAt"])
        self.assertEqual(current["rev"], doc["rev"] + 1)
        error = last_json(results[loser])
        self.assertEqual((error["ok"], error["code"], error["status"], error["rev"]),
                         (False, "owned", 409, current["rev"]))

        before = path.read_bytes()
        for args in (["fly", "1", "task"], ["done", "1", "--writeup", "cannot steal"],
                     ["cancel", "1", "--reason", "cannot steal"],
                     ["rework", "1", "--reason", "cannot steal"], ["takeover", "1", "--reason", "   "]):
            self.assertNotEqual(wb(args, root, envs[loser]).returncode, 0, args)
        self.assertEqual(wb(["start", "1", "--json"], root, envs[winner]).returncode, 0)
        self.assertEqual(path.read_bytes(), before)

        first = wb(["workpad", "1", "--json"], root, envs[loser])
        workpad = path.read_bytes()
        second = wb(["workpad", "1", "--json"], root, envs[loser])
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(path.read_bytes(), workpad)
        notes = core.load(path)["cards"][0]["notes"]
        self.assertTrue(notes.startswith("Existing notes\n\n## Acceptance criteria\nKeep this."), notes)
        self.assertEqual((notes.count("## Acceptance criteria"), notes.count("## Verification")), (1, 1))
        self.assertEqual(core.load(path)["cards"][0]["activeOwner"], winner)

        added = wb(["comment", "1", "add", "Please verify the boundary", "--json"], root, envs[loser])
        comment = core.load(path)["cards"][0]["comments"][0]
        edited = wb(["comment", "1", "edit", comment["id"], "Boundary verified", "--json"],
                    root, envs[winner])
        changed = core.load(path)["cards"][0]["comments"][0]
        deleted = wb(["comment", "1", "delete", comment["id"], "--json"], root, envs[loser])
        for proc in (added, edited, deleted):
            self.assertEqual(proc.returncode, 0, detail(proc))
        self.assertEqual((changed["text"], changed["updatedBy"]), ("Boundary verified", winner))
        self.assertTrue(changed["updatedAt"])
        self.assertEqual(last_json(added)["item"], comment)
        self.assertEqual(last_json(edited)["item"], changed)
        self.assertEqual(last_json(deleted)["item"], changed)
        self.assertEqual({key: changed[key] for key in ("id", "at", "by")},
                         {key: comment[key] for key in ("id", "at", "by")})
        self.assertEqual(core.load(path)["cards"][0]["comments"], [])

        takeover = wb(["takeover", "1", "--reason", "handoff agreed", "--json"], root, envs[loser])
        stale_owner = wb(["done", "1", "--writeup", "old owner"], root, envs[winner])
        cancel = wb(["cancel", "1", "--reason", "scope removed", "--json"], root, envs[loser])
        rework = wb(["rework", "1", "--reason", "scope restored", "--json"], root, envs[winner])
        for proc in (takeover, cancel, rework):
            self.assertEqual(proc.returncode, 0, detail(proc))
        self.assertEqual(last_json(takeover)["activeOwner"], loser)
        self.assertNotEqual(stale_owner.returncode, 0)
        self.assertEqual(last_json(cancel)["outcome"], "canceled")
        self.assertIsNone(last_json(cancel)["activeOwner"])
        card = core.load(path)["cards"][0]
        self.assertEqual((card["column"], card["reworkReason"], card["cycles"][-1]["cancelReason"]),
                         ("task", "scope restored", "scope removed"))

    def test_c21_normalization_identity_and_attachment_recovery(self):
        path = project("durable-contract") / "board" / "board.json"
        path.parent.mkdir()
        doc = core.normalize_doc({
            "tagTaxonomy": {"main": [{"name": "core", "color": "#123456"}]},
            "activeWork": {"agent": "durable"}, "activeWorkId": "durable",
            "cards": [{"id": "durable", "num": 1, "column": "inprogress", "activeOwner": "Ada",
                       "claimedAt": "2020-01-01T00:00:00Z", "dependsOn": ["missing"],
                       "reworkReason": "review feedback", "linkedCards": ["related"],
                       "lifecycleCycles": [{"writeup": "historical evidence"}],
                       "subtasks": [{"id": "s1", "text": "nested", "collapsed": True,
                                     "children": [{"id": "s2", "text": "child", "done": True}]}],
                       "verification": [{"evidence": "observed"}], "reviews": [{"findings": "checked"}],
                       "meta": {"autoSource": "import"}, "lastTouchedSubtask": "s2",
                       "agentRuns": [{"status": "finished", "summary": "legacy fact"}]}],
        })
        card = doc["cards"][0]
        comment = core.comment_action(card, {"type": "add", "text": "Durable comment"}, "Grace")
        blob = b"\x00binary\r\nattachment\xff"
        metadata = core.attachment_store(path, "../original-name.bin", blob, "Grace")
        card["attachments"].append(metadata)
        with core.board_lock(path):
            core.save(path, doc, by="Ada")
        revision = doc["rev"]
        loaded = core.load(path)
        self.assertEqual(loaded, doc)
        self.assertEqual(loaded["cards"][0]["links"], ["related"])
        self.assertEqual(loaded["cards"][0]["cycles"], [{"writeup": "historical evidence"}])
        self.assertNotIn("linkedCards", loaded["cards"][0])
        self.assertNotIn("lifecycleCycles", loaded["cards"][0])
        self.assertEqual(loaded["cards"][0]["comments"][0]["id"], comment["id"])
        self.assertEqual(core.attachment_path(path, metadata["id"]).read_bytes(), blob)

        for operation in ({"type": "add", "text": " "}, {"type": "add", "text": "a" * 16001},
                          {"type": "edit", "id": comment["id"], "text": "forged", "by": "Ada"}):
            before = json.dumps(card, sort_keys=True)
            with self.assertRaises(core.WorkflowError) as caught:
                core.comment_action(card, operation, "Grace")
            self.assertEqual(caught.exception.status, 422)
            self.assertEqual(json.dumps(card, sort_keys=True), before)
        for attachment_id in ("../outside", "/absolute", "A" * 32):
            with self.assertRaises(core.WorkflowError):
                core.attachment_path(path, attachment_id)

        card["attachments"] = []
        with core.board_lock(path):
            core.save(path, doc, by="Ada")
        backup = next(p for rev, p in core.list_backups(path) if rev == revision)
        recovered = core.normalize_doc(json.loads(core.read_text_shared(backup)))
        self.assertEqual(core.load(path)["cards"][0]["attachments"], [])
        self.assertEqual(recovered["cards"][0]["attachments"][0], metadata)
        self.assertEqual(core.attachment_path(path, metadata["id"]).read_bytes(), blob)

        old_id = core.unique_id(doc, "same title")
        doc["cards"].append(core.normalize_card({"id": old_id, "num": core.new_num(doc)}))
        doc["cards"].pop()
        new_id = core.unique_id(doc, "same title")
        doc["cards"][0]["dependsOn"].append(old_id)
        self.assertNotEqual(old_id, new_id)
        self.assertTrue(old_id.startswith("same-title-"))
        with self.assertRaises(core.WorkflowError) as caught:
            core.validate_new_identity(doc, old_id)
        self.assertEqual(caught.exception.status, 409)

    def test_c22_agent_context_attachments_and_guards(self):
        root = BASE / "agent-interface"
        created = wb(["init", "agent", "--dir", root, "--json"], BASE)
        self.assertEqual(created.returncode, 0, detail(created))
        path = root / "board" / "board.json"
        created_path = Path(last_json(created)["board"])
        self.assertTrue(created_path.is_absolute())
        self.assertEqual(created_path.resolve(), path.resolve())
        identity = last_json(wb(["--json", "--board", root, "add", "--title", "Inspect every input"], BASE))
        reference = str(identity["num"])
        comment = wb(["comment", reference, "add", "User context " + "x" * 1000,
                      "--expected-rev", identity["rev"], "--board", root, "--json"], BASE)
        self.assertEqual(comment.returncode, 0, detail(comment))
        source = BASE / "source.bin"
        content = b"\x00\xffagent attachment\r\n"
        source.write_bytes(content)
        uploaded = wb(["attachment", reference, "add", "--file", source,
                       "--expected-rev", last_json(comment)["rev"], "--board", root, "--json"], BASE)
        self.assertEqual(uploaded.returncode, 0, detail(uploaded))
        upload = last_json(uploaded)
        metadata = upload["item"]
        self.assertEqual((upload["id"], upload["actor"]), (identity["id"], "tester"))
        rev = upload["rev"]

        selected = last_json(wb(["--json", "which", "--board", root], BASE))
        self.assertEqual(Path(selected["board"]).resolve(), path.resolve())
        self.assertEqual((selected["rev"], selected["cards"]), (rev, 1))
        registered = last_json(wb(["boards", "--json"], BASE))["boards"]
        self.assertTrue(any(item["name"] == "agent" and item["exists"] is True
                            and Path(item["board"]).resolve() == path.resolve()
                            and item["url"] == "http://127.0.0.1:7891/b/agent/"
                            for item in registered), registered)
        pulse = last_json(wb(["digest", "--board", root, "--json"], BASE))
        self.assertEqual((pulse["rev"], pulse["stats"]["total"], pulse["ready"]),
                         (rev, 1, [identity["num"]]))
        for command in (["list"], ["search", "Inspect"], ["search", "User", "context"]):
            result = last_json(wb([*command, "--json", "--board", root], BASE))
            self.assertEqual(result["rev"], rev)
            self.assertEqual([card["id"] for card in result["cards"]], [identity["id"]])

        lock = path.parent / ".board.lock"
        lock.unlink()
        context = last_json(wb(["--board", root, "--json", "context", reference], BASE))
        manifest = last_json(wb(["attachment", reference, "list", "--board", root, "--json"], BASE))
        self.assertFalse(lock.exists(), "read-only context/list created a board lock")
        self.assertEqual(context["rev"], rev)
        self.assertTrue(context["card"]["comments"][0]["text"].endswith("x" * 1000))
        self.assertEqual(context["card"]["attachments"], [metadata])
        self.assertEqual(manifest["attachments"], [metadata])
        self.assertTrue(context["ready"])
        self.assertEqual((context["dependencies"], context["missingDependencies"], context["dependents"]),
                         ([], [], []))
        self.assertEqual(context["card"]["changedRev"], rev)

        destination = root / "downloads" / "source.bin"
        exported = wb(["attachment", reference, "get", metadata["id"], "--out", destination,
                       "--board", root, "--json"], BASE)
        self.assertEqual(exported.returncode, 0, detail(exported))
        self.assertEqual(destination.read_bytes(), content)
        export = last_json(exported)
        self.assertEqual(export["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual((export["num"], export["id"]), (identity["num"], identity["id"]))
        refused = wb(["attachment", reference, "get", metadata["id"], "--out", destination,
                      "--board", root, "--json"], BASE)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(last_json(refused)["status"], 409)
        self.assertEqual(destination.read_bytes(), content)

        before = path.read_bytes()
        for operation in (
                ["start", reference], ["update", reference, "--title", "Stale"],
                ["fly", reference, "backlog"], ["note", reference, "--text", "Stale"],
                ["subtask", reference, "add", "Stale"], ["depends", reference, "--clear"],
                ["comment", reference, "add", "Stale"], ["wip", "off"],
                ["attachment", reference, "add", "--file", source],
                ["attachment", reference, "remove", metadata["id"]],
                ["columns-core", "--apply"], ["sweep", "--apply"], ["recover", "--apply"]):
            proc = wb([*operation, "--board", root, "--json", "--expected-rev", rev - 1], BASE)
            self.assertNotEqual(proc.returncode, 0, operation)
            error = last_json(proc)
            self.assertEqual((error["ok"], error["status"], error["code"], error["rev"]),
                             (False, 409, "stale", rev), (operation, proc.stdout, proc.stderr))
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list((path.parent / "attachments").iterdir()),
                         [path.parent / "attachments" / metadata["id"]])
        for operation in (["add", "--title", "Wrong", "--borad", str(root)],
                          ["add", "--title", "No guard on add", "--expected-rev", str(rev)],
                          ["context", reference, "--expected-rev", str(rev)],
                          ["attachment", reference, "list", "--expected-rev", str(rev)],
                          ["sweep", "--expected-rev", str(rev)],
                          ["init", "--dir", str(root / "wrong")],
                          ["--board", str(root), "start", reference]):
            proc = wb(["--board", root, *operation, "--json"], BASE)
            self.assertNotEqual(proc.returncode, 0, (operation, proc.stdout))
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse((root / "wrong").exists())

        removed = wb(["attachment", reference, "remove", metadata["id"], "--expected-rev", rev,
                      "--board", root, "--json"], BASE)
        self.assertEqual(removed.returncode, 0, detail(removed))
        detached = last_json(removed)
        self.assertEqual((detached["rev"], detached["item"], detached["id"], detached["actor"]),
                         (rev + 1, metadata, identity["id"], "tester"))
        self.assertEqual((path.parent / "attachments" / metadata["id"]).read_bytes(), content)
        absent = wb(["attachment", reference, "get", metadata["id"], "--out",
                     root / "downloads" / "absent", "--board", root, "--json"], BASE)
        self.assertNotEqual(absent.returncode, 0)
        error = last_json(absent)
        self.assertEqual((error["status"], error["code"], error["rev"]), (404, "not_found", rev + 1))
        self.assertFalse((HOME / ".workboard" / "server.json").exists())

    def test_c23_schema_threads_and_export_boundaries(self):
        path = project("schema-contract") / "board" / "board.json"
        path.parent.mkdir()
        raw = {"vendorDocument": {"keep": 1}, "columns": [
            {**column, "vendorColumn": ["keep"]} for column in core.DEFAULT_COLUMNS],
            "cards": [{"id": "subject", "num": 1, "title": "Subject", "vendorCard": {"keep": 2},
                       "dependsOn": ["done-dep", "lost-dep"],
                       "subtasks": [{"id": "step", "text": "Step", "vendorSubtask": {"keep": 3}}],
                       "linkedCards": ["peer"], "lifecycleCycles": [{"writeup": "old"}]},
                      {"id": "done-dep", "num": 2, "title": "Done dependency",
                       "column": "done", "outcome": "completed", "writeup": "Evidence"}]}
        subject = raw["cards"][0]
        subject["notes"] = "Resume notes " * 100
        subject["comments"] = [{"id": "discussion", "by": "Ada", "text": "Complete discussion " * 100}]
        subject["history"] = [{"ev": "note", "by": "Ada", "note": str(n)} for n in range(30)]
        subject["subtasks"].append({"id": "ancestor", "done": True, "doneAt": "2020-01-01T00:00:00Z",
                                    "children": [{"id": "open-child", "text": "Still needed",
                                                  "done": False}]})
        subject["subtasks"].extend({"id": f"done-{n}", "text": f"Finished {n}", "done": True,
                                    "doneAt": f"2020-02-{n + 1:02d}T00:00:00Z"} for n in range(12))
        doc = core.normalize_doc(raw)
        core.save(path, doc, by="Ada")
        actual = core.load(path)
        self.assertEqual(actual["vendorDocument"], raw["vendorDocument"])
        self.assertEqual(actual["columns"][0]["vendorColumn"], ["keep"])
        self.assertEqual(actual["cards"][0]["vendorCard"], {"keep": 2})
        self.assertEqual(actual["cards"][0]["subtasks"][0]["vendorSubtask"], {"keep": 3})
        context = core.card_context(path, "1")
        self.assertTrue(context["dependencies"][0]["satisfied"])
        self.assertEqual(context["missingDependencies"], ["lost-dep"])
        self.assertFalse(context["ready"])
        self.assertEqual(context["omitted"], {"doneSubtasks": 2, "history": 5})
        visible = {item["id"] for item, _ in core.iter_subtasks(context["card"]["subtasks"])}
        self.assertLessEqual({"step", "ancestor", "open-child", "done-11"}, visible)
        self.assertFalse({"done-0", "done-1"} & visible)
        self.assertEqual(context["card"]["notes"], subject["notes"])
        self.assertEqual(context["card"]["comments"], subject["comments"])
        self.assertEqual(context["card"]["history"], subject["history"][-25:])
        full = last_json(wb(["context", "1", "--full", "--json"], path.parent.parent))
        self.assertNotIn("omitted", full)
        self.assertEqual(full["card"], actual["cards"][0])
        self.assertEqual(core.card_context(path, "2")["dependents"], [{
            "num": 1, "id": "subject", "title": "Subject", "column": "task", "outcome": None}])

        healthy = path.read_bytes()
        lock = path.parent / core.LOCK_NAME
        for legacy in (raw, {**raw, "schemaVersion": 1}):
            path.write_text(json.dumps(legacy), encoding="utf-8")
            core.save(path, core.load(path), by="Ada")
            saved = json.loads(path.read_bytes())
            self.assertEqual(saved["schemaVersion"], 2)
            self.assertEqual(saved["vendorDocument"], raw["vendorDocument"])
            self.assertNotIn("linkedCards", saved["cards"][0])
            self.assertNotIn("lifecycleCycles", saved["cards"][0])
        for version in (True, False, 0, 3, "2", 2.0, None):
            path.write_text(json.dumps({**raw, "schemaVersion": version}), encoding="utf-8")
            before = path.read_bytes()
            lock.unlink(missing_ok=True)
            with self.assertRaises(core.WorkflowError, msg=f"unsupported schema written: {version!r}"):
                core.save(path, doc, by="Ada")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(lock.exists())
        for canonical, alias in (("links", "linkedCards"), ("cycles", "lifecycleCycles")):
            invalid = json.loads(healthy)
            invalid["cards"][0][alias] = []
            invalid["cards"][0][canonical] = ["conflict"] if canonical == "links" else [{"writeup": "conflict"}]
            path.write_text(json.dumps(invalid), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(core.WorkflowError):
                with core.board_transaction(path):
                    self.fail("ambiguous alias was accepted")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(lock.exists())
            invalid["cards"][0][alias] = invalid["cards"][0][canonical]
            self.assertNotIn(alias, core.normalize_doc(invalid)["cards"][0])
        with self.assertRaises(core.WorkflowError) as caught:
            core.normalize_card({"cycles": [{"extension": True}], "lifecycleCycles": [{"extension": 1}]})
        self.assertEqual(caught.exception.status, 409)

        path.write_bytes(healthy)
        entered, excluded, acquired = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def contender():
            entered.set()
            try:
                with core.board_lock(path, timeout=0.15):
                    errors.append("other thread inherited reentrancy")
            except core.LockTimeout:
                excluded.set()
            except Exception as exc:
                errors.append(str(exc))

        with core.board_lock(path):
            with core.board_lock(path.parent / ".." / "board" / "board.json", timeout=0.1):
                thread = threading.Thread(target=contender)
                thread.start()
                self.assertTrue(entered.wait(2))
                thread.join(3)
                self.assertTrue(excluded.is_set() and not thread.is_alive(), errors)
                self.assertEqual(errors, [])

        def after_release():
            with core.board_lock(path, timeout=1):
                acquired.set()

        thread = threading.Thread(target=after_release)
        thread.start()
        thread.join(3)
        self.assertTrue(acquired.is_set(), "thread did not acquire after owner released")

        doc, card, metadata = core.attachment_add(path, "1", "edge.bin", b"safe", "Ada")
        blob = core.attachment_path(path, metadata["id"])
        if os.name == "nt":
            runtime = BASE / "case-runtime"
            runtime.mkdir()
            with mock.patch.object(core, "__file__", str(runtime / "core.py")):
                for destination in (runtime / "__PYCACHE__" / "injected.bin",
                                    runtime / "WEB" / "injected.bin",
                                    BASE / "BOard" / "injected.bin"):
                    with self.assertRaises(core.WorkflowError) as caught:
                        core.attachment_export(path, "1", metadata["id"], destination)
                    self.assertEqual(caught.exception.status, 403)
                    self.assertFalse(destination.parent.exists(), destination)
        oversized = b"x" * (core.MAX_ATTACHMENT_BYTES + 2)
        blob.write_bytes(oversized)
        forged = {**metadata, "size": core.MAX_ATTACHMENT_BYTES,
                  "sha256": hashlib.sha256(oversized[:core.MAX_ATTACHMENT_BYTES]).hexdigest()}
        for manifest in (forged, {**forged, "size": core.MAX_ATTACHMENT_BYTES + 1},
                         {**forged, "size": -1}, {**forged, "size": True}):
            with self.assertRaises(core.WorkflowError) as caught:
                core.attachment_verified_bytes(path, manifest)
            self.assertEqual(caught.exception.status, 413)
        blob.write_bytes(b"safe")
        package = Path(core.__file__).resolve().parent
        for destination in (path.parent / "injected.json", core.home() / "injected",
                            package / "injected.py", package / "web" / "injected.html"):
            with self.assertRaises(core.WorkflowError) as caught:
                core.attachment_export(path, "1", metadata["id"], destination)
            self.assertEqual(caught.exception.status, 403, destination)
            self.assertFalse(destination.exists())

        destination = BASE / "export-race" / "result.bin"
        real_link = os.link

        def racing_link(source, target):
            Path(target).write_bytes(b"other writer")
            return real_link(source, target)

        with mock.patch.object(core.os, "link", side_effect=racing_link):
            with self.assertRaises(FileExistsError):
                core.attachment_export(path, "1", metadata["id"], destination)
        self.assertEqual(destination.read_bytes(), b"other writer")
        self.assertEqual(list(destination.parent.iterdir()), [destination])

        linked = BASE / "linked-blob.bin"
        os.link(blob, linked)
        try:
            with self.assertRaises(core.WorkflowError, msg="non-private hardlinked attachment was read"):
                core.attachment_read(path, "1", metadata["id"])
        finally:
            linked.unlink()
        blob.unlink()
        with self.assertRaises(core.WorkflowError) as caught:
            core.attachment_read(path, "1", metadata["id"])
        self.assertEqual(caught.exception.status, 404)

        blob.write_bytes(b"safe")
        before, blobs = path.read_bytes(), set(blob.parent.iterdir())
        with mock.patch.object(core, "save", side_effect=OSError("uncommitted save failure")):
            with self.assertRaises(OSError):
                core.attachment_add(path, "1", "uncommitted.bin", b"never saved", "Ada")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(blob.parent.iterdir()), blobs)
        real_save = core.save

        def committed_failure(*args, **kwargs):
            real_save(*args, **kwargs)
            raise OSError("post-commit finalization failed")

        with mock.patch.object(core, "save", side_effect=committed_failure):
            with self.assertRaises(OSError):
                core.attachment_add(path, "1", "committed.bin", b"keep recovery bytes", "Ada")
        saved = core.card_context(path, "1")["card"]["attachments"]
        committed = next(item for item in saved if item["name"] == "committed.bin")
        self.assertEqual(core.attachment_verified_bytes(path, committed), b"keep recovery bytes")

    def test_c24_scope_fences_and_strict_registry(self):
        scope = BASE / "scope"
        outside = BASE / "outside"
        board = outside / "board" / "board.json"
        board.parent.mkdir(parents=True)
        board.write_text(json.dumps({"schemaVersion": 2, "cards": [], "columns": core.DEFAULT_COLUMNS}),
                         encoding="utf-8")
        before = board.read_bytes()
        env = make_env(scope / "home", WORKBOARD_SCOPE_ROOT=str(scope),
                       WORKBOARD_DEFAULT_BOARD=str(outside))
        read = wb(["--json", "query"], BASE, env)
        self.assertEqual(read.returncode, 0, detail(read))
        self.assertFalse(scope.exists())
        self.assertFalse((board.parent / core.LOCK_NAME).exists())
        for command in (["add", "--title", "Escape"], ["init"], ["init", "--dir", str(outside / "new")],
                        ["attachment", "1", "get", "0" * 32, "--out", str(outside / "export.bin")]):
            result = wb([*command, "--json"], BASE, env)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn(last_json(result)["status"], (403, 422), (command, result.stdout, result.stderr))
            self.assertEqual(board.read_bytes(), before)
            self.assertFalse(scope.exists())
        self.assertFalse((outside / "new").exists())

        copied = scope / "copied"
        created = wb(["init", "Scoped copy", "--dir", copied, "--json"], BASE, env)
        self.assertEqual(created.returncode, 0, detail(created))
        added = wb(["add", "--title", "Explicit copied board", "--board", copied, "--json"], BASE, env)
        self.assertEqual(added.returncode, 0, detail(added))
        self.assertEqual(board.read_bytes(), before)
        source = outside / "input.bin"
        source.write_bytes(b"external input is read-only task data")
        uploaded = wb(["attachment", "1", "add", "--file", source, "--board", copied, "--json"], BASE, env)
        self.assertEqual(uploaded.returncode, 0, detail(uploaded))
        destination = scope / "downloads" / "input.bin"
        exported = wb(["attachment", "1", "get", last_json(uploaded)["item"]["id"],
                       "--out", destination, "--board", copied, "--json"], BASE, env)
        self.assertEqual(exported.returncode, 0, detail(exported))
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(board.read_bytes(), before)

        registry = BASE / "boards-broken.json"
        began = time.monotonic()
        self.assertEqual(core.registry_load(registry), {"boards": {}})
        self.assertFalse(registry.exists())
        self.assertLess(time.monotonic() - began, 2, "missing optional registry blocked first use")
        for broken in ("{", "[]", "null", '{"boards": []}'):
            registry.write_text(broken, encoding="utf-8")
            with self.assertRaises((OSError, ValueError), msg=f"corrupt registry treated as empty: {broken}"):
                core.registry_load(registry)
            self.assertEqual(registry.read_text(encoding="utf-8"), broken)
        encoded = json.dumps({"boards": {}}).encode()
        registry.write_bytes(encoded)
        self.assertEqual(core.registry_load(registry, max_bytes=len(encoded)), {"boards": {}})
        with self.assertRaises(ValueError):
            core.registry_load(registry, max_bytes=len(encoded) - 1)

    def test_c25_card_scoped_reviewed_revision(self):
        root = self.init("card guards", "card-scoped-guards")
        x = last_json(wb(["--actor", "Agent-X", "add", "--title", "X", "--json"], root))
        y = last_json(wb(["add", "--title", "Y", "--actor", "Agent-Y", "--json"], root))
        self.assertEqual((x["actor"], y["actor"]), ("Agent-X", "Agent-Y"))
        reviewed = last_json(wb(["context", x["id"], "--json"], root))
        changed_y = wb(["note", y["id"], "--text", "Y changed", "--actor", "Agent-Y", "--json"], root)
        self.assertEqual(changed_y.returncode, 0, detail(changed_y))
        claim = wb(["start", x["id"], "--expected-rev", reviewed["rev"], "--actor", "Agent-X", "--json"], root)
        self.assertEqual(claim.returncode, 0, detail(claim))
        claimed = last_json(claim)
        self.assertEqual(claimed["rev"], last_json(changed_y)["rev"] + 1)
        self.assertEqual(claimed["activeOwner"], "Agent-X")
        current = last_json(wb(["context", x["id"], "--json"], root))
        self.assertEqual(current["card"]["changedRev"], claimed["rev"])
        path = root / "board" / "board.json"
        before = path.read_bytes()
        stale = wb(["note", x["id"], "--text", "old decision", "--expected-rev", reviewed["rev"],
                    "--actor", "Agent-X", "--json"], root)
        error = last_json(stale)
        self.assertEqual((stale.returncode, error["status"], error["code"], error["rev"]),
                         (1, 409, "stale", claimed["rev"]))
        self.assertEqual(error["card"], {"num": x["num"], "id": x["id"], "changedRev": claimed["rev"],
                                         "last": current["card"]["history"][-1]})
        self.assertEqual(path.read_bytes(), before)
        projection = ["num", "id", "title", "column", "priority", "tags", "outcome", "owner",
                      "deps", "changedRev", "createdAt", "updatedAt", "doneAt", "origin"]
        mine = last_json(wb(["--actor", "Agent-X", "query", "--mine", "--fields", ",".join(projection),
                             "--json"], root))
        owned = last_json(wb(["query", "--owner", "Agent-X", "--fields", ",".join(projection),
                              "--json"], root))
        self.assertEqual(mine, owned)
        self.assertEqual(mine["rev"], claimed["rev"])
        self.assertEqual(mine["cards"], [{**{field: current["card"][field] for field in projection
                                             if field not in ("owner", "deps")},
                                          "owner": "Agent-X", "deps": []}])

    def test_c27_bulk_subtasks_are_atomic_and_idempotent(self):
        root = self.init("bulk", "bulk-subtasks")
        card = last_json(wb(["add", "--title", "Bulk", "--json"], root))
        added = wb(["subtask", "1", "add", "First", "Second", "Third", "--actor", "Ada",
                    "--expected-rev", card["rev"], "--json"], root)
        self.assertEqual(added.returncode, 0, detail(added))
        result = last_json(added)
        items = result["items"]
        ids = [item["id"] for item in items]
        self.assertEqual(result["rev"], card["rev"] + 1)
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual([item["text"] for item in items], ["First", "Second", "Third"])
        self.assertTrue(all(item["by"] == "Ada" and not item["done"] for item in items))
        done = wb(["subtask", "1", "done", *ids, "--actor", "Grace", "--json"], root)
        self.assertEqual(done.returncode, 0, detail(done))
        completed = last_json(done)
        self.assertEqual(completed["rev"], result["rev"] + 1)
        self.assertTrue(all(item["done"] and item["doneBy"] == "Grace" for item in completed["items"]))
        path = root / "board" / "board.json"
        before = path.read_bytes()
        repeat = wb(["subtask", "1", "done", *ids, "--actor", "Ada", "--json"], root)
        self.assertEqual(repeat.returncode, 0, detail(repeat))
        self.assertEqual(last_json(repeat)["rev"], completed["rev"])
        self.assertEqual(last_json(repeat)["items"], completed["items"])
        self.assertEqual(path.read_bytes(), before)
        missing = wb(["subtask", "1", "undone", ids[0], "absent", "--json"], root)
        self.assertEqual((missing.returncode, last_json(missing)["code"]), (1, "not_found"))
        self.assertEqual(path.read_bytes(), before)
        reopened = last_json(wb(["subtask", "1", "undone", *ids, "--json"], root))
        self.assertTrue(all(not item["done"] and item.get("doneBy") is None and item["doneAt"] is None
                            for item in reopened["items"]))
        before = path.read_bytes()
        again = wb(["subtask", "1", "undone", *ids, "--json"], root)
        self.assertEqual((again.returncode, last_json(again)["rev"]), (0, reopened["rev"]))
        self.assertEqual(path.read_bytes(), before)
        history = last_json(wb(["context", "1", "--full", "--json"], root))["card"]["history"]
        self.assertEqual([(entry["ev"], entry["by"]) for entry in history[-3:]],
                         [("subtask-add", "Ada"), ("subtask-done", "Grace"), ("subtask-undone", "tester")])
        self.assertTrue(all(entry["note"] == ", ".join(ids) for entry in history[-3:]))

    def test_c28_errors_have_stable_codes_and_revision(self):
        root = self.init("errors", "error-envelope")
        card = last_json(wb(["add", "--title", "Error target", "--json"], root))
        path = root / "board" / "board.json"
        before = path.read_bytes()
        cases = [
            (["subtask", "1", "done", "absent"], 404, "not_found"),
            (["fly", "1", "done"], 422, "state"),
            (["query", "--fields", "num,title,column,n"], 422, "invalid"),
            (["note", "1", "--text", "future", "--expected-rev", card["rev"] + 1], 422, "invalid"),
            (["attachment", "1", "add", "--file", root / "absent.txt"], 404, "io"),
        ]
        for command, status, code in cases:
            proc = wb([*command, "--json"], root)
            error = last_json(proc)
            self.assertEqual((proc.returncode, error["ok"], error["status"], error["code"], error["rev"]),
                             (1, False, status, code, card["rev"]), command)
            self.assertTrue(error["error"] and not error["error"].startswith("error:"), error)
            self.assertEqual(path.read_bytes(), before)
        human = wb(["subtask", "1", "done", "absent"], root)
        self.assertEqual(human.returncode, 1)
        self.assertTrue(human.stderr.startswith("error [not_found]: "), human.stderr)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
