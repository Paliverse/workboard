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
        configured, rescoped, note1, note2 = self.calls(
            {"op": "subtask.configure", "card": 1, "subtask": sid,
             "scope": ["src/a.py", "src/b.py"], "expectedRev": added["rev"]},
            {"op": "subtask.configure", "card": 1, "subtask": sid,
             "clearScope": False, "scope": ["src/c.py", "tests"], "reason": None},
            {"op": "note", "card": 1, "actor": "Peer", "summary": "one", "body": "first"},
            {"op": "note", "card": 1, "actor": "Peer", "summary": "two", "body": "second"}, pretty=True)
        self.assertEqual(configured["item"]["delegation"]["scope"], ["src/a.py", "src/b.py"])
        self.assertEqual(rescoped["item"]["delegation"]["scope"], ["src/c.py", "tests"])
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
        self.assertIn('"summary"', results[21]["error"])

    def test_single_value_lists_empty_lists_and_missing_keys_name_request_keys(self):
        self.calls({"op": "start", "card": 1})
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["One", "Two"]})[0]
        one, two = [item["id"] for item in added["items"]]
        before = Path(self.board).read_bytes()
        cases = [
            ({"op": "update", "card": 1, "notes": ["a", "b"]}, "notes"),
            ({"op": "note", "card": 1, "summary": "s", "subtask": [one, two]}, "subtask"),
            ({"op": "subtask.done", "card": 1, "subtask": [one, two]}, "subtasks"),
            ({"op": "show", "card": [1, 2]}, "card"),
            ({"op": "query", "fields": ["num", "title"]}, "fields"),
            ({"op": "subtask.configure", "card": 1, "subtask": one, "scope": []}, "clearScope"),
            ({"op": "subtask.configure", "card": 1, "subtask": one, "on": []}, "clearDeps"),
            ({"op": "update", "card": 1, "addTag": []}, "addTag"),
            ({"op": "subtask.claim", "card": 1}, "subtask"),
            ({"op": "subtask.add", "card": 1}, "text"),
            ({"op": "start"}, "card"),
            ({"op": "ack", "card": 1}, "notes"),
            ({"op": "ack", "card": 1, "notes": []}, "notes"),
            ({"op": "comment.add", "card": 1}, "text"),
            ({"op": "block", "card": 1, "until": "later"}, "reason"),
            ({"op": "note", "card": 1}, "summary"),
            ({"op": "context", "card": 1, "id": one}, "subtask"),
            ({"op": "wait", "card": 1, "subtasks": [one]}, "subtask"),
            ({"op": "context", "card": "-h"}, "card"),
            ({"op": "attachment.get", "card": 1, "id": "-h", "out": "copy.txt"}, "id"),
            ({"op": "subtask.configure", "card": 1, "subtask": one, "reviewRequired": True}, "reviewRequired"),
            ({"op": "subtask.add", "card": 1, "text": "Child", "parent": one}, "parent"),
            ({"op": "subtask.edit", "card": 1, "subtask": "s-1", "text": "x"}, "subtask.edit"),
            ({"op": "comment.5", "card": 1, "text": "x"}, "comment.5"),
        ]
        *results, shown = self.calls(*[request for request, _ in cases], {"op": "show", "card": 1}, ok=False)
        for (request, key), result in zip(cases, results):
            self.assertEqual((result["ok"], result["status"], result["code"], result["rev"]),
                             (False, 422, "invalid", None), (request, result))
            self.assertIn(key, result["error"], request)
            self.assertNotIn("--", result["error"], request)
        self.assertTrue(shown["ok"], "a value starting with '-' must not end the stream")
        done = self.calls({"op": "done", "card": 1}, ok=False)[0]
        self.assertEqual((done["status"], done["code"]), (422, "invalid"))
        self.assertIn('"writeup"', done["error"])
        self.assertEqual(Path(self.board).read_bytes(), before)

    def test_query_and_list_columns_take_aliases_and_refuse_unknown_names(self):
        self.calls({"op": "start", "card": 1}, {"op": "add", "title": "Still waiting"})
        for column in ("in-progress", "In Progress", "#inprogress"):
            queried = self.calls({"op": "query", "column": column})[0]
            self.assertEqual([card["num"] for card in queried["cards"]], [1], column)
            listed = self.json(["list", "--column", column])
            self.assertEqual([card["num"] for card in listed["cards"]], [1], column)
        for command in ("query", "list"):
            proc = self.cli([command, "--column", "doing", "--json"])
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual((last_json(proc)["status"], last_json(proc)["code"]), (422, "invalid"))

    def test_query_and_list_match_a_boards_own_legacy_column(self):
        path = Path(self.board)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["columns"].append({"id": "review", "name": "Review"})
        doc["cards"][0]["column"] = "review"
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual([card["num"] for card in self.calls({"op": "query", "column": "review"})[0]["cards"]], [1])
        self.assertEqual([card["num"] for card in self.json(["list", "--column", "review"])["cards"]], [1])

    def test_add_into_in_progress_runs_start_checks(self):
        started = self.calls({"op": "add", "title": "Started", "column": "inprogress"})[0]
        self.assertEqual((started["column"], started["activeOwner"]), ("inprogress", "Main"))
        before = Path(self.board).read_bytes()
        waiting, shipped = self.calls({"op": "add", "title": "Waiting", "column": "in-progress", "on": 1},
                                      {"op": "add", "title": "Shipped", "column": "done"}, ok=False)
        self.assertEqual((waiting["status"], waiting["code"]), (409, "deps"))
        self.assertEqual((shipped["status"], shipped["code"]), (409, "state"))
        self.assertEqual(Path(self.board).read_bytes(), before)

    def test_add_pins_notes_from_the_request_or_stdin(self):
        added = self.calls({"op": "add", "title": "Pinned", "notes": "Acceptance: one\nAcceptance: two"})[0]
        piped = self.cli(["add", "--title", "Piped", "--notes-stdin", "--json"], input="Acceptance: piped\n")
        self.assertEqual(piped.returncode, 0, piped.stdout + piped.stderr)
        request, stdin = self.calls({"op": "show", "card": added["num"], "full": True},
                                    {"op": "show", "card": last_json(piped)["num"], "full": True})
        self.assertEqual(request["card"]["notes"], "Acceptance: one\nAcceptance: two")
        self.assertEqual(stdin["card"]["notes"], "Acceptance: piped")
        both = self.cli(["add", "--title", "Both", "--origin-stdin", "--notes-stdin"], input="text")
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)

    def test_search_projects_fields_and_keeps_the_newest_hits(self):
        self.calls(*[{"op": "add", "title": f"Needle {n}"} for n in range(3)])
        default, limited, unknown, zero = self.calls(
            {"op": "search", "terms": ["needle"]},
            {"op": "search", "terms": ["needle"], "limit": 2, "fields": "num,id,owner"},
            {"op": "search", "terms": ["needle"], "fields": "num,nope"},
            {"op": "search", "terms": ["needle"], "limit": 0}, ok=False)
        self.assertEqual(default["cards"], [{"num": n + 2, "title": f"Needle {n}", "column": "task"}
                                            for n in range(3)])
        self.assertEqual([card["num"] for card in limited["cards"]], [3, 4])
        self.assertEqual(set(limited["cards"][0]), {"num", "id", "owner"})
        for result in (unknown, zero):
            self.assertEqual((result["status"], result["code"]), (422, "invalid"))

    def test_handoff_is_slim_takes_over_and_runs_through_call(self):
        added = self.calls({"op": "start", "card": 1},
                           {"op": "subtask.add", "card": 1, "text": "Review", "delegated": True})[1]
        sid = added["item"]["id"]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Ada"})
        request = {"op": "handoff", "card": 1, "subtask": sid, "worker": "Grace"}
        handoff = self.calls(request)[0]
        self.assertEqual(set(handoff), {"ok", "rev", "num", "worker", "previousOwner", "writeScope",
                                        "scopeWarnings", "requests", "prompt"})
        self.assertEqual((handoff["previousOwner"], handoff["writeScope"]), ("Ada", []))
        self.assertEqual(handoff["requests"]["claim"], {"op": "subtask.takeover", "card": 1, "subtask": sid,
                                                        "reason": "{reason}", "board": "calls", "actor": "Grace"})
        for text in ("read-only: edit no files", "{reason}", "`workboard call`"):
            self.assertIn(text, handoff["prompt"])

        def printed(handoff):  # Each request is its own prompt line, in order, as call takes it.
            lines = [line for line in handoff["prompt"].splitlines() if line.startswith("{")]
            self.assertEqual(lines, [json.dumps(r, ensure_ascii=False) for r in handoff["requests"].values()])
            return dict(zip(handoff["requests"], lines))

        steps = printed(handoff)
        text = self.cli(["handoff", "1", "--subtask", sid, "--worker", "Grace"])
        self.assertEqual(text.stdout, handoff["prompt"])
        for option, value in (("--write-scope", "src"), ("--fields", "prompt"), ("--peer", "Ada=agent://x")):
            removed = self.cli(["handoff", "1", "--subtask", sid, "--worker", "Grace", option, value])
            self.assertEqual(removed.returncode, 2, option)
        worker = ["call"]  # The requests name their board and actor; no shell-quoted names on the command line.
        pasted = (steps["read"], steps["claim"].replace("{reason}", "Main reassigned the review"),
                  steps["publish"].replace("{summary}", "Checked").replace("{body}", "Evidence"))
        taken = self.cli(worker, input="\n".join(pasted) + "\n")
        self.assertEqual(taken.returncode, 0, taken.stdout + taken.stderr)
        self.assertEqual(json.loads(taken.stdout.splitlines()[1])["item"]["delegation"]["owner"], "Grace")
        again = self.calls(request)[0]
        self.assertIsNone(again["previousOwner"])
        self.assertEqual(again["requests"]["claim"],
                         {"op": "subtask.claim", "card": 1, "subtask": sid, "board": "calls", "actor": "Grace"})
        self.assertNotIn("{reason}", again["prompt"])
        self.assertLess(len(again["prompt"]), 1500)
        complete = printed(again)["complete"].replace("{result}", "Verified\\nby the review")
        completed = self.cli(worker, input=complete + "\n")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(last_json(completed)["item"]["delegation"]["result"], "Verified\nby the review")

    def test_handoff_of_blocked_work_adds_resume_after_the_takeover(self):
        sid = self.calls({"op": "start", "card": 1},
                         {"op": "subtask.add", "card": 1, "text": "Fix", "delegated": True})[1]["item"]["id"]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Ada"},
                   {"op": "subtask.block", "card": 1, "subtask": sid, "reason": "waiting", "until": "CI is green",
                    "actor": "Ada"})
        handoff = self.calls({"op": "handoff", "card": 1, "subtask": sid, "worker": "Grace"})[0]
        steps = handoff["requests"]
        self.assertEqual(list(steps), ["read", "claim", "resume", "publish", "complete"])
        self.assertIn("CI is green", handoff["prompt"])
        flow = [{**steps["claim"], "reason": "Ada is gone"}, steps["resume"], {**steps["complete"], "result": "Fixed"}]
        done = self.cli(["--board=calls", "--actor=Grace", "call"], input="\n".join(map(json.dumps, flow)) + "\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout.splitlines()[-1])["item"]["delegation"]["state"], "completed")

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
                   {"op": "subtask.configure", "card": 1, "subtask": sid, "scope": ["src"]},
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

    def test_wait_subtask_ignores_siblings_and_observes_itself(self):
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["Watched", "Sibling"]})[0]
        watched, sibling = [item["id"] for item in added["items"]]
        sibling_only = self.wait_with_writer(
            ["--subtask", watched, "--after", added["rev"], "--timeout", 2],
            lambda: self.calls({"op": "subtask.done", "card": 1, "subtask": sibling}))
        self.assertFalse(sibling_only["changed"])
        self.assertFalse(sibling_only["subtask"]["done"])
        self.assertEqual(sibling_only["notes"], [])
        baseline = sibling_only["rev"]
        own = self.wait_with_writer(
            ["--subtask", watched, "--after", baseline, "--timeout", 3],
            lambda: self.calls({"op": "subtask.done", "card": 1, "subtask": watched}))
        self.assertTrue(own["changed"])
        self.assertEqual(own["subtask"], {"id": watched, "done": True, "delegation": None})

    def test_wait_subtask_returns_the_new_notes_on_that_subtask(self):
        added = self.calls({"op": "start", "card": 1}, {"op": "subtask.add", "card": 1, "text": "Asked"})[1]
        sid = added["item"]["id"]
        self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Worker's question"})
        answered = self.wait_with_writer(
            ["--subtask", sid, "--timeout", 3],
            lambda: self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Main's answer"}))
        self.assertTrue(answered["changed"])
        self.assertEqual(answered["subtask"]["id"], sid)
        self.assertEqual([(note["summary"], note["by"]) for note in answered["notes"]], [("Main's answer", "Main")])

    def test_wait_subtask_catching_up_returns_notes_posted_after_your_own(self):
        sid = self.calls({"op": "start", "card": 1},
                         {"op": "subtask.add", "card": 1, "text": "Asked", "delegated": True})[1]["item"]["id"]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Ada"})
        asked = self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Worker's question", "actor": "Ada"})[0]
        self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Main's answer"})  # Before Ada waits.
        caught = self.calls({"op": "wait", "card": 1, "subtask": sid, "after": asked["rev"], "timeout": 1,
                             "actor": "Ada"})[0]
        self.assertTrue(caught["changed"])
        self.assertEqual([note["summary"] for note in caught["notes"]], ["Main's answer"])

    def test_wait_subtask_ignores_notes_on_other_subtasks(self):
        added = self.calls({"op": "start", "card": 1},
                           {"op": "subtask.add", "card": 1, "texts": ["Watched", "Other"], "delegated": True})[1]
        watched, other = [item["id"] for item in added["items"]]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": watched, "actor": "Ada"})
        done = self.calls({"op": "subtask.done", "card": 1, "subtask": watched, "result": "Verified",
                           "actor": "Ada"})[0]
        self.calls({"op": "note", "card": 1, "subtask": other, "summary": "About the other step"})
        wait = {"op": "wait", "card": 1, "subtask": watched, "after": done["rev"], "timeout": 1, "actor": "Ada"}
        quiet = self.calls(wait)[0]
        self.assertFalse(quiet["changed"])
        self.assertGreaterEqual(quiet["waited"], 1)
        self.calls({"op": "note", "card": 1, "subtask": watched, "summary": "Main's review"})
        answered = self.calls(wait)[0]
        self.assertTrue(answered["changed"])
        self.assertLess(answered["waited"], 0.5)
        self.assertEqual([note["summary"] for note in answered["notes"]], ["Main's review"])

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
