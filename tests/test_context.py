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
                {"id": "held", "text": "Owned by someone else", "done": False,
                 "delegation": self.assignment("claimed", "Grace")},
                {"id": "mine", "text": "My narrow assignment", "done": False,
                 "delegation": self.assignment("claimed", "Ada")},
                {"id": "separate", "text": "Separate owner", "done": False,
                 "delegation": self.assignment("claimed", "Chen")},
                {"id": "sibling", "text": "Unrelated", "done": False},
                {"id": "available", "text": "New assignment", "done": False,
                 "delegation": self.assignment("available")},
                {"id": "legacy", "text": "Checklist only", "done": False},
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

    def test_focus_keeps_card_requirements_and_only_the_selected_work(self):
        before = self.path.read_bytes()
        self.path.with_name(core.LOCK_NAME).unlink(missing_ok=True)
        focused = self.json(["context", "1", "--subtask", "mine"], "Ada")
        self.assertEqual(self.ids(focused), ["mine"])
        self.assertEqual(focused["num"], 1)
        self.assertNotIn("ancestorIds", focused["focus"])
        self.assertEqual(focused["omitted"]["unrelatedSubtasks"], 5)
        self.assertEqual(focused["card"]["activeOwner"], "Main")
        self.assertEqual(focused["card"]["origin"], "Approved request")
        self.assertIn("producer and consumer", focused["card"]["notes"])
        self.assertIn("Subtask: sibling", focused["card"]["comments"][0]["text"])
        mine = self.json(["context", "1", "--mine"], "Ada")
        explicit = self.json(["context", "1", "--assigned-to", "Ada"])
        self.assertEqual(mine, explicit)
        empty = self.json(["context", "1", "--mine"], "Nobody")
        self.assertEqual(empty["card"]["subtasks"], [])
        self.assertEqual(empty["focus"]["matchedSubtaskIds"], [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name(core.LOCK_NAME).exists())

    def test_history_only_with_full(self):
        history = self.json(["context", "1", "--full"])["card"]["history"]
        self.assertTrue(history)
        for args in (["context", "1"], ["context", "1", "--subtask", "mine"]):
            trimmed = self.json(args)
            self.assertNotIn("history", trimmed["card"])
            self.assertEqual(trimmed["omitted"]["history"], len(history))

    def test_selected_completed_work_survives_trimming(self):
        with core.board_transaction(self.path) as doc:
            card = core.resolve_ref(doc, "1")
            card["subtasks"] += [
                {"id": f"done-{i}", "text": f"Done {i}", "done": True,
                 "doneAt": f"2026-01-{i + 1:02}T00:00:00Z", "doneBy": "Ada",
                 "delegation": self.assignment("completed", "Ada", "Verified")}
                for i in range(12)]
            card["log"] = [{"id": f"{i:032x}", "at": core.now_iso(), "by": "Grace",
                            "summary": f"Shared finding {i}", "body": "Evidence"}
                           for i in range(12)]
            card["comments"] += [{"id": f"c{i}", "by": "Grace", "at": core.now_iso(), "text": f"Thread {i}"}
                                 for i in range(11)]
            core.save(self.path, doc, by="Main")
        trimmed = self.json(["context", "1"])
        self.assertNotIn("done-0", self.ids(trimmed))
        self.assertEqual(trimmed["omitted"]["doneSubtasks"], 2)
        focused = self.json(["context", "1", "--subtask", "done-0"])
        self.assertEqual(self.ids(focused), ["done-0"])
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
        self.json(["subtask", "1", "configure", "available", "--scope", "consumer.py only",
                   "--scope", "tests/test_consumer.py"])
        before = self.path.read_bytes()
        payload = self.json(["handoff", "1", "--subtask", "available", "--worker", "Worker B"])
        self.assertEqual(set(payload), {"ok", "rev", "num", "worker", "previousOwner", "writeScope",
                                        "scopeWarnings", "requests", "prompt"})
        self.assertEqual((payload["num"], payload["worker"], payload["previousOwner"]), (1, "Worker B", None))
        self.assertEqual(payload["writeScope"], ["consumer.py only", "tests/test_consumer.py"])
        self.assertIn("registered-name", payload["prompt"])  # The registered name, not the display name.
        self.assertIn("consumer.py only", payload["prompt"])
        fresh = run(["--board=registered-name", "--actor=Worker B", "call"], cwd=self.project, env=self.env,
                    input=json.dumps(payload["requests"]["read"]) + "\n")
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertEqual(last_json(fresh)["focus"]["subtaskId"], "available")
        self.assertEqual(self.path.read_bytes(), before)
        self.json(["handoff", "1", "--subtask", "mine", "--worker", "Ada"])

    def test_handoff_rejects_ambiguous_or_unavailable_work(self):
        base = ["handoff", "1", "--worker", "Ada"]
        takeover = self.json([*base, "--subtask", "held"])  # Claimed by Grace: Ada takes it over.
        self.assertEqual(takeover["previousOwner"], "Grace")
        self.assertEqual(takeover["requests"]["claim"]["op"], "subtask.takeover")
        self.rejected([*base, "--subtask", "legacy"], "state")
        self.rejected([*base, "--subtask", "mine"], "owned", actor="Chen")
        self.rejected([*base, "--subtask", " "], "invalid")
        self.json(["subtask", "1", "configure", "available", "--on", "mine"])
        self.rejected([*base, "--subtask", "available"], "deps")
        self.json(["subtask", "1", "done", "mine", "--result", "Verified narrow work"], "Ada")
        self.rejected([*base, "--subtask", "mine"], "state")
        self.rejected([*base, "--subtask", "available"], "deps")  # The prerequisite's result awaits acceptance.
        self.json(["subtask", "1", "accept", "mine"])
        self.json([*base, "--subtask", "available"])
        self.json(["fly", "1", "task"])
        self.rejected([*base, "--subtask", "available"], "state")

    def test_handoff_uses_the_saved_scope(self):
        sid = self.json(["subtask", "1", "add", "Saved boundary", "--delegated"])["item"]["id"]
        self.json(["subtask", "1", "configure", sid, "--scope", "src\\worker.py", "--scope", "tests\\"])
        before = self.path.read_bytes()
        handoff = self.json(["handoff", "1", "--subtask", sid, "--worker", "Ada"])
        self.assertEqual((handoff["writeScope"], handoff["previousOwner"]), (["src/worker.py", "tests"], None))
        self.assertEqual(self.path.read_bytes(), before)

    def test_handoff_without_a_scope_is_read_only(self):
        before = self.path.read_bytes()
        handoff = self.json(["handoff", "1", "--subtask", "available", "--worker", "Ada"])
        self.assertEqual((handoff["writeScope"], handoff["previousOwner"]), ([], None))
        self.assertEqual(self.path.read_bytes(), before)

    def test_handoff_requests_run_through_call_with_multiline_text(self):
        sid, worker = "-odd Δ & {result} id", "--worker Δ & $(never) {summary}"
        with core.board_transaction(self.path) as doc:
            item = next(item for item in core.resolve_ref(doc, "1")["subtasks"] if item["id"] == "available")
            item["id"], item["delegation"]["scope"] = sid, ["src"]
            core.save(self.path, doc, by="Main")
        handoff = self.json(["handoff", "1", f"--subtask={sid}", f"--worker={worker}"])
        self.assertEqual((handoff["writeScope"], set(handoff["requests"])),
                         (["src"], {"read", "claim", "publish", "complete"}))
        values = {"summary": "Finding Δ; $(never) & \"quoted\"", "body": "Evidence:\n- line 2 & ^%PATH%\n",
                  "result": "Verified Δ.\nTests: 14/14 & 'quoted'"}

        def send(name):
            request = {key: values[key] if key in values and value == "{" + key + "}" else value
                       for key, value in handoff["requests"][name].items()}
            result = run(["--board=registered-name", f"--actor={worker}", "call"], cwd=self.project, env=self.env,
                         input=json.dumps(request) + "\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return last_json(result)

        self.assertEqual(send("read")["focus"]["subtaskId"], sid)
        self.assertEqual(send("claim")["item"]["delegation"]["owner"], worker)
        published = send("publish")["item"]
        self.assertEqual((published["summary"], published["body"], published["by"], published["subtaskId"]),
                         (values["summary"], values["body"].rstrip(), worker, sid))
        completed = send("complete")["item"]
        self.assertEqual((completed["delegation"]["result"], completed["doneBy"]), (values["result"], worker))


if __name__ == "__main__":
    unittest.main()
