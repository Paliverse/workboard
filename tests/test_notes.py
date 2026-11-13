"""Notes timeline: entry validation, authorship and inbox claims.

Pure in-process checks on ``workboard.core``; nothing here touches the filesystem.
"""
from __future__ import annotations

import unittest

import tests.support  # noqa: F401  (puts src/ on sys.path)

from workboard import core


def board(*cards) -> dict:
    return core.normalize_doc({"schemaVersion": 3, "cards": list(cards)})


class TimelineEntries(unittest.TestCase):
    def card(self) -> dict:
        return core.normalize_card({"id": "c", "num": 1, "title": "T"})

    def test_append_note_validates_and_records(self):
        card = self.card()
        for summary, body in (("   ", ""), (None, ""), ("ok", "b" * (core.NOTE_BODY_MAX + 1)), ("ok", 5),
                              ("s" * 161, "b" * (core.NOTE_BODY_MAX - 100))):  # Moved summary overflows the body.
            with self.subTest(summary=str(summary)[:20], body=str(body)[:20]):
                with self.assertRaises(core.WorkflowError) as caught:
                    core.append_note(card, summary, body, "tester")
                self.assertEqual((caught.exception.status, caught.exception.code), (422, "invalid"))
        self.assertEqual((card["log"], card["history"]), ([], []))

        entry = core.append_note(card, " " + "s" * 160 + " ", "b" * core.NOTE_BODY_MAX + "\n\n", "Ada")
        self.assertEqual(card["log"], [entry])
        self.assertRegex(entry["id"], r"^[0-9a-f]{32}$")
        self.assertRegex(entry["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual((entry["by"], entry["summary"], entry["body"]),
                         ("Ada", "s" * 160, "b" * core.NOTE_BODY_MAX))
        self.assertEqual({k: card["history"][-1][k] for k in ("ev", "by", "note")},
                         {"ev": "note", "by": "Ada", "note": "s" * 80})
        self.assertEqual(core.append_note(card, "No body", None, "Ada")["body"], "")

    def test_long_or_multiline_summary_moves_to_the_body(self):
        card = self.card()
        words = ("word " * 60).strip()   # 299 characters
        cases = [
            ("s" * 160, None, ("s" * 160, "")),                                   # at the limit: kept
            ("s" * 161, None, ("s" * 159 + "…", "s" * 161)),                      # no whitespace: hard cut
            (words, "evidence", (("word " * 31).strip() + "…", words + "\n\nevidence")),
            ("two\nlines", "", ("two", "two\nlines")),                             # first line fits: no …
            ("carriage\rreturn", "- e", ("carriage", "carriage\rreturn\n\n- e")),
        ]
        for summary, body, expected in cases:
            with self.subTest(summary=summary[:20]):
                entry = core.append_note(card, summary, body, "Ada")
                self.assertEqual((entry["summary"], entry["body"]), expected)
                self.assertLessEqual(len(entry["summary"]), core.NOTE_SUMMARY_MAX)

    def test_only_the_author_edits_or_removes_a_note(self):
        doc = board({"id": "c", "num": 1, "column": "inprogress", "activeOwner": "Main"})
        card = doc["cards"][0]
        note = core.append_note(card, "Finding", "old body", "Ada")
        for change in (lambda by: core.edit_note(card, note["id"], "Hijacked", None, by),
                       lambda by: core.remove_note(card, note["id"], by)):
            with self.assertRaises(core.WorkflowError) as caught:
                change("Grace")
            self.assertEqual((caught.exception.code, "Ada" in str(caught.exception)), ("owned", True))
        with self.assertRaises(core.WorkflowError) as caught:
            core.remove_note(card, "f" * 32, "Ada")
        self.assertEqual(caught.exception.status, 404)

        edited = core.edit_note(card, note["id"], "x" * 200, None, "Ada")
        self.assertEqual((edited["summary"], edited["body"]), ("x" * 159 + "…", "x" * 200 + "\n\nold body"))
        self.assertIn("editedAt", edited)
        self.assertEqual(core.edit_note(card, note["id"], None, "new body", "Ada")["summary"], "x" * 159 + "…")
        core.remove_note(card, note["id"], "Ada")
        self.assertEqual(card["log"], [])
        self.assertEqual([(e["ev"], e["by"], e["note"]) for e in card["history"][-3:]],
                         [("note-edited", "Ada", note["id"]), ("note-edited", "Ada", note["id"]),
                          ("note-removed", "Ada", note["id"])])
        core.normalize_doc(doc)   # Still a valid board.

    def test_workflow_records_cannot_be_edited_or_removed(self):
        doc = board({"id": "c", "num": 1, "title": "T", "column": "task"})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "start", {}, "Main")
        core.subtask_action(doc, card, "add", {"texts": ["Step"], "delegated": True}, "Main")
        core.subtask_action(doc, card, "claim", {"ids": ["s-1"]}, "W")
        core.subtask_action(doc, card, "done", {"ids": ["s-1"], "result": "Shipped"}, "W")
        result = card["log"][-1]
        with self.assertRaises(core.WorkflowError) as caught:
            core.remove_note(card, result["id"], "W")
        self.assertEqual(caught.exception.code, "state")
        self.assertIn(result, card["log"])

    def test_note_action_ignores_ownership(self):
        doc = board({"id": "c", "num": 1, "column": "inprogress", "activeOwner": "Ada"})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "note", {"summary": "Reviewer remark", "body": "- looks good"}, "Grace")
        self.assertEqual((card["log"][-1]["by"], card["log"][-1]["body"], card["activeOwner"]),
                         ("Grace", "- looks good", "Ada"))

    def test_subtask_notes_follow_the_holder_not_the_column(self):
        held = {"id": "s-1", "text": "Recheck", "done": True, "children": [], "delegation": {
            "state": "completed", "owner": "Ada", "claimedAt": "2026-10-10T00:00:00Z", "result": "Verified"}}
        doc = board({"id": "c", "num": 1, "column": "done", "subtasks": [held]})
        card = doc["cards"][0]
        self.assertEqual(core.publish_subtask_note(doc, card, "s-1", "Recheck passes", "", "Ada")["subtaskId"], "s-1")
        for actor in ("Main", "Grace"):  # A Done card has no owner, so only the holder notes its subtask.
            with self.assertRaises(core.WorkflowError) as caught:
                core.publish_subtask_note(doc, card, "s-1", "Not mine", "", actor)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "owned"))
            self.assertIn("Ada", str(caught.exception))
        self.assertEqual([entry["by"] for entry in card["log"]], ["Ada"])

    def test_inbox_keeps_claims_on_a_blocked_card(self):
        doc = board({"id": "c", "num": 1, "title": "T", "column": "task"})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "start", {}, "Main")
        core.subtask_action(doc, card, "add", {"texts": ["Worker step"], "delegated": True}, "Main")
        core.subtask_action(doc, card, "claim", {"ids": ["s-1"]}, "W")
        core.workflow_action(doc, card, "block", {"reason": "Waiting on W", "until": "W reports"}, "Main")
        self.assertEqual([(row["kind"], row["subtaskId"], row["owner"]) for row in core.inbox(doc, "Main")],
                         [("claim", "s-1", "W")])

    def test_normalize_card_refuses_malformed_log(self):
        valid = {"id": "a" * 32, "at": "2026-09-01", "by": None, "summary": "ok", "body": ""}
        for log in ({}, ["entry"], [{**valid, "id": "A" * 32}], [valid, valid],
                    [{**valid, "summary": "one\ntwo"}], [{**valid, "at": None}], [{**valid, "by": 7}]):
            with self.subTest(log=str(log)[:60]):
                with self.assertRaises(core.WorkflowError) as caught:
                    core.normalize_card({"id": "c", "num": 1, "log": log})
                self.assertEqual((caught.exception.status, caught.exception.code), (422, "invalid"))
        kept = core.normalize_card({"id": "c", "num": 1, "log": [{**valid, "summary": " ok ", "extra": 1}]})
        self.assertEqual(kept["log"], [{**valid, "summary": "ok", "extra": 1}])

    def test_log_is_server_owned(self):
        self.assertIn("log", core.PROTECTED_CARD_FIELDS)


if __name__ == "__main__":
    unittest.main()
