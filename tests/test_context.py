# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Focused reads and copy-ready handoffs through the public CLI."""
from __future__ import annotations

import contextlib
import json
import unittest
from unittest import mock

from tests.support import last_json, make_env, run, scratch
from workboard import core


class FocusedContext(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.base = self.stack.enter_context(scratch("wb-context-"))
        self.home, self.project = self.base / "home", self.base / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.env = make_env(self.home)
        self.stack.enter_context(mock.patch.dict(core.os.environ, self.env, clear=True))
        self.call(["init", "registered-name"])
        self.json(["add", "--title", "Parent acceptance", "--origin", "Approved request"])
        self.json(["start", "1"])
        self.path = core.board_file("registered-name")
        with core.board_transaction(self.path) as doc:
            doc["name"] = "Display name differs"
            card = core.resolve_ref(doc, "1")
            card["notes"] = "Acceptance: producer and consumer agree"
            card["subtasks"] = [
                {"id": "ancestor", "text": "Owned by someone else", "done": False,
                 "delegation": self.assignment("claimed", "Grace"), "children": [
                    {"id": "mine", "text": "My narrow assignment", "done": False,
                     "delegation": self.assignment("claimed", "Ada"), "children": [
                        {"id": "foreign-child", "text": "Separate owner", "done": False,
                         "delegation": self.assignment("claimed", "Chen"), "children": []}]},
                    {"id": "sibling", "text": "Unrelated", "done": False, "children": []}]},
                {"id": "available", "text": "New assignment", "done": False,
                 "delegation": self.assignment("available"), "children": []},
                {"id": "legacy", "text": "Checklist only", "done": False, "children": []},
            ]
            card["comments"] = [{"id": "finding", "by": "Grace", "at": core.now_iso(),
                                 "text": "Kind: finding; Subtask: sibling; Evidence: shared contract"}]
            core.save(self.path, doc, by="Main")

    @staticmethod
    def assignment(state, owner=None, result=""):
        return {"state": state, "owner": owner,
                "claimedAt": core.now_iso() if owner else None, "result": result}

    def tearDown(self):
        self.stack.close()

    def call(self, args, actor="Main"):
        return run([*args, "--actor", actor], cwd=self.project, env=self.env)

    def json(self, args, actor="Main"):
        result = self.call([*args, "--json"], actor)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return last_json(result)

    def ids(self, payload):
        return [item["id"] for item, _ in core.iter_subtasks(payload["card"]["subtasks"])]

    def rejected(self, args, code, actor="Main"):
        before = self.path.read_bytes()
        result = self.call([*args, "--json"], actor)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(last_json(result)["code"], code, result.stdout + result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_focus_preserves_parent_shared_findings_and_separate_owners(self):
        before = self.path.read_bytes()
        self.path.with_name(core.LOCK_NAME).unlink(missing_ok=True)
        focused = self.json(["context", "1", "--subtask", "mine"], "Ada")
        self.assertEqual(self.ids(focused), ["ancestor", "mine", "foreign-child"])
        self.assertEqual(focused["focus"]["ancestorIds"], ["ancestor"])
        self.assertEqual(focused["omitted"]["unrelatedSubtasks"], 3)
        self.assertEqual(focused["card"]["activeOwner"], "Main")
        self.assertEqual(focused["card"]["origin"], "Approved request")
        self.assertIn("producer and consumer", focused["card"]["notes"])
        self.assertIn("Subtask: sibling", focused["card"]["comments"][0]["text"])
        child = focused["card"]["subtasks"][0]["children"][0]["children"][0]
        self.assertEqual(child["delegation"]["owner"], "Chen")
        mine = self.json(["context", "1", "--mine"], "Ada")
        explicit = self.json(["context", "1", "--assigned-to", "Ada"])
        self.assertEqual(mine, explicit)
        empty = self.json(["context", "1", "--mine"], "Nobody")
        self.assertEqual(empty["card"]["subtasks"], [])
        self.assertEqual(empty["focus"]["matchedSubtaskIds"], [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name(core.LOCK_NAME).exists())

    def test_selected_completed_descendants_survive_trimming(self):
        with core.board_transaction(self.path) as doc:
            card = core.resolve_ref(doc, "1")
            group = card["subtasks"][1]
            group["children"] = [
                {"id": f"done-{i}", "text": f"Done {i}", "done": True,
                 "doneAt": f"2026-01-{i + 1:02}T00:00:00Z", "doneBy": "Ada",
                 "delegation": self.assignment("completed", "Ada", "Verified"), "children": []}
                for i in range(12)]
            card["log"] = [{"id": f"{i:032x}", "at": core.now_iso(), "by": "Grace",
                            "summary": f"Shared finding {i}", "body": "Evidence"}
                           for i in range(12)]
            card["comments"] += [{"id": f"c{i}", "by": "Grace", "at": core.now_iso(), "text": f"Thread {i}"}
                                 for i in range(11)]
            core.save(self.path, doc, by="Main")
        self.assertNotIn("done-0", self.ids(self.json(["context", "1"])))
        focused = self.json(["context", "1", "--subtask", "available"])
        self.assertTrue(all(f"done-{i}" in self.ids(focused) for i in range(12)))
        self.assertEqual(focused["omitted"]["log"], 2)
        self.assertEqual(focused["omitted"]["comments"], 2)
        self.assertIn("unrelatedSubtasks", focused["omitted"])
        self.assertEqual([c["text"] for c in focused["card"]["comments"]], [f"Thread {i}" for i in range(1, 11)])
        full = self.json(["context", "1", "--subtask", "available", "--full"])
        self.assertEqual((len(full["card"]["log"]), len(full["card"]["comments"])), (12, 12))
        self.assertNotIn("mine", self.ids(full))
        mine = self.json(["context", "1", "--mine"], "Ada")
        self.assertIn("done-0", self.ids(mine))

    def test_invalid_focus_is_read_only(self):
        self.rejected(["context", "1", "--subtask", "missing"], "not_found")
        self.rejected(["context", "1", "--assigned-to", " "], "invalid")
        before = self.path.read_bytes()
        result = self.call(["context", "1", "--mine", "--subtask", "mine"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_handoff_uses_registered_board_and_worker_without_claiming(self):
        before = self.path.read_bytes()
        payload = self.json(["handoff", "1", "--subtask", "available", "--worker", "Worker B",
                             "--write-scope", "consumer.py only", "--write-scope", "tests/test_consumer.py",
                             "--peer", "Ada=agent://producer"])
        self.assertEqual(payload["boardName"], "registered-name")
        self.assertEqual(payload["worker"], "Worker B")
        self.assertEqual(payload["parentOwner"], "Main")
        self.assertEqual(payload["assignment"]["delegation"]["state"], "available")
        self.assertEqual(payload["writeScope"], ["consumer.py only", "tests/test_consumer.py"])
        self.assertEqual(payload["peers"], [{"actor": "Ada", "reference": "agent://producer"}])
        fresh = self.call(payload["commands"]["read"][1:], actor="Worker B")
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertEqual(last_json(fresh)["focus"]["subtaskId"], "available")
        self.assertIn("Approved request", payload["prompt"])
        self.assertIn("consumer.py only", payload["prompt"])
        self.assertEqual(self.path.read_bytes(), before)
        self.json(["handoff", "1", "--subtask", "mine", "--worker", "Ada", "--write-scope", "a.py"])

    def test_handoff_rejects_ambiguous_or_unavailable_work(self):
        base = ["handoff", "1", "--worker", "Ada", "--write-scope", "a.py"]
        self.rejected([*base, "--subtask", "ancestor"], "owned")
        self.rejected([*base, "--subtask", "legacy"], "state")
        self.rejected([*base, "--subtask", "mine"], "owned", actor="Chen")
        for extra in (["--write-scope", " "], ["--peer", "Ada=agent://self"],
                      ["--peer", "Chen=x", "--peer", "Chen=y"], ["--peer", "not-a-mapping"]):
            self.rejected([*base, "--subtask", "mine", *extra], "invalid")
        self.json(["subtask", "1", "configure", "available", "--on", "mine"])
        self.rejected([*base, "--subtask", "available"], "deps")
        self.json(["subtask", "1", "done", "mine", "--result", "Verified narrow work"], "Ada")
        self.rejected([*base, "--subtask", "mine"], "state")
        self.json([*base, "--subtask", "available"])
        self.json(["fly", "1", "task"])
        self.rejected([*base, "--subtask", "available"], "state")

    def test_handoff_uses_saved_scope_without_an_explicit_scope_argument(self):
        sid = self.json(["subtask", "1", "add", "Saved boundary", "--delegated"])["item"]["id"]
        self.json(["subtask", "1", "configure", sid, "--scope", "src\\worker.py", "--scope", "tests\\"])
        before = self.path.read_bytes()
        handoff = self.json(["handoff", "1", "--subtask", sid, "--worker", "Ada"])
        self.assertEqual(handoff["writeScope"], ["src/worker.py", "tests"])
        self.assertEqual(handoff["assignment"]["delegation"]["owner"], None)
        self.assertEqual(self.path.read_bytes(), before)

    def test_handoff_argv_runs_literally_with_body_on_stdin(self):
        sid, worker = "-odd Δ & {result} id", "--worker Δ & $(never) {summary}"
        with core.board_transaction(self.path) as doc:
            next(item for item, _ in core.iter_subtasks(core.resolve_ref(doc, "1")["subtasks"])
                 if item["id"] == "available")["id"] = sid
            core.save(self.path, doc, by="Main")
        handoff = self.json(["handoff", "1", f"--subtask=  {sid}  ", f"--worker={worker}",
                             "--write-scope", "consumer space/δ & 'quoted'.py"])
        self.assertEqual(set(handoff["commands"]), {"read", "claim", "publish", "complete"})
        self.assertEqual(set(handoff["placeholders"]), {"summary", "result"})
        values = {"summary": "Finding Δ; $(never) & quoted \"yes\"",
                  "result": "Verified Δ; $(never) & 'quoted' evidence"}
        body = "---\nFinding Δ with spaces; $(never) & {literal braces}"

        def execute(name, stdin=None):
            argv = []
            for argument in handoff["commands"][name]:
                option, _, value = argument.partition("=")
                if option in ("--summary", "--result"):
                    self.assertEqual(value, "{" + option[2:] + "}")
                    argument = f"{option}={values[option[2:]]}"
                argv.append(argument)
            self.assertEqual(argv[0], "workboard")
            result = run(argv[1:], cwd=self.project, env=self.env, input=stdin)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return last_json(result)

        self.assertEqual(execute("read")["focus"]["subtaskId"], sid)
        self.assertEqual(execute("claim")["item"]["delegation"]["owner"], worker)
        published = execute("publish", stdin=body)["item"]
        self.assertEqual((published["summary"], published["body"], published["by"], published["subtaskId"]),
                         (values["summary"], body, worker, sid))
        completed = execute("complete")["item"]
        self.assertEqual((completed["delegation"]["result"], completed["doneBy"]), (values["result"], worker))


if __name__ == "__main__":
    unittest.main()
