"""Notes timeline (schema v3): legacy note migration, summary derivation and entry validation.

Pure in-process checks on ``workboard.core``; nothing here touches the filesystem.
"""
from __future__ import annotations

import hashlib
import re
import unittest
from unittest import mock
from collections import Counter

import tests.support  # noqa: F401  (puts src/ on sys.path)

from workboard import core

# (label, legacy notes, expected pinned notes, expected (at, by, summary, body) entries)
MIGRATIONS = [
    ("date-only log", "[2026-09-01] Fixed the parser\n[2026-09-02] Added tests. 12 pass",
     "", [("2026-09-01", None, "Fixed the parser", ""), ("2026-09-02", None, "Added tests.", "12 pass")]),
    ("actor stamps", "[2026-09-03 ada] Reviewed the diff\n[2026-09-04 Codex agent] Shipped it",
     "", [("2026-09-03", "ada", "Reviewed the diff", ""), ("2026-09-04", "Codex agent", "Shipped it", "")]),
    ("free text first", "Goal: faster sync\nKeep it simple.\n[2026-09-05 bob] Started",
     "Goal: faster sync\nKeep it simple.", [("2026-09-05", "bob", "Started", "")]),
    ("free text only", "Just context\n[not a stamp] here",
     "Just context\n[not a stamp] here", []),
    ("continuation lines", "[2026-09-06 ada] Root cause found\n- lock held across fsync\n- see `abc123`",
     "", [("2026-09-06", "ada", "Root cause found", "- lock held across fsync\n- see `abc123`")]),
    ("heading after entries", "[2026-09-07] Drafted plan\n  ## Acceptance criteria\n- ships\n[2026-09-08] Done",
     "## Acceptance criteria\n- ships",
     [("2026-09-07", None, "Drafted plan", ""), ("2026-09-08", None, "Done", "")]),
    ("blank lines", "Intro\n\n[2026-09-09 ada] First\n\n  details after blank\n\nmore detail\n\n\n"
                    "[2026-09-10] Second\n\n",
     "Intro", [("2026-09-09", "ada", "First", "details after blank\n\nmore detail"),
               ("2026-09-10", None, "Second", "")]),
    ("empty entries", "[2026-09-11 ada]\n[2026-09-12] \n\n",
     "", [("2026-09-11", "ada", "(empty note)", ""), ("2026-09-12", None, "(empty note)", "")]),
]


def synthetic_note() -> str:
    """Fifty stamped entries mixing every summary rule, continuations, and pinned headings."""
    firsts = ("Step {n} done; details follow", "Step {n} finished. Evidence below", "{long}", "Short {n}")
    lines = ["Pinned intro line"]
    for n in range(50):
        by = f" agent-{n % 3}" if n % 2 else ""
        lines.append(f"[2026-08-{n % 28 + 1:02d}{by}] " + firsts[n % 4].format(n=n, long="word " * (40 + n)))
        if n % 3 == 0:
            lines += [f"- evidence {n}", ""]
        if n % 10 == 9:
            lines += ["## Heading kept pinned", f"pinned text {n}"]
    return "\n".join(lines)


def nonws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def is_subsequence(needle: str, haystack: str) -> bool:
    remaining = iter(haystack)
    return all(char in remaining for char in needle)


class LegacyMigration(unittest.TestCase):
    def test_migration_table(self):
        for label, legacy, notes, entries in MIGRATIONS:
            with self.subTest(label):
                pinned, log = core.split_legacy_notes("card", legacy)
                self.assertEqual(pinned, notes)
                self.assertEqual([(e["at"], e["by"], e["summary"], e["body"]) for e in log], entries)

    def assert_lossless(self, legacy: str) -> list:
        notes, log = core.split_legacy_notes("card", legacy)
        self.assertTrue(is_subsequence(nonws(notes), nonws(legacy)))
        rebuilt = [notes]
        for entry in log:
            summary, body = entry["summary"], entry["body"]
            if summary.endswith("…") and nonws(body).startswith(nonws(summary[:-1])):
                text = body   # rule 3 keeps the full text in the body
            elif (summary, body) == ("(empty note)", ""):
                text = ""
            else:
                text = summary + body
            stamp = f"[{entry['at']}{' ' + entry['by'] if entry['by'] else ''}]"
            self.assertTrue(is_subsequence(nonws(stamp + text), nonws(legacy)), entry)
            rebuilt.append(stamp + text)
        original, migrated = Counter(nonws(legacy)), Counter(nonws("".join(rebuilt)))
        self.assertEqual(migrated - original, Counter(), "migration invented text")
        lost = original - migrated
        self.assertLessEqual(set(lost), {";"}, "only a summary's trailing ';' may be dropped")
        self.assertLessEqual(lost[";"], len(log))
        return log

    def test_migration_is_lossless(self):
        for label, legacy, _, _ in MIGRATIONS:
            with self.subTest(label):
                self.assert_lossless(legacy)
        self.assertEqual(len(self.assert_lossless(synthetic_note())), 50)

    def test_migrated_ids_are_deterministic(self):
        raw = {"schemaVersion": 2, "cards": [{"id": "card-a", "num": 1,
                                              "notes": "[2026-09-01] same\n[2026-09-01] same"}]}
        first = core.normalize_doc(raw)["cards"][0]["log"]
        self.assertEqual(core.normalize_doc(raw)["cards"][0]["log"], first)
        ids = [entry["id"] for entry in first]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids[0], hashlib.sha256(b"card-a\n0\n[2026-09-01] same").hexdigest()[:32])

    def test_v3_notes_never_resplit(self):
        stamped = "[2026-09-01 ada] looks like a legacy stamp"
        card = core.normalize_doc({"schemaVersion": 3, "cards": [{"id": "c", "num": 1, "notes": stamped}]})["cards"][0]
        self.assertEqual((card["notes"], card["log"]), (stamped, []))
        migrated = core.normalize_doc({"schemaVersion": 2, "cards": [{"id": "c", "num": 1, "notes": f"Pinned\n{stamped}"}]})
        self.assertEqual((migrated["schemaVersion"], migrated["cards"][0]["notes"],
                          len(migrated["cards"][0]["log"])), (3, "Pinned", 1))
        self.assertEqual(core.normalize_doc(migrated), migrated)
        migrated["cards"][0]["notes"] = stamped   # a pinned-notes edit that looks like a stamp stays pinned
        self.assertEqual(core.normalize_doc(migrated)["cards"][0]["notes"], stamped)

    def test_v1_doc_migrates(self):
        doc = core.normalize_doc({"cards": [{"id": "old", "num": 1,
                                             "notes": "Pinned\n[2026-01-02 ada] Legacy v1 note"}]})
        card = doc["cards"][0]
        self.assertEqual((doc["schemaVersion"], card["notes"]), (3, "Pinned"))
        self.assertEqual([(e["at"], e["by"], e["summary"], e["body"]) for e in card["log"]],
                         [("2026-01-02", "ada", "Legacy v1 note", "")])

    def test_oversized_legacy_entry_stays_pinned(self):
        legacy = "[2026-09-01] big\n" + "y" * (core.NOTE_BODY_MAX + 1)
        doc = core.normalize_doc({"schemaVersion": 2, "cards": [{"id": "c", "num": 1, "notes": legacy}]})
        self.assertEqual((doc["cards"][0]["notes"], doc["cards"][0]["log"]), (legacy, []))
        self.assertEqual(core.normalize_doc(doc), doc)


class DeriveSummary(unittest.TestCase):
    def test_rules(self):
        line = "abcdefghi " * 39 + "abcdefghij"
        self.assertEqual(len(line), 400)
        cases = [
            # 1: first sentence end within 160 characters; ';' is dropped, '.', '!', '?' kept
            ("Fixed the race. Added a lock around save", ("Fixed the race.", "Added a lock around save")),
            ("Works now! Verified on Windows\n- 12 tests", ("Works now!", "Verified on Windows\n- 12 tests")),
            ("Moved the cache; callers updated\nsee notes", ("Moved the cache", "callers updated\nsee notes")),
            ("a" * 159 + ". rest", ("a" * 159 + ".", "rest")),
            ("; leading semicolon", ("; leading semicolon", "")),
            # 2: a first line of at most 160 characters is the summary
            ("Short title\nline two\nline three", ("Short title", "line two\nline three")),
            ("x" * 160, ("x" * 160, "")),
            # 3: longer first line: cut at whitespace (hard cut if none) plus …, full text kept in the body
            (line + "\nsecond line", (line[:149] + "…", line + "\nsecond line")),
            ("a" * 160 + ". rest", ("a" * 159 + "…", "a" * 160 + ". rest")),
            ("x" * 400, ("x" * 159 + "…", "x" * 400)),
            # 4: empty text
            ("", ("(empty note)", "")),
            (" \n\n\t", ("(empty note)", "")),
        ]
        for text, expected in cases:
            with self.subTest(text[:40]):
                summary, body = core.derive_summary(text)
                self.assertEqual((summary, body), expected)
                self.assertLessEqual(len(summary), core.NOTE_SUMMARY_MAX)


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
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "column": "inprogress", "activeOwner": "Main"}]})
        card = doc["cards"][0]
        note = core.append_note(card, "Finding", "old body", "Ada")
        core.ack_inbox_notes(doc, card, [note["id"]], "Main")
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
        self.assertEqual((card["log"], card["inboxAcknowledged"]["Main"]), ([], []))
        self.assertEqual([(e["ev"], e["by"], e["note"]) for e in card["history"][-3:]],
                         [("note-edited", "Ada", note["id"]), ("note-edited", "Ada", note["id"]),
                          ("note-removed", "Ada", note["id"])])
        core.normalize_doc(doc)   # Still a valid board.

    def test_an_edited_finding_returns_to_the_inbox(self):
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "title": "T", "column": "task"}]})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "start", {}, "Main")
        core.subtask_action(doc, card, "add", {"texts": ["Step"], "delegated": True}, "Main")
        core.subtask_action(doc, card, "claim", {"ids": ["s-1"]}, "W")

        def findings():
            return [row["note"]["id"] for row in core.inbox(doc, "Main") if row["kind"] == "finding"]

        # A: an acknowledged finding that its author edits is new again.
        shared = core.append_note(card, "Tests pass", "", "W")
        core.ack_inbox_notes(doc, card, [shared["id"]], "Main")
        self.assertEqual(findings(), [])
        core.edit_note(card, shared["id"], "Tests FAIL on X", None, "W")
        self.assertEqual(findings(), [shared["id"]])
        core.ack_inbox_notes(doc, card, [shared["id"]], "Main")

        # B: a subtask note posted before the acceptance, edited after it.
        early = core.publish_subtask_note(doc, card, "s-1", "Looks fine", "", "W")
        core.subtask_action(doc, card, "done", {"ids": ["s-1"], "result": "Shipped"}, "W")
        core.subtask_action(doc, card, "accept", {"ids": ["s-1"]}, "Main")
        self.assertEqual(findings(), [])
        with mock.patch.object(core, "now_iso", return_value="2999-01-01T00:00:00Z"):
            core.edit_note(card, early["id"], "Actually broken", None, "W")
        self.assertEqual(findings(), [early["id"]])

    def test_workflow_records_cannot_be_edited_or_removed(self):
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "title": "T", "column": "task"}]})
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
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "column": "inprogress", "activeOwner": "Ada"}]})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "note", {"summary": "Reviewer remark", "body": "- looks good"}, "Grace")
        self.assertEqual((card["log"][-1]["by"], card["log"][-1]["body"], card["activeOwner"]),
                         ("Grace", "- looks good", "Ada"))

    def test_subtask_notes_follow_the_holder_not_the_column(self):
        held = {"id": "s-1", "text": "Recheck", "done": True, "children": [], "delegation": {
            "state": "completed", "owner": "Ada", "claimedAt": "2026-10-10T00:00:00Z", "result": "Verified"}}
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "column": "done", "subtasks": [held]}]})
        card = doc["cards"][0]
        self.assertEqual(core.publish_subtask_note(doc, card, "s-1", "Recheck passes", "", "Ada")["subtaskId"], "s-1")
        for actor in ("Main", "Grace"):  # A Done card has no owner, so only the holder notes its subtask.
            with self.assertRaises(core.WorkflowError) as caught:
                core.publish_subtask_note(doc, card, "s-1", "Not mine", "", actor)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "owned"))
            self.assertIn("Ada", str(caught.exception))
        self.assertEqual([entry["by"] for entry in card["log"]], ["Ada"])

    def test_inbox_hides_findings_posted_before_a_subtask_acceptance(self):
        accepted = {"state": "completed", "owner": "Ada", "claimedAt": "2026-10-10T10:00:00Z", "result": "Verified",
                    "review": {"state": "accepted", "by": "Main", "at": "2026-10-10T12:00:00Z"}}
        claimed = {"state": "claimed", "owner": "Grace", "claimedAt": "2026-10-10T10:00:00Z", "result": ""}
        log = [{"id": f"{number:032x}", "at": "2026-10-10T12:00:00Z", "by": by, "summary": summary, "body": "",
                **({"subtaskId": sid} if sid else {}), **({"kind": "generated"} if by == "Main" else {})}
               for number, by, summary, sid in ((1, "Grace", "Note 1", None),
                                                (2, "Grace", "Note 2", "s-2"),
                                                (3, "Ada", "Note 3", "s-1"),
                                                (4, "Main", "accepted s-1", "s-1"),
                                                (5, "Ada", "Note 5", "s-1"))]  # Same second, after acceptance.
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "column": "inprogress", "activeOwner": "Main",
                                             "log": log, "subtasks": [
                                                 {"id": "s-1", "text": "Done", "done": True, "delegation": accepted},
                                                 {"id": "s-2", "text": "Open", "done": False, "delegation": claimed}]}]})
        findings = [row["note"]["summary"] for row in core.inbox(doc, "Main") if row["kind"] == "finding"]
        self.assertEqual(findings, ["Note 1", "Note 2", "Note 5"])  # Note 5 is a recheck after acceptance.

    def test_inbox_keeps_claims_on_a_blocked_card_and_findings_wait_for_resume(self):
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "title": "T", "column": "task"}]})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "start", {}, "Main")
        core.subtask_action(doc, card, "add", {"texts": ["Worker step"], "delegated": True}, "Main")
        core.subtask_action(doc, card, "claim", {"ids": ["s-1"]}, "W")
        core.workflow_action(doc, card, "block", {"reason": "Waiting on W", "until": "W reports"}, "Main")

        def rows(kind):
            return [row for row in core.inbox(doc, "Main") if row["kind"] == kind]

        self.assertEqual([(row["subtaskId"], row["owner"]) for row in rows("claim")], [("s-1", "W")])
        core.workflow_action(doc, card, "note", {"summary": "W's finding", "body": ""}, "W")
        self.assertEqual(rows("finding"), [])
        core.workflow_action(doc, card, "resume", {"note": "W reported"}, "Main")
        self.assertEqual([row["note"]["by"] for row in rows("finding")], ["W"])

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
