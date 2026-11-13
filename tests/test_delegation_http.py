# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Browser contracts for reviews, assignment coordination and the inbox."""
import copy
import os
import unittest
import urllib.parse
from unittest import mock

from tests import test_server
from tests.support import make_env, scratch
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

    def test_review_scope_and_inbox(self):
        ctx = self.new_board()
        card_id, ids = self.parent(ctx)
        before = board(ctx)
        status, removed, _ = life(ctx, card_id, "subtask", {"op": "configure", "ids": [ids[0]], "scope": ["src"],
                                                            "reviewRequired": True}, actor="Main")
        self.assertEqual((status, removed["code"]), (422, "invalid"), removed)
        self.assertEqual(board(ctx), before)
        self.assignment(ctx, card_id, "configure", ids=[ids[0]], scope=["src"])
        self.assignment(ctx, card_id, "configure", ids=[ids[1]], scope=["src/tests"])
        status, denied, _ = life(ctx, card_id, "subtask", {"op": "configure", "ids": [ids[0]], "scope": ["other"]}, actor="Worker")
        self.assertEqual(status, 409, denied)
        self.assignment(ctx, card_id, "claim", ids=[ids[0]], actor="Worker")
        status, conflicts, _ = http_req(ctx["url"] + f"/api/card/{card_id}/scopes?subtask={ids[1]}")
        self.assertEqual(status, 200, conflicts)
        self.assertEqual(conflicts["conflicts"][0]["subtaskId"], ids[0])
        self.assertTrue(conflicts["conflicts"][0]["claimedAt"])
        self.assignment(ctx, card_id, "done", ids=[ids[0]], actor="Worker", result="Implementation verified")
        row = self.inbox(ctx)[0]
        self.assertEqual((row["kind"], row["result"]), ("review", "Implementation verified"))
        self.assignment(ctx, card_id, "request-changes", ids=[ids[0]], reason="Cover the error case")
        self.assertEqual(self.inbox(ctx, "Worker")[0]["kind"], "changes_requested")
        self.assignment(ctx, card_id, "done", ids=[ids[0]], actor="Worker", result="Error case covered")
        self.assignment(ctx, card_id, "accept", ids=[ids[0]])
        self.assertEqual(self.inbox(ctx), [])
        self.assignment(ctx, card_id, "claim", ids=[ids[1]], actor="Verifier")
        self.assignment(ctx, card_id, "done", ids=[ids[1]], actor="Verifier", result="Verification passed")
        row, = self.inbox(ctx)  # Every result reaches the parent owner; the card's done accepts it too.
        self.assertEqual((row["kind"], row["subtaskId"]), ("review", ids[1]))
        status, done, _ = life(ctx, card_id, "complete", {"writeup": "Results accepted by done"}, actor="Main")
        self.assertEqual(status, 200, done)
        self.assertEqual([st["delegation"]["review"]["state"] for st in done["card"]["subtasks"]], ["accepted"] * 2)
        self.assertEqual(self.inbox(ctx), [])

    def test_checklist_boolean_fields_are_not_truthiness_coerced(self):
        ctx = self.new_board()
        card = create(ctx, "Checklist", subtasks=[{"id": "parent", "text": "Parent"},
                                                  {"id": "child", "text": "Check this", "done": False}])
        before = board(ctx)
        for invalid in ("false", 1, None):
            tree = copy.deepcopy(card["subtasks"])
            tree[1]["done"] = invalid
            status, rejected, _ = mutate(ctx, f"/api/card/{card['id']}", {"card": {"subtasks": tree}})
            self.assertEqual(status, 422, (invalid, rejected))
            self.assertEqual(board(ctx), before)
        tree = copy.deepcopy(card["subtasks"])
        tree[1]["done"] = True
        status, changed, _ = mutate(ctx, f"/api/card/{card['id']}", {"card": {"subtasks": tree}})
        self.assertEqual(status, 200, changed)
        self.assertTrue(changed["card"]["subtasks"][1]["done"])
        stale = [{"id": "parent", "text": "Parent", "collapsed": True, "children": [{"id": "child", "text": "Kept"}]}]
        status, flattened, _ = mutate(ctx, f"/api/card/{card['id']}", {"card": {"subtasks": stale}})
        self.assertEqual(status, 200, flattened)  # A stale nested client keeps its items, flat.
        self.assertEqual([(st["id"], st["text"]) for st in flattened["card"]["subtasks"]],
                         [("parent", "Parent"), ("child", "Kept")])
        self.assertFalse(any("children" in st or "collapsed" in st for st in flattened["card"]["subtasks"]))


if __name__ == "__main__":
    unittest.main()
