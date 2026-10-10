# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Browser contracts for reviews, assignment coordination and the inbox."""
import copy
import os
import unittest
import urllib.parse
from unittest import mock

from tests import test_server
from tests.support import make_env, run, scratch
from tests.test_server import RunningServer, board, create, http_req, life, mutate


class DelegationHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = cls.enterClassContext(scratch("wb-delegation-http-"))
        cls.env = make_env(cls.base / "home")
        cls.enterClassContext(mock.patch.dict(os.environ, cls.env, clear=True))
        cls.server = RunningServer(cls.base, cls.env)
        cls.addClassCleanup(cls.server.stop)
        cls.origin = cls.server.root

    new_board = test_server.MultiBoardApiTest.new_board

    def assignment(self, ctx, card_id, op, *, actor="Main", ids=None, **details):
        status, result, _ = life(ctx, card_id, "subtask", {"op": op, "ids": ids or [], **details}, actor=actor)
        self.assertEqual(status, 200, result)
        return result

    def parent(self, ctx):
        card = create(ctx, "Parent")
        status, result, _ = life(ctx, card["id"], "start", actor="Main")
        self.assertEqual(status, 200, result)
        result = self.assignment(ctx, card["id"], "add", texts=["Implementation", "Verification"], delegated=True)
        return card["id"], [item["id"] for item in result["items"]]

    def inbox(self, ctx, actor="Main"):
        status, result, _ = http_req(ctx["url"] + "/api/inbox?actor=" + urllib.parse.quote(actor))
        self.assertEqual(status, 200, result)
        return result["items"]

    def test_review_scope_dependencies_block_and_inbox(self):
        ctx = self.new_board()
        card_id, ids = self.parent(ctx)
        self.assignment(ctx, card_id, "configure", ids=[ids[0]], scope=["src"], reviewRequired=True)
        self.assignment(ctx, card_id, "configure", ids=[ids[1]], scope=["src/tests"], dependsOn=[ids[0]])
        status, denied, _ = life(ctx, card_id, "subtask", {"op": "configure", "ids": [ids[0]], "scope": ["other"]}, actor="Worker")
        self.assertEqual(status, 409, denied)
        self.assignment(ctx, card_id, "claim", ids=[ids[0]], actor="Worker")
        status, conflicts, _ = http_req(ctx["url"] + f"/api/card/{card_id}/scopes?subtask={ids[1]}")
        self.assertEqual(status, 200, conflicts)
        self.assertEqual(conflicts["conflicts"][0]["subtaskId"], ids[0])
        status, denied, _ = life(ctx, card_id, "subtask", {"op": "claim", "ids": [ids[1]]}, actor="Verifier")
        self.assertEqual((status, denied["code"]), (409, "deps"))
        self.assignment(ctx, card_id, "block", ids=[ids[0]], actor="Worker", reason="Waiting on spec", until="Spec approved")
        self.assertEqual(self.inbox(ctx)[0]["kind"], "blocker")
        self.assignment(ctx, card_id, "resume", ids=[ids[0]], actor="Worker")
        self.assignment(ctx, card_id, "done", ids=[ids[0]], actor="Worker", result="Implementation verified")
        row = self.inbox(ctx)[0]
        self.assertEqual((row["kind"], row["result"]), ("review", "Implementation verified"))
        self.assignment(ctx, card_id, "request-changes", ids=[ids[0]], reason="Cover the error case")
        self.assertEqual(self.inbox(ctx, "Worker")[0]["kind"], "changes_requested")
        self.assignment(ctx, card_id, "done", ids=[ids[0]], actor="Worker", result="Error case covered")
        self.assignment(ctx, card_id, "accept", ids=[ids[0]])
        self.assertEqual(self.inbox(ctx), [])
        self.assignment(ctx, card_id, "claim", ids=[ids[1]], actor="Verifier")

    def test_findings_acknowledge_many_in_one_write(self):
        ctx = self.new_board()
        card_id, _ = self.parent(ctx)
        for summary in ("First finding", "Second finding"):
            result = run(["note", card_id, "--board", ctx["name"], "--summary", summary, "--actor", "Worker"],
                         cwd=self.base, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
        note_ids = [row["note"]["id"] for row in self.inbox(ctx) if row["kind"] == "finding"]
        self.assertEqual(len(note_ids), 2)
        path = f"/api/card/{card_id}/inbox"
        before = board(ctx)
        status, denied, _ = mutate(ctx, path, {"noteIds": note_ids}, actor="Worker")
        self.assertEqual(status, 409, denied)
        status, acked, _ = mutate(ctx, path, {"noteIds": note_ids}, actor="Main", rev=before["rev"])
        self.assertEqual(status, 200, acked)
        self.assertEqual(acked["rev"], before["rev"] + 1)
        self.assertEqual(acked["card"]["history"], before["cards"][0]["history"])
        self.assertEqual(self.inbox(ctx), [])
        status, again, _ = mutate(ctx, path, {"noteIds": note_ids[:1]}, actor="Main")
        self.assertEqual(status, 200, again)
        self.assertEqual(self.inbox(ctx), [])
        status, conflict, _ = mutate(ctx, path, {"noteIds": note_ids}, actor="Main", rev=before["rev"])
        self.assertEqual(status, 409, conflict)
        self.assertIn("rev", conflict)

    def test_checklist_boolean_fields_are_not_truthiness_coerced(self):
        ctx = self.new_board()
        card = create(ctx, "Checklist", subtasks=[{"id": "parent", "text": "Parent", "children": [
            {"id": "child", "text": "Check this", "done": False, "collapsed": False}]}])
        before = board(ctx)
        for key in ("done", "collapsed"):
            for invalid in ("false", 1, None):
                tree = copy.deepcopy(card["subtasks"])
                tree[0]["children"][0][key] = invalid
                status, rejected, _ = mutate(ctx, f"/api/card/{card['id']}", {"card": {"subtasks": tree}})
                self.assertEqual(status, 422, (key, invalid, rejected))
                self.assertEqual(board(ctx), before)
        tree = copy.deepcopy(card["subtasks"])
        tree[0]["children"][0]["done"] = True
        status, changed, _ = mutate(ctx, f"/api/card/{card['id']}", {"card": {"subtasks": tree}})
        self.assertEqual(status, 200, changed)
        self.assertTrue(changed["card"]["subtasks"][0]["children"][0]["done"])


if __name__ == "__main__":
    unittest.main()
