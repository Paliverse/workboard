"""Notes timeline (schema v3): legacy note migration, summary derivation and entry validation.

Pure in-process checks on ``workboard.core``; nothing here touches the filesystem.
"""
from __future__ import annotations

import hashlib
import re
import unittest
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
        for summary, body in (("   ", ""), (None, ""), ("two\nlines", ""), ("carriage\rreturn", ""),
                              ("s" * 161, ""), ("ok", "b" * (core.NOTE_BODY_MAX + 1)), ("ok", 5)):
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

    def test_note_action_ignores_ownership(self):
        doc = core.normalize_doc({"cards": [{"id": "c", "num": 1, "column": "inprogress", "activeOwner": "Ada"}]})
        card = doc["cards"][0]
        core.workflow_action(doc, card, "note", {"summary": "Reviewer remark", "body": "- looks good"}, "Grace")
        self.assertEqual((card["log"][-1]["by"], card["log"][-1]["body"], card["activeOwner"]),
                         ("Grace", "- looks good", "Ada"))

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
