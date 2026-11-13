# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""SQLite board store: migration from board.json, crash recovery, concurrent writers, load scopes,
restore, and board-name lookup. Every test runs in-process against its own scratch home."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_env, scratch

from workboard import cli, core

AT = "2026-01-02T03:04:05Z"


def card(num, cid, column="task", **extra):
    return {"num": num, "id": cid, "title": cid.title(), "column": column, "createdAt": AT, "updatedAt": AT,
            **({"doneAt": AT} if column == "done" else {}), **extra}


LEGACY = {
    "schemaVersion": 3, "name": "legacy", "rev": 41, "nextNum": 5, "savedAt": AT, "savedBy": "old",
    "x-extension": {"kept": [1, 2]},
    "columns": [dict(c) for c in core.DEFAULT_COLUMNS],
    "cards": [
        card(1, "alpha-1", "done"),
        card(2, "beta-2", dependsOn=["alpha-1"], log=[{"id": "a" * 32, "at": AT, "by": "x", "summary": "s",
                                                        "body": ""}]),
        card(3, "gamma-3", "inprogress", activeOwner="worker", claimedAt=AT,
             subtasks=[{"id": "s-1", "text": "nested", "done": False, "children": [
                 {"id": "s-2", "text": "child", "done": True, "doneAt": AT}]}]),
    ],
}
SWEPT = card(4, "delta-4", "done")
PURGED = card(5, "trash-5", "trash")  # columns-core removed its column: the migration leaves it out.


class Store(unittest.TestCase):
    def setUp(self):
        self.base = self.enterContext(scratch("wb-store-"))
        self.env = make_env(self.base / "home")
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))

    def legacy_board(self) -> tuple[Path, bytes, list]:
        path = core.boards_dir() / "legacy" / "board.json"
        path.parent.mkdir(parents=True)
        data = json.dumps(LEGACY, indent=2).encode("utf-8")
        path.write_bytes(data)
        (path.parent / core.ARCHIVE_DIR).mkdir()
        (path.parent / core.ARCHIVE_DIR / "board-2026-01.json").write_text(
            json.dumps({"cards": [LEGACY["cards"][0], SWEPT, PURGED]}), encoding="utf-8")
        return path, data, core.normalize_doc(json.loads(data))["cards"] + [core.normalize_card(SWEPT)]

    def board(self, name, cards, next_num=None) -> Path:
        project = self.base / f"project-{len(list(self.base.glob('project-*')))}"
        project.mkdir()
        return core.create_board(name, project, {
            "name": name, "rev": 0, "nextNum": next_num or len(cards) + 1,
            "columns": [dict(c) for c in core.DEFAULT_COLUMNS], "cards": cards})

    def nums(self, doc) -> list:
        return [c["num"] for c in doc["cards"]]

    def test_migration_round_trip_keeps_every_card_and_key(self):
        path, data, expected = self.legacy_board()
        doc = core.load(path)
        self.assertEqual(doc["cards"], expected)
        self.assertEqual((doc["rev"], doc["nextNum"], doc["schemaVersion"]), (41, 5, 4))
        self.assertEqual(doc["x-extension"], {"kept": [1, 2]})
        self.assertEqual((path.parent / core.BACKUP_DIR / "board-41.json").read_bytes(), data)
        self.assertEqual(core.load(path), doc)

    def test_legacy_readers_see_only_a_schema_5_stub(self):
        path, _, _ = self.legacy_board()
        core.load(path)
        stub = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stub, {"schemaVersion": 5, "storage": "board.db", "name": "legacy"})
        with self.assertRaisesRegex(core.WorkflowError, "unsupported board schemaVersion 5"):
            core.validate_schema(stub)  # What 0.3.x runs on every read.
        path.write_text(json.dumps(LEGACY), encoding="utf-8")  # A hand-restored board.json: never merged.
        with self.assertRaises(core.WorkflowError) as caught:
            core.load(path)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "state"))

    def test_crash_before_the_stub_leaves_board_json_in_charge(self):
        path, data, expected = self.legacy_board()
        real = core._atomic_write

        def crash_on_stub(target, payload):
            if Path(target).name == "board.json":
                raise OSError("simulated crash")
            return real(target, payload)

        with mock.patch.object(core, "_atomic_write", crash_on_stub), self.assertRaises(OSError):
            core.load(path)
        self.assertEqual(path.read_bytes(), data)
        self.assertTrue(path.with_name("board.db.tmp").exists())
        self.assertFalse(path.with_name("board.db").exists())
        self.assertEqual(core.load(path)["cards"], expected)

    def test_crash_after_the_stub_finishes_on_next_touch(self):
        path, _, expected = self.legacy_board()
        real = core.atomic_replace

        def crash_on_rename(src, target, *args, **kwargs):
            if Path(target).name == "board.db":
                raise OSError("simulated crash")
            return real(src, target, *args, **kwargs)

        with mock.patch.object(core, "atomic_replace", crash_on_rename), self.assertRaises(OSError):
            core.load(path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schemaVersion"], 5)
        self.assertFalse(path.with_name("board.db").exists())
        self.assertEqual(core.load(path)["cards"], expected)
        self.assertFalse(path.with_name("board.db.tmp").exists())

    def test_a_board_json_read_just_before_another_migration_commits_is_rechecked(self):
        path, data, expected = self.legacy_board()
        core.load(path)  # Another process migrated the board...
        real_read, reads = core._read_shared, []

        def stale_first(target, *args, **kwargs):  # ...just after this one read the old board.json.
            reads.append(target)
            return data if len(reads) == 1 else real_read(target, *args, **kwargs)

        with mock.patch.object(core, "_read_shared", stale_first):
            self.assertEqual(core.load(path)["cards"], expected)
        opens = []

        def missing_once(target, *args, **kwargs):  # ReplaceFileW's moment without a board.json
            opens.append(target)
            if len(opens) == 1:
                raise FileNotFoundError(2, "replacing", str(target))
            return open(target, *args, **kwargs)  # not open_shared: its Windows opener exists only on Windows

        with mock.patch.object(core, "_IS_WINDOWS", True), mock.patch.object(core, "open_shared", missing_once):
            self.assertEqual(core.load(path)["cards"], expected)
        path.rename(path.with_name("aside.json"))
        started = time.monotonic()
        with self.assertRaises(FileNotFoundError):  # A board.json that stays missing fails at once.
            core.load(path)
        self.assertLess(time.monotonic() - started, 2)

    def test_eight_processes_migrating_and_writing_one_card_all_succeed(self):
        path, _, _ = self.legacy_board()  # The first touches race to migrate it, too.
        script = ("import sys\nfrom workboard import core\n"
                  "for i in range(10):\n"
                  "    with core.board_transaction(sys.argv[1], None, '2', scope='card') as doc:\n"
                  "        core.append_note(core.resolve_ref(doc, '2'), f'note {i}', '', sys.argv[2])\n"
                  "        core.save(sys.argv[1], doc, by=sys.argv[2])\n")
        workers = [subprocess.Popen([sys.executable, "-c", script, str(path), f"w{n}"], env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(8)]
        for worker in workers:
            _, err = worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0, err)
        doc = core.load(path)
        self.assertEqual(len(core.resolve_ref(doc, "2")["log"]), 1 + 80)
        self.assertEqual(doc["rev"], LEGACY["rev"] + 80)

    def test_scoped_loads_hold_the_card_its_dependencies_and_dependents(self):
        path = self.board("scoped", [
            card(1, "alpha-1", "done"), card(2, "beta-2", dependsOn=["alpha-1"]),
            card(3, "gamma-3", dependsOn=["beta-2"]), card(4, "delta-4", "done"), card(5, "eps-5", "backlog"),
            card(6, "zeta-a", "done"), card(7, "zeta-b", "done")])
        self.assertEqual(self.nums(core.load(path, ["2"], "card")), [1, 2, 3])
        self.assertEqual(self.nums(core.load(path, ["#3"], "card")), [2, 3])
        self.assertEqual(self.nums(core.load(path, ["beta"], "card")), [1, 2, 3])
        self.assertEqual(self.nums(core.load(path, [], "open")), [1, 2, 3, 5])
        self.assertEqual(self.nums(core.load(path, ["4"], "open")), [1, 2, 3, 4, 5])
        with self.assertRaisesRegex(core.RefError, "ambiguous"):
            core.resolve_ref(core.load(path, ["zeta"], "card"), "zeta")
        before = {c["id"]: c["changedRev"] for c in core.load(path)["cards"]}
        with core.board_transaction(path, before["gamma-3"], "3", scope="card") as doc:
            core.append_note(core.resolve_ref(doc, "3"), "scoped", "", "tester")
            rev = core.save(path, doc)
        after = core.load(path)
        self.assertEqual(self.nums(after), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual({c["id"]: c["changedRev"] for c in after["cards"]}, {**before, "gamma-3": rev})
        with self.assertRaises(RuntimeError):  # Saved outside a transaction it would delete 1 and 4-7.
            core.save(path, core.load(path, ["3"], "card"))
        self.assertEqual(self.nums(core.load(path)), [1, 2, 3, 4, 5, 6, 7])

    def test_whole_board_reorder_and_delete(self):
        path = self.board("order", [card(n, f"c-{n}") for n in (1, 2, 3)])
        with core.board_transaction(path) as doc:
            doc["cards"] = doc["cards"][::-1][:2]
            core.save(path, doc)
        self.assertEqual(self.nums(core.load(path)), [3, 2])
        self.assertEqual(core.load(path)["nextNum"], 4)

    def test_restore_returns_cards_to_an_earlier_revision(self):
        path = self.board("restore", [card(1, "keep-1")])
        for summary in ("first", "second"):
            with core.board_transaction(path) as doc:
                core.append_note(doc["cards"][0], summary, "", "tester")
                core.save(path, doc)
            if summary == "first":
                first = doc["rev"]
        with core.board_transaction(path) as doc:
            core.create_card(doc, "tester", title="Later")
            later = core.save(path, doc)
        self.assertLessEqual(core.list_versions(path)["oldest"], first)
        result = core.restore(path, first)
        doc = core.load(path)
        self.assertEqual((result["rev"], result["removed"]), (later + 1, [2]))
        self.assertEqual(self.nums(doc), [1])
        self.assertEqual([entry["summary"] for entry in doc["cards"][0]["log"]], ["first"])
        self.assertEqual(doc["nextNum"], 3)
        core.restore(path, later)  # A restore is a revision too, so it can be undone.
        doc = core.load(path)
        self.assertEqual(self.nums(doc), [1, 2])
        self.assertEqual(len(doc["cards"][0]["log"]), 2)
        with self.assertRaises(core.WorkflowError):
            core.restore(path, doc["rev"])

    def test_card_versions_keep_a_byte_budget_and_the_oldest_offered_revision_restores(self):
        path = self.board("versions", [card(1, "big-1", notes="n" * 50_000), card(2, "small-2")])
        notes = {core.board_rev(path): 0}  # rev -> notes on card 1
        with mock.patch.object(core, "VERSION_BYTES", 200_000):
            for i in range(10):
                with core.board_transaction(path, None, "1", scope="card") as doc:
                    core.append_note(core.resolve_ref(doc, "1"), f"note {i}", "", "tester")
                    notes[core.save(path, doc)] = i + 1
        with core.snapshot(path) as con:
            kept = con.execute("SELECT sum(length(body)) FROM card_versions").fetchone()[0]
        self.assertLessEqual(kept, 200_000)
        oldest = core.list_versions(path)["oldest"]
        self.assertGreater(oldest, min(notes))
        with self.assertRaises(core.WorkflowError):
            core.restore(path, oldest - 1)
        core.restore(path, oldest)
        self.assertEqual(len(core.resolve_ref(core.load(path), "1")["log"]), notes[oldest])
        db = path.with_name(core.DB_NAME)
        db.rename(db.with_name("aside.db"))  # Closed connections leave no mapping that pins the file.
        db.with_name("aside.db").rename(db)

    def test_recover_apply_undoes_only_the_revision_it_read(self):
        path = self.board("undo", [card(1, "one-1")])
        for title in ("first", "second"):
            with core.board_transaction(path) as doc:
                doc["cards"][0]["title"] = title
                core.save(path, doc)
        out = io.StringIO()
        with (mock.patch.object(core, "board_rev", return_value=core.board_rev(path) - 1),  # read before "second"
              contextlib.redirect_stdout(out), self.assertRaises(SystemExit)):
            cli.main(["recover", "--apply", "--json", "--board", "undo", "--actor", "tester"])
        self.assertEqual(json.loads(out.getvalue())["code"], "stale")
        self.assertEqual(core.load(path)["cards"][0]["title"], "second")

    def test_board_names_match_ignoring_case_or_by_folder(self):
        self.board("DSX_V3_SourceCode", [])
        self.board("other", [])
        for given in ("DSX_V3_SourceCode", "dsx_v3_sourcecode", "dsx-v3-sourcecode", "DSX V3 SourceCode"):
            self.assertEqual(core.resolve_board(given)[0], "DSX_V3_SourceCode", given)
        with self.assertRaises(core.WorkflowError) as caught:
            self.board("dsx_v3_SOURCECODE", [])
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "state"))
        with self.assertRaisesRegex(core.WorkflowError, r"DSX_V3_SourceCode, other.*--board NAME.*\"board\""):
            core.resolve_board("nope")
        outside = self.base / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(core.WorkflowError, "outside every linked project"):
            core.resolve_board(str(outside))
        registry = core.registry_load()
        registry["boards"]["Other"] = {"dir": "other-2", "project": str(self.base / "x")}
        core._atomic_write_json(core.registry_path(), registry)
        with self.assertRaisesRegex(core.WorkflowError, "several boards: Other, other"):
            core.resolve_board("OTHER")


if __name__ == "__main__":
    unittest.main()
