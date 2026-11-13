# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""0.4.0 policies: done/cancel over another holder (P1), done accepting finished results (P2), notes on
unheld steps (P3), comments folding into the log on migration, and schema 1-2 refusal."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from tests.support import make_env, scratch

from workboard import core

AT = "2026-01-02T03:04:05Z"


def board():
    return core.normalize_doc({"schemaVersion": core.SCHEMA_VERSION, "cards": []})


def started(doc, owner="Main"):
    card = core.create_card(doc, owner, title="Policy card")
    core.workflow_action(doc, card, "start", {}, owner)
    return card


def subtask(doc, card, op, by, **details):
    return core.workflow_action(doc, card, "subtask", {"op": op, **details}, by)


class DoneAndCancel(unittest.TestCase):
    def test_done_and_cancel_close_another_holders_card(self):
        for action, details, outcome in (("complete", {"writeup": "shipped"}, "completed"),
                                         ("cancel", {"reason": "duplicate"}, "canceled")):
            with self.subTest(action=action):
                doc = board()
                card = started(doc, "Ada")
                core.workflow_action(doc, card, action, details, "Bob")
                last = card["history"][-1]
                self.assertEqual((card["column"], card["outcome"], card["activeOwner"], card["claimedAt"]),
                                 ("done", outcome, None, None))
                self.assertEqual((last["by"], last["previousOwner"], last["to"]), ("Bob", "Ada", "done"))

    def test_the_holder_closing_records_no_previous_owner(self):
        doc = board()
        card = started(doc, "Ada")
        core.workflow_action(doc, card, "complete", {"writeup": "shipped"}, "Ada")
        self.assertNotIn("previousOwner", card["history"][-1])


class DoneAcceptsResults(unittest.TestCase):
    def setUp(self):
        self.doc = board()
        self.card = started(self.doc)
        subtask(self.doc, self.card, "add", "Main", texts=["parser", "writer"], delegated=True)
        subtask(self.doc, self.card, "claim", "W", ids=["s-1"])
        subtask(self.doc, self.card, "done", "W", ids=["s-1"], result="parser verified")

    def test_done_accepts_a_completed_result(self):
        subtask(self.doc, self.card, "rm", "Main", ids=["s-2"])
        core.workflow_action(self.doc, self.card, "complete", {"writeup": "shipped"}, "Main")
        review = self.card["subtasks"][0]["delegation"]["review"]
        self.assertEqual((self.card["column"], review["state"], review["by"]), ("done", "accepted", "Main"))
        accepted = [e for e in self.card["log"] if e["summary"] == "accepted s-1"]
        self.assertEqual([(e["kind"], e["subtaskId"]) for e in accepted], [("generated", "s-1")])
        self.assertIn(("subtask-accept", "s-1"), [(h["ev"], h.get("note")) for h in self.card["history"]])

    def test_done_refuses_unfinished_delegated_work_naming_it(self):
        subtask(self.doc, self.card, "claim", "V", ids=["s-2"])
        with self.assertRaises(core.WorkflowError) as caught:
            core.workflow_action(self.doc, self.card, "complete", {"writeup": "shipped"}, "Main")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "state"))
        self.assertIn("s-2 is claimed by V", str(caught.exception))
        self.assertNotIn("s-1", str(caught.exception))
        self.assertEqual((self.card["column"], self.card["subtasks"][0]["delegation"]["review"]["state"]),
                         ("inprogress", "pending"))


class StepNotes(unittest.TestCase):
    def test_anyone_notes_an_unheld_step_but_not_a_held_one(self):
        doc = board()
        card = started(doc)
        subtask(doc, card, "add", "Main", texts=["plain step"])
        subtask(doc, card, "add", "Main", texts=["open work", "held work"], delegated=True)
        subtask(doc, card, "claim", "W", ids=["s-3"])
        for sid in ("s-1", "s-2"):
            self.assertEqual(core.publish_subtask_note(doc, card, sid, f"seen {sid}", "", "X")["subtaskId"], sid)
        with self.assertRaises(core.WorkflowError) as caught:
            core.publish_subtask_note(doc, card, "s-3", "seen s-3", "", "X")
        self.assertEqual(caught.exception.code, "owned")
        self.assertIn("held by W", str(caught.exception))
        for by in ("W", "Main"):
            core.publish_subtask_note(doc, card, "s-3", f"from {by}", "", by)


class LegacyBoards(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, make_env(self.enterContext(scratch("wb-policy-")) / "home"),
                                          clear=True))
        self.path = core.boards_dir() / "legacy" / "board.json"
        self.path.parent.mkdir(parents=True)

    def write(self, doc) -> bytes:
        data = json.dumps(doc).encode("utf-8")
        self.path.write_bytes(data)
        return data

    def test_migration_folds_comments_into_the_log_by_time(self):
        long = "word " * 60 + "\nsecond line"
        log = [{"id": "a" * 32, "at": "2026-01-01T00:00:00Z", "by": "x", "summary": "first", "body": ""},
               {"id": "c" * 32, "at": "2026-01-03T00:00:00Z", "by": "x", "summary": "third", "body": ""}]
        comments = [{"id": "d" * 32, "at": "2026-01-04T00:00:00Z", "by": "y", "text": "fourth"},
                    {"id": "b" * 32, "at": "2026-01-02T00:00:00Z", "by": None, "text": long,
                     "updatedAt": AT, "updatedBy": "y"}]
        self.write({"schemaVersion": 3, "name": "legacy", "rev": 7, "nextNum": 2,
                    "columns": [dict(c) for c in core.DEFAULT_COLUMNS],
                    "cards": [{"num": 1, "id": "one-1", "title": "One", "column": "task", "log": log,
                               "comments": comments, "links": ["two"], "inboxAcknowledged": {"y": []}}]})
        card = core.load(self.path)["cards"][0]
        self.assertEqual([e["id"] for e in card["log"]], ["a" * 32, "b" * 32, "c" * 32, "d" * 32])
        self.assertEqual([(e["at"], e["by"]) for e in card["log"][1::2]],
                         [("2026-01-02T00:00:00Z", None), ("2026-01-04T00:00:00Z", "y")])
        self.assertEqual((card["log"][1]["summary"], card["log"][1]["body"]),
                         core.fit_note(long, None))
        self.assertEqual((card["log"][3]["summary"], card["log"][3]["body"]), ("fourth", ""))
        self.assertEqual((card["log"][1]["editedAt"], card["log"][1]["editedBy"]), (AT, "y"))  # As note edits are.
        self.assertFalse({"editedAt", "editedBy"} & card["log"][3].keys())
        self.assertFalse({"comments", "links", "inboxAcknowledged"} & card.keys())

    def test_schema_2_board_is_refused_with_the_upgrade_path(self):
        data = self.write({"schemaVersion": 2, "name": "old", "rev": 1, "cards": []})
        with self.assertRaises(core.WorkflowError) as caught:
            core.load(self.path)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("unsupported board schemaVersion 2; supported: 3, 4", str(caught.exception))
        self.assertIn("0.3.11", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), data)
        self.assertFalse(self.path.with_name(core.DB_NAME).exists())


if __name__ == "__main__":
    unittest.main()
