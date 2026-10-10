# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Portable board ZIPs: faithful round trips, hostile input refused without residue."""
from __future__ import annotations

import json
import os
import stat
import struct
import tracemalloc
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from tests.support import last_json, make_env, run, scratch
from workboard import bundles, core


class Bundles(unittest.TestCase):
    def setUp(self):
        self.base = self.enterContext(scratch("wb-bundles-"))
        self.enterContext(mock.patch.dict(os.environ, make_env(self.base / "home"), clear=True))
        self.project = self.folder("source-project")
        cards = [{"id": f"card-{number}", "num": number, "title": f"Card {number} Δ",
                  "column": "inprogress" if number == 1 else "task", "activeOwner": "Main" if number == 1 else None,
                  "notes": "## Acceptance criteria\nPreserve the content.",
                  "subtasks": [{"id": "s-1", "text": "Keep this tree", "done": False, "children": [],
                                "extension": {"untouched": [1, 2, "Δ"]}}],
                  "extension": {"literal": "$(never) & ../quoted", "nested": {"z": 1, "a": 2}}}
                 for number in (1, 2)]
        self.path = core.create_board("source", self.project, core.normalize_doc(
            {"name": "source", "rev": 3, "cards": cards, "extension": {"untouched": ["Δ", {"value": 42}]}}))
        self.live_bytes = b"live attachment\x00\xff\r\n"
        self.other_bytes = b"second attachment"
        live = core.attachment_store(self.path, '../report "café".bin', self.live_bytes, "Main")
        other = core.attachment_store(self.path, "other.bin", self.other_bytes, "Main")
        with core.board_transaction(self.path) as doc:
            doc["cards"][0]["attachments"].append({**live, "extension": {"keep": True}})
            doc["cards"][1]["attachments"].append(other)
            core.save(self.path, doc, by="Main")
        self.live_id, self.other_id = live["id"], other["id"]
        self.archive_bytes = b"archived evidence only"
        archived = core.attachment_store(self.path, "archive.bin", self.archive_bytes, "Main")
        self.archive_id = archived["id"]
        with core.board_transaction(self.path):
            self.archive = core.archive_removed_cards(
                self.path, [core.normalize_card({"id": "gone", "num": 9, "title": "Archived",
                                                 "attachments": [archived], "extension": "kept"})], "removed")
        self.serial = 0

    def folder(self, *parts):
        path = self.base.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output(self):
        self.serial += 1
        return self.base / f"bundle-{self.serial}.zip"

    def export(self, **kwargs):
        output = self.output()
        return output, core.export_bundle(self.path, output, **kwargs)

    def members(self, source):
        with zipfile.ZipFile(source) as archive:
            return {info.filename: archive.read(info) for info in archive.infolist()}

    def build(self, entries, compression=zipfile.ZIP_DEFLATED):
        """Write (name or ZipInfo, bytes) pairs, duplicates included, as a ZIP."""
        output = self.output()
        with warnings.catch_warnings(), zipfile.ZipFile(output, "w", compression) as archive:
            warnings.simplefilter("ignore")
            for name, data in entries:
                archive.writestr(name, data)
        return output

    def residue(self):
        return (core.registry_path().read_bytes(), sorted(path.name for path in core.boards_dir().iterdir()))

    def rejected(self, source, status=None):
        before, project = self.residue(), self.folder("rejected-project")
        for apply in (False, True):
            with self.assertRaises(core.WorkflowError) as caught:
                core.import_bundle(source, name="rejected", project=project, apply=apply)
            self.assertEqual(caught.exception.code, "invalid", str(caught.exception))
            if status is not None:
                self.assertEqual(caught.exception.status, status, str(caught.exception))
        self.assertEqual(self.residue(), before)
        self.assertEqual(list(project.iterdir()), [])

    def test_round_trip_preserves_cards_extensions_attachments_and_archives(self):
        before, archive_before = self.path.read_bytes(), self.archive.read_bytes()
        output, exported = self.export()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((exported["rev"], exported["cards"], exported["attachments"], exported["archives"],
                          exported["backups"]), (json.loads(before)["rev"], 2, 3, 1, 0))
        self.assertEqual(Path(exported["out"]), output)
        source = core.load(self.path)
        backslash = self.output()  # Windows PowerShell 5.1 Compress-Archive writes `\` separators.
        backslash.write_bytes(output.read_bytes().replace(b"attachments/", b"attachments\\"))
        self.assertNotEqual(backslash.read_bytes(), output.read_bytes())
        variants = {"copy": output, "stored": self.build(self.members(output).items(), zipfile.ZIP_STORED),
                    "deflated": self.build(self.members(output).items()), "backslash": backslash}
        for name, bundle in variants.items():
            with self.subTest(name=name):
                project = self.folder(f"{name}-project")
                registry = core.registry_path().read_bytes()
                preview = core.import_bundle(bundle, name=name, project=project)
                self.assertEqual((preview["applied"], preview["sourceName"], preview["cards"]), (False, "source", 2))
                self.assertEqual(core.registry_path().read_bytes(), registry)
                self.assertNotIn(name, [path.name for path in core.boards_dir().iterdir()])
                imported = core.import_bundle(bundle, name=name, project=project, apply=True)
                target = Path(imported["board"])
                self.assertEqual(target, core.board_file(name))
                restored = core.load(target)
                self.assertEqual((restored["name"], restored["rev"], imported["rev"]),
                                 (name, source["rev"] + 1, source["rev"] + 1))
                self.assertEqual(restored["extension"], source["extension"])
                for previous, current in zip(source["cards"], restored["cards"], strict=True):
                    for key in ("id", "num", "title", "column", "activeOwner", "notes", "subtasks",
                                "attachments", "extension"):
                        self.assertEqual(current[key], previous[key], key)
                blobs = target.parent / "attachments"
                self.assertEqual((blobs / self.live_id).read_bytes(), self.live_bytes)
                self.assertEqual((blobs / self.other_id).read_bytes(), self.other_bytes)
                self.assertEqual((blobs / self.archive_id).read_bytes(), self.archive_bytes)
                self.assertEqual((target.parent / "archive" / self.archive.name).read_bytes(), archive_before)
                self.assertEqual(core.registry_load()["boards"][name]["project"], str(project))
                self.assertEqual(list(project.iterdir()), [])  # Nothing is written into the project.

    def test_scope_flags_select_archives_and_backups(self):
        backups = {path.name: path.read_bytes() for _, path in core.list_backups(self.path)}
        output, exported = self.export(include_archives=False, include_backups=True)
        self.assertEqual((exported["archives"], exported["backups"], exported["attachments"]), (0, len(backups), 2))
        names = set(self.members(output))
        self.assertNotIn(f"attachments/{self.archive_id}", names)
        self.assertFalse(any(name.startswith("archive/") for name in names))
        target = Path(core.import_bundle(output, name="copy", project=self.folder("copy"), apply=True)["board"])
        for name, data in backups.items():
            self.assertEqual((target.parent / ".backups" / name).read_bytes(), data)
        self.assertFalse((target.parent / "archive").exists())

    def test_export_never_overwrites_or_targets_managed_storage(self):
        target = self.output()
        target.write_bytes(b"keep me")
        with self.assertRaises(core.WorkflowError):
            core.export_bundle(self.path, target)
        self.assertEqual(target.read_bytes(), b"keep me")
        with self.assertRaises(core.WorkflowError) as caught:
            core.export_bundle(self.path, self.path.parent / "bundle.zip")
        self.assertEqual(caught.exception.code, "scope")
        stale = self.output()
        with self.assertRaises(core.RevisionConflict):
            core.export_bundle(self.path, stale, expected_rev=core.load(self.path)["rev"] - 1)
        self.assertFalse(stale.exists())
        raced, real_link = self.output(), os.link

        def competing_link(source, destination, *args, **kwargs):
            Path(destination).write_bytes(b"winner")
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(bundles.os, "link", side_effect=competing_link):
            with self.assertRaises(FileExistsError):
                core.export_bundle(self.path, raced)
        self.assertEqual(raced.read_bytes(), b"winner")
        self.assertEqual(list(self.base.glob(".bundle-*.tmp")), [])

    def test_export_without_hard_links_copies_into_a_new_file(self):
        output = self.output()
        with mock.patch.object(bundles.os, "link", side_effect=OSError("no hard links")):
            exported = core.export_bundle(self.path, output)
        self.assertEqual(exported["bytes"], output.stat().st_size)
        self.assertTrue(core.import_bundle(output, name="copy", project=self.folder("copy"), apply=True)["applied"])
        self.assertEqual(list(self.base.glob(".bundle-*.tmp")), [])

    def test_corrupt_source_attachment_never_publishes_an_export(self):
        (self.path.parent / "attachments" / self.live_id).write_bytes(b"corrupt")
        output = self.output()
        with self.assertRaises(core.WorkflowError):
            core.export_bundle(self.path, output)
        self.assertFalse(output.exists())

    def test_name_and_project_conflicts_are_refused_before_any_write(self):
        output, _ = self.export()
        before = self.residue()
        for name, project in (("source", self.folder("fresh")), ("fresh", self.project)):
            for apply in (False, True):
                with self.subTest(name=name, apply=apply), self.assertRaises(core.WorkflowError) as caught:
                    core.import_bundle(output, name=name, project=project, apply=apply)
                self.assertEqual((caught.exception.status, caught.exception.code), (409, "state"))
        self.assertEqual(self.residue(), before)

    def test_failed_registration_leaves_nothing_but_a_committed_one_is_kept(self):
        output, _ = self.export()
        before, real_write = self.residue(), core._atomic_write_json

        def fail(path, value):
            raise OSError("registry write failed")

        def committed_then_fail(path, value):
            real_write(path, value)
            raise OSError("reported after commit")

        with mock.patch.object(core, "_atomic_write_json", side_effect=fail), self.assertRaises(OSError):
            core.import_bundle(output, name="copy", project=self.folder("copy"), apply=True)
        self.assertEqual(self.residue(), before)
        with mock.patch.object(core, "_atomic_write_json", side_effect=committed_then_fail), \
                self.assertRaises(OSError):
            core.import_bundle(output, name="copy", project=self.folder("copy"), apply=True)
        target = core.board_file("copy")
        self.assertEqual(len(core.load(target)["cards"]), 2)
        self.assertEqual((target.parent / "attachments" / self.live_id).read_bytes(), self.live_bytes)

    def test_hostile_member_names_and_entry_types_are_refused(self):
        valid = list(self.members(self.export()[0]).items())
        board = dict(valid)["board.json"]
        for name in ("../escape", "archive/../board.json", "/board.json", "C:/board.json", "C:board.json",
                     "//server/share/board.json", "board.json:stream", "archive/CON.json", "archive/nul.txt.json",
                     "archive/com1.json", "archive/a.json.", "archive/a.json ", "archive/sub/a.json",
                     "attachments/" + "A" * 32, "attachments/x/" + "a" * 32, "archive/", "notes.txt",
                     "board.json\x00hidden"):
            with self.subTest(name=name):
                self.rejected(self.build([*valid, (name, b"[]")]))
        self.rejected(self.build([*valid, ("board.json", board)]))
        self.rejected(self.build([*valid, ("archive/x.json", b"[]"), ("archive/X.json", b"[]")]))
        for mode, attributes in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFDIR | 0o755, 0x10), (stat.S_IFREG, 0x10),
                                 (stat.S_IFREG, 0x400), (stat.S_IFIFO | 0o600, 0)):
            info = zipfile.ZipInfo("board.json")
            info.external_attr = (mode << 16) | attributes
            with self.subTest(mode=oct(mode), attributes=attributes):
                self.rejected(self.build([(name, data) for name, data in valid if name != "board.json"]
                                         + [(info, board)]))

    def test_missing_extra_and_swapped_attachment_bytes_are_refused(self):
        valid = self.members(self.export(include_archives=False)[0])
        live, other = f"attachments/{self.live_id}", f"attachments/{self.other_id}"
        swapped = {**valid, live: valid[other], other: valid[live]}
        self.rejected(self.build(swapped.items()))
        self.rejected(self.build([(name, data) for name, data in valid.items() if name != live]))
        self.rejected(self.build([*valid.items(), ("attachments/" + "e" * 32, b"orphan")]))
        corrupt = {**valid, live: valid[live][:-1] + b"?"}
        self.rejected(self.build(corrupt.items()))

    def test_invalid_marker_and_json_are_refused(self):
        valid = self.members(self.export()[0])
        board = valid["board.json"]
        self.rejected(self.build([(name, data) for name, data in valid.items() if name != bundles.MARKER]))
        self.rejected(self.build({**valid, bundles.MARKER: b'{"format":"workboard-bundle","version":2}'}.items()))
        for raw in (b"{" + b'"deep":' + b"[" * 80 + b"0" + b"]" * 80 + b"," + board[1:],
                    b'{"number":NaN,' + board[1:], b'{"number":1e999,' + board[1:],
                    b'{"text":"\\ud800",' + board[1:], board.decode().encode("utf-16"),
                    json.dumps({**json.loads(board), "schemaVersion": 999}).encode()):
            with self.subTest(raw=raw[:30]):
                self.rejected(self.build({**valid, "board.json": raw}.items()))

    def test_size_bombs_are_refused_without_expanding_them(self):
        valid = self.members(self.export()[0])
        live = f"attachments/{self.live_id}"
        honest = self.output()
        with zipfile.ZipFile(honest, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in valid.items():
                if name != live:
                    archive.writestr(name, data)
            with archive.open(live, "w") as stream:
                for _ in range(64):
                    stream.write(bytes(1024 * 1024))
        lying = self.output()
        lying.write_bytes(self.shrunk(honest.read_bytes(), live, len(self.live_bytes)))
        nodes = self.build({**valid, "board.json": b'{"cards":[' + b"{}," * 5_000_000 + b"{}]}"}.items())
        for source, status in ((honest, 413), (lying, None), (nodes, 413)):
            with self.subTest(source=source.name):
                self.assertLess(source.stat().st_size, 256 * 1024)
                tracemalloc.start()
                try:
                    self.rejected(source, status)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 96 * 1024 * 1024)
        source = self.export()[0]
        for limit, value in (("MAX_MEMBERS", len(valid) - 1), ("MAX_TOTAL_BYTES", 1024)):
            with self.subTest(limit=limit), mock.patch.object(bundles, limit, value):
                self.rejected(source, 413)

    @staticmethod
    def shrunk(data, name, size):
        """Make the central directory understate a member's uncompressed size."""
        data, raw = bytearray(data), name.encode()
        at = data.index(b"PK\x01\x02")
        while data[at + 46:at + 46 + len(raw)] != raw:
            at = data.index(b"PK\x01\x02", at + 4)
        struct.pack_into("<I", data, at + 24, size)
        return bytes(data)


class BundleCLI(unittest.TestCase):
    """`export` and `import` through the public CLI."""

    def setUp(self):
        self.base = self.enterContext(scratch("wb-bundle-cli-"))
        self.home, self.project = self.base / "home", self.base / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.env = make_env(self.home)
        self.enterContext(mock.patch.dict(core.os.environ, self.env, clear=True))
        self.json(["init", "workflow"])
        self.path = core.board_file("workflow")
        self.ref = self.json(["add", "--title", "Integration"])["id"]
        self.json(["start", self.ref])

    def call(self, args, actor="Main"):
        return run([*args, "--actor", actor], cwd=self.project, env=self.env)

    def json(self, args, actor="Main"):
        result = self.call([*args, "--json"], actor)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        return last_json(result)

    def reject(self, args, code, actor="Main"):
        before = self.path.read_bytes()
        result = self.call([*args, "--json"], actor)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        error = last_json(result)
        self.assertEqual(error["code"], code, result.stdout + result.stderr)
        self.assertEqual(error["rev"], json.loads(before)["rev"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_portable_bundle_cli_preview_apply_and_attachment_round_trip(self):
        evidence = self.project / "evidence.bin"
        evidence.write_bytes(b"Verified portable evidence\x00\r\n")
        attached = self.json(["attachment", self.ref, "add", "--file", evidence])["item"]
        source = self.path.read_bytes()
        bundle = self.base / "portable.zip"
        exported = self.json(["export", "--out", bundle, "--no-archives"])
        self.assertEqual((exported["rev"], exported["cards"], exported["attachments"]),
                         (json.loads(source)["rev"], 1, 1))
        plain = self.call(["export", "--out", self.base / "plain.zip"])
        self.assertEqual((plain.returncode, len(plain.stdout.splitlines())), (0, 1), plain.stdout + plain.stderr)
        exported_bytes = bundle.read_bytes()
        self.reject(["export", "--out", bundle], "invalid")
        self.assertEqual(bundle.read_bytes(), exported_bytes)
        self.assertEqual(self.path.read_bytes(), source)
        destination = self.base / "imported-project"
        destination.mkdir()
        registry = core.registry_path().read_bytes()
        preview = self.call(["import", bundle, "--name", "imported-workflow", "--dir", destination])
        self.assertEqual((preview.returncode, len(preview.stdout.splitlines())), (0, 1),
                         preview.stdout + preview.stderr)
        self.assertEqual(core.registry_path().read_bytes(), registry)
        imported = self.json(["import", bundle, "--name", "imported-workflow", "--dir", destination, "--apply"])
        self.assertEqual((imported["applied"], imported["name"], imported["rev"]),
                         (True, "imported-workflow", exported["rev"] + 1))
        self.assertEqual(str(core.board_file("imported-workflow")), imported["board"])
        self.assertEqual(list(destination.iterdir()), [])
        restored = self.json(["context", self.ref, "--board", "imported-workflow"])["card"]
        self.assertEqual(restored["attachments"][0]["id"], attached["id"])
        recovered = self.base / "recovered.bin"
        self.json(["attachment", self.ref, "get", attached["id"], "--out", recovered,
                   "--board", "imported-workflow"])
        self.assertEqual(recovered.read_bytes(), evidence.read_bytes())
        self.reject(["import", bundle, "--name", "imported-workflow", "--dir", self.base, "--apply"], "state")
        self.assertEqual(self.path.read_bytes(), source)


if __name__ == "__main__":
    unittest.main()
