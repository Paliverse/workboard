# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""JSON stdin calls and read-only card/subtask waits, including binary contracts."""
from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from tests.support import command, last_json, make_env, run, scratch


class CallAndWait(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = self.stack.enter_context(scratch("wb-call-"))
        self.home = self.base / "home"
        self.home.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        self.env = make_env(self.home, WORKBOARD_ACTOR="Main")
        self.board = last_json(self.cli(["init", "calls", "--json"]))["board"]
        self.card = self.calls({"op": "add", "title": "Parent"})[0]

    def cli(self, args, **kwargs):
        return run(args, cwd=self.project, env=self.env, **kwargs)

    def calls(self, *requests, ok=True, array=False, pretty=False, defaults=()):
        text = (json.dumps(requests, indent=2 if pretty else None) if array else
                "\n".join(json.dumps(request, indent=2 if pretty else None) for request in requests))
        proc = self.cli([*defaults, "call"], input=text)
        self.assertEqual(proc.returncode, 0 if ok else 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), len(requests), proc.stdout)
        return [json.loads(line) for line in lines]

    def json(self, args):
        proc = self.cli([*args, "--json"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return last_json(proc)

    def test_order_errors_and_card_scoped_stale(self):
        results = self.calls(
            {"op": "show", "card": 1},
            {"op": "start", "card": 404},
            {"op": "note", "card": 1, "summary": "new finding"},
            {"op": "start", "card": 1, "expectedRev": self.card["rev"]},
            {"op": "show", "card": 1}, ok=False)
        self.assertEqual([r["ok"] for r in results], [True, False, True, False, True])
        self.assertEqual(results[1]["code"], "not_found")
        self.assertEqual((results[3]["code"], results[3]["status"]), ("stale", 409))
        self.assertEqual(results[3]["card"]["changedRev"], results[2]["rev"])
        self.assertEqual(results[4]["card"]["log"][-1]["summary"], "new finding")
        self.assertEqual(results[4]["card"]["column"], "task")

    def test_text_options_and_free_positionals_are_lossless(self):
        text = '-heading "quoted" \'single\' 100% ^ & café\nsecond line\nthird line'
        note, comment, subtasks, shown, hits = self.calls(
            {"op": "note", "card": 1, "summary": "Special text", "body": text},
            {"op": "comment.add", "card": 1, "text": text},
            {"op": "subtask.add", "card": 1, "texts": [text, "-another"]},
            {"op": "show", "card": 1, "full": True},
            {"op": "search", "terms": ["-heading", "100%"]})
        self.assertEqual(note["item"]["body"], text)
        self.assertEqual(comment["item"]["text"], text)
        self.assertEqual(subtasks["items"][0]["text"], text)
        self.assertEqual(shown["card"]["log"][-1]["body"], text)
        self.assertEqual(shown["card"]["comments"][-1]["text"], text)
        self.assertEqual(shown["card"]["subtasks"][0]["text"], text)
        self.assertEqual([c["num"] for c in hits["cards"]], [1])
        edited = self.calls({"op": "comment.edit", "card": 1,
                             "id": comment["item"]["id"], "text": "-replacement\n% ^ &"})[0]
        self.assertEqual(edited["item"]["text"], "-replacement\n% ^ &")
        self.calls({"op": "start", "card": 1},
                   {"op": "done", "card": 1, "writeup": text})
        improved = self.calls({"op": "improve", "card": 1, "text": text})[0]
        self.assertEqual(improved["item"]["text"], text)
        if sys.version_info >= (3, 12):  # 3.11's argparse drops a literal "--" after "--" (fixed in 3.12).
            literal_comment, literal_subtask = self.calls(
                {"op": "comment.add", "card": 1, "text": "--"},
                {"op": "subtask.add", "card": 1, "text": "--"})
            self.assertEqual(literal_comment["item"]["text"], "--")
            self.assertEqual(literal_subtask["item"]["text"], "--")

    def test_arrays_pretty_stream_and_boolean_list_options(self):
        started, added = self.calls({"op": "start", "card": 1},
                                   {"op": "subtask.add", "card": 1, "text": "Worker", "delegated": True},
                                   array=True, pretty=True)
        sid = added["item"]["id"]
        required, optional, note1, note2 = self.calls(
            {"op": "subtask.configure", "card": 1, "subtask": sid, "reviewRequired": True,
             "scope": ["src/a.py", "src/b.py"], "expectedRev": added["rev"]},
            {"op": "subtask.configure", "card": 1, "subtask": sid, "review-required": False,
             "clearScope": False, "scope": ["src/c.py", "tests"], "reason": None},
            {"op": "note", "card": 1, "actor": "Peer", "summary": "one", "body": "first"},
            {"op": "note", "card": 1, "actor": "Peer", "summary": "two", "body": "second"}, pretty=True)
        self.assertTrue(required["item"]["delegation"]["review"]["required"])
        self.assertFalse(optional["item"]["delegation"]["review"]["required"])
        self.assertEqual(optional["item"]["delegation"]["scope"], ["src/c.py", "tests"])
        ack, inbox = self.calls(
            {"op": "ack", "card": 1, "notes": [note1["item"]["id"], note2["item"]["id"]]},
            {"op": "inbox", "card": 1})
        self.assertEqual({item["id"] for item in ack["items"]},
                         {note1["item"]["id"], note2["item"]["id"]})
        self.assertFalse(any(item.get("kind") == "finding" for item in inbox["items"]))

    def test_defaults_and_request_actor_board_overrides(self):
        other = self.base / "other"
        other.mkdir()
        created = run(["init", "other", "--json"], cwd=other, env=self.env)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        added, overridden, defaulted = self.calls(
            {"op": "add", "board": "other", "actor": "Worker", "title": "Elsewhere"},
            {"op": "note", "card": 1, "board": "calls", "actor": "Peer", "summary": "override"},
            {"op": "note", "card": 1, "summary": "default"}, defaults=["--actor=Leader", "--board=calls"])
        self.assertEqual([r["actor"] for r in (added, overridden, defaulted)], ["Worker", "Peer", "Leader"])
        here = self.calls({"op": "show", "card": 1})[0]["card"]
        self.assertEqual([n["by"] for n in here["log"]], ["Peer", "Leader"])
        there = self.calls({"op": "show", "card": 1, "board": "other"})[0]["card"]
        self.assertEqual(there["title"], "Elsewhere")

    def test_invalid_request_and_argparse_envelopes(self):
        requests = [{"op": op} for op in (
            "init", "link", "serve", "open", "setup", "skills", "service", "upgrade", "version",
            "doctor", "recover", "sweep", "columns-core", "wip", "export", "import", "call", "list")]
        requests += [
            {"op": "done", "card": 1, "writeupStdin": True},
            {"op": "note", "card": 1, "stdin": False},
            {"op": "note", "card": 1, "body-stdin": True},
            {"op": "note", "card": 1},
            {"op": "update", "card": 1, "priority": "wrong"},
            {"op": "note", "card": 1, "summary": "bad", "body": {"text": "not scalar"}},
            {"op": "start"}, "not an object", {"card": 1},
            {"op": "show", "card": 1, "notAnOption": True},
        ]
        results = self.calls(*requests, ok=False)
        for result in results:
            self.assertEqual((result["ok"], result["status"], result["code"], result["rev"]),
                             (False, 422, "invalid", None), result)
            self.assertNotIn("usage:", result["error"])
            self.assertTrue(result["error"])
        for request, result in zip(requests[:18], results[:18]):
            self.assertEqual(result["error"], f"op {request['op']} is not available in call")
        self.assertIn("text goes in the request", results[18]["error"])
        self.assertIn("--summary", results[21]["error"])

    def test_empty_and_malformed_input(self):
        for text in ("", " \n\t", '{"op":', '{"op":"show"}\nnot JSON'):
            proc = self.cli(["call"], input=text)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            result = last_json(proc)
            self.assertEqual((result["code"], result["status"], result["rev"]), ("invalid", 422, None))

    def test_stream_flushes_before_stdin_closes(self):
        proc = subprocess.Popen([*command(), "call"], cwd=self.project, env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")
        try:
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: lines.put(proc.stdout.readline()))
            reader.start()
            proc.stdin.write(json.dumps({"op": "show", "card": 1}) + "\n")
            proc.stdin.flush()
            result = json.loads(lines.get(timeout=10))
            reader.join(timeout=1)
            self.assertTrue(result["ok"])
            self.assertIsNone(proc.poll(), "call must remain available for the next request")
            proc.stdin.write(json.dumps({"op": "note", "card": 1, "summary": "second request"}) + "\n")
            proc.stdin.close()
            self.assertEqual(json.loads(proc.stdout.readline())["item"]["summary"], "second request")
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

    def test_every_allowed_operation_has_one_json_object(self):
        self.calls(*[{"op": op, **({"card": 1} if op in ("context", "show", "inbox") else {})}
                     for op in ("digest", "next", "context", "show", "query", "inbox", "boards", "which")])
        self.calls({"op": "fly", "card": 1, "column": "backlog"},
                   {"op": "fly", "card": 1, "column": "task"},
                   {"op": "add", "title": "Dependency"},
                   {"op": "depends", "card": 1, "on": [2]},
                   {"op": "depends", "card": 1, "clear": True},
                   {"op": "update", "card": 1, "addTag": ["a", "b"]},
                   {"op": "start", "card": 1}, {"op": "workpad", "card": 1})
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["Worker", "Legacy"]})[0]
        sid, legacy = [item["id"] for item in added["items"]]
        self.calls({"op": "subtask.delegate", "card": 1, "subtask": sid},
                   {"op": "subtask.configure", "card": 1, "subtask": sid, "scope": ["src"],
                    "reviewRequired": True},
                   {"op": "handoff", "card": 1, "subtask": sid, "worker": "Worker"},
                   {"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Worker"},
                   {"op": "subtask.release", "card": 1, "subtask": sid, "actor": "Worker", "reason": "pause"},
                   {"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Worker"},
                   {"op": "subtask.block", "card": 1, "subtask": sid, "actor": "Worker",
                    "reason": "waiting", "until": "ready"},
                   {"op": "subtask.resume", "card": 1, "subtask": sid, "actor": "Worker"},
                   {"op": "subtask.takeover", "card": 1, "subtask": sid, "actor": "Peer", "reason": "asked"},
                   {"op": "subtask.done", "card": 1, "subtask": sid, "actor": "Peer", "result": "evidence"},
                   {"op": "subtask.undone", "card": 1, "subtask": sid, "actor": "Peer"},
                   {"op": "subtask.done", "card": 1, "subtask": sid, "actor": "Peer", "result": "evidence"},
                   {"op": "subtask.request-changes", "card": 1, "subtask": sid, "reason": "one gap"},
                   {"op": "subtask.done", "card": 1, "subtask": sid, "actor": "Peer", "result": "verified"},
                   {"op": "subtask.accept", "card": 1, "subtask": sid},
                   {"op": "subtask.done", "card": 1, "subtasks": [legacy]},
                   {"op": "subtask.undone", "card": 1, "subtasks": [legacy]},
                   {"op": "subtask.rm", "card": 1, "subtasks": [legacy]},
                   {"op": "block", "card": 1, "reason": "pause", "until": "ready"},
                   {"op": "resume", "card": 1, "note": "ready"},
                   {"op": "takeover", "card": 1, "actor": "Peer", "reason": "asked"},
                   {"op": "done", "card": 1, "actor": "Peer", "writeup": "verified"},
                   {"op": "bug", "card": 1, "reason": "regression"},
                   {"op": "done", "card": 1, "writeup": "fixed"},
                   {"op": "improve", "card": 1, "text": "-new improvement"},
                   {"op": "done", "card": 1, "writeup": "improved"},
                   {"op": "reopen", "card": 1, "reason": "more work"},
                   {"op": "cancel", "card": 1, "reason": "not needed"},
                   {"op": "rework", "card": 1, "reason": "needed again"})
        comment = self.calls({"op": "comment.add", "card": 1, "text": "discussion"})[0]["item"]
        self.calls({"op": "comment.edit", "card": 1, "id": comment["id"], "text": "updated"},
                   {"op": "comment.delete", "card": 1, "id": comment["id"]},
                   {"op": "search", "terms": ["Parent"]},
                   {"op": "wait", "card": 1, "after": 0, "timeout": 1})
        file = self.base / "evidence.txt"
        file.write_text("evidence", encoding="utf-8")
        attachment = self.calls({"op": "attachment.add", "card": 1, "file": str(file)})[0]["item"]
        out = self.base / "exported.txt"
        listed, exported, removed = self.calls(
            {"op": "attachment.list", "card": 1},
            {"op": "attachment.get", "card": 1, "id": attachment["id"], "out": str(out)},
            {"op": "attachment.remove", "card": 1, "id": attachment["id"]})
        self.assertEqual(listed["attachments"][0]["id"], attachment["id"])
        self.assertEqual(out.read_text(encoding="utf-8"), "evidence")
        self.assertEqual(removed["item"]["id"], attachment["id"])

    def wait_with_writer(self, wait_args, write, expected=0):
        results = queue.Queue()

        def writer():
            try:
                time.sleep(0.6)
                write()
            except BaseException as exc:
                results.put(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = self.cli(["wait", "1", *wait_args, "--json"], timeout=10)
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        if not results.empty():
            raise results.get()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return last_json(result)

    def test_wait_observes_other_process_card_write(self):
        result = self.wait_with_writer(["--after", self.card["rev"], "--timeout", 3],
                                      lambda: self.calls({"op": "note", "card": 1, "summary": "writer"}))
        self.assertTrue(result["changed"])
        self.assertEqual(result["last"]["ev"], "note")
        self.assertEqual(result["last"]["by"], "Main")
        self.assertEqual(result["rev"], result["changedRev"])
        self.assertGreater(result["waited"], 0)
        self.assertNotIn("subtask", result)
        self.assertEqual(set(result), {"ok", "changed", "num", "id", "rev", "changedRev", "last", "waited"})

    def test_wait_times_out_without_a_write(self):
        result = self.json(["wait", "1", "--timeout", 1])
        self.assertFalse(result["changed"])
        self.assertEqual(result["rev"], self.card["rev"])
        self.assertGreaterEqual(result["waited"], 1)
        self.assertLess(result["waited"], 2)
        human = self.cli(["wait", "1", "--timeout", 1])
        self.assertEqual(human.returncode, 0, human.stdout + human.stderr)
        self.assertRegex(human.stdout, r"#1 unchanged after [0-9.]+s")

    def test_wait_disappearing_board_keeps_the_normal_error_envelope(self):
        result = self.wait_with_writer(["--timeout", 2], lambda: Path(self.board).unlink(), expected=1)
        self.assertEqual((result["ok"], result["status"], result["code"], result["rev"]),
                         (False, 404, "io", None))

    def test_wait_subtask_ignores_siblings_and_observes_own_subtree(self):
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["Watched", "Sibling"]})[0]
        watched, sibling = [item["id"] for item in added["items"]]
        sibling_only = self.wait_with_writer(
            ["--subtask", watched, "--after", added["rev"], "--timeout", 2],
            lambda: self.calls({"op": "subtask.done", "card": 1, "subtask": sibling}))
        self.assertFalse(sibling_only["changed"])
        self.assertFalse(sibling_only["subtask"]["done"])
        baseline = sibling_only["rev"]
        own = self.wait_with_writer(
            ["--subtask", watched, "--after", baseline, "--timeout", 3],
            lambda: self.calls({"op": "subtask.done", "card": 1, "subtask": watched}))
        self.assertTrue(own["changed"])
        self.assertEqual(own["subtask"], {"id": watched, "done": True, "delegation": None})
        descendant = self.wait_with_writer(
            ["--subtask", watched, "--after", own["rev"], "--timeout", 3],
            lambda: self.calls({"op": "subtask.add", "card": 1, "parent": watched, "text": "Child"}))
        self.assertTrue(descendant["changed"])

    def test_wait_immediate_after_bounds_and_missing_subtask(self):
        note, added = self.calls({"op": "note", "card": 1, "summary": "already changed"},
                                {"op": "subtask.add", "card": 1, "text": "Watch"})
        sid = added["item"]["id"]
        for extra in ([], ["--subtask", sid]):
            result = self.json(["wait", "1", "--after", self.card["rev"], "--timeout", 1, *extra])
            self.assertTrue(result["changed"])
            self.assertLess(result["waited"], 0.5)
        bounds = self.calls(*[{"op": "wait", "card": 1, "timeout": value}
                              for value in (0, 0.5, 3601, -1)], ok=False)
        for result in bounds:
            self.assertEqual((result["status"], result["code"]), (422, "invalid"))
        maximum = self.calls({"op": "wait", "card": 1, "after": 0, "timeout": 3600})[0]
        self.assertTrue(maximum["changed"])
        missing = self.calls({"op": "wait", "card": 1, "subtask": "missing", "timeout": 1}, ok=False)[0]
        self.assertEqual((missing["status"], missing["code"]), (404, "not_found"))
        human = self.cli(["wait", "1", "--after", 0, "--timeout", 1])
        self.assertEqual(human.returncode, 0, human.stdout + human.stderr)
        self.assertIn("(last: subtask-add by Main)", human.stdout)


if __name__ == "__main__":
    unittest.main()
