"""`workboard doctor`: installation checks plus read-only board data integrity.

Data-integrity cases are ported from the preview release checks. Every case runs
in a scratch home with a fake server, a fake service registry, and no
`workboard` on PATH, so doctor never talks to a real server or registration.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_env, scratch

from workboard import __version__, cli, doctor, install
from workboard import core as wb

BASE = Path()


def setUpModule():
    global BASE
    BASE = unittest.enterModuleContext(scratch("wb-doctor-"))
    (BASE / "cwd").mkdir()
    unittest.enterModuleContext(contextlib.chdir(BASE / "cwd"))
    unittest.enterModuleContext(mock.patch.dict(os.environ, make_env(BASE / "home"), clear=True))


class FakeServer:
    def __init__(self):
        self.info = None

    def server_info(self, timeout=1.0):
        return self.info

    def server_url(self, port=None):
        return f"http://127.0.0.1:{port}/"


class FakeRegistry(dict):
    def set(self, name, value):
        self[name] = value

    def delete(self, name):
        del self[name]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_board(root, name="source"):
    """A registered board for project root/name, overwritten with a fixed legacy-schema document."""
    project = root / name
    project.mkdir()
    board = wb.create_board(name, project)
    doc = wb.normalize_doc({"schemaVersion": 2, "name": name, "rev": 4, "nextNum": 2,
                            "columns": copy.deepcopy(wb.DEFAULT_COLUMNS),
                            "cards": [{"id": "one", "num": 1, "title": "Review input", "column": "task"}]})
    write_json(board, doc)
    return board, doc


def tree_state(root):
    """No-write oracle: names, file bytes, and modification times."""
    return {path.relative_to(root).as_posix(): (None if path.is_dir() else (path.read_bytes(), path.stat().st_mtime_ns))
            for path in root.rglob("*")}


def codes(report, field="blockers"):
    return {item["code"] for item in report[field]}


def attachment(board, doc, content=b"required evidence", identity="a" * 32):
    metadata = {"id": identity, "name": "input.txt", "mime": "text/plain", "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "createdAt": wb.now_iso(), "by": "fixture"}
    blob = board.parent / "attachments" / identity
    blob.parent.mkdir(exist_ok=True)
    blob.write_bytes(content)
    doc["cards"][0]["attachments"].append(metadata)
    write_json(board, doc)
    return metadata, blob


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.root = BASE / self.id().rsplit(".", 1)[-1]
        self.root.mkdir()
        self.home = self.root / "home"
        self.enterContext(mock.patch.dict(os.environ, make_env(self.home), clear=True))
        self.server = FakeServer()
        self.run_key = FakeRegistry()
        backend = install.WindowsService(registry=self.run_key)
        self.enterContext(mock.patch.object(install, "_server", lambda: self.server))
        self.enterContext(mock.patch.object(install, "_backend", lambda: backend))
        self.enterContext(mock.patch("shutil.which", return_value=None))
        self.board, self.doc = make_board(self.root)

    def inspect(self, board="source", **kwargs):
        return doctor.diagnose(board, **kwargs)

    def cli(self, *argv):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                cli.main([str(arg) for arg in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()


class DataIntegrityTest(DoctorCase):
    def test_healthy_board_passes_without_writing(self):
        before = tree_state(self.root)
        with contextlib.chdir(self.root / "source"):
            code, out = self.cli("doctor", "--json")
        report = json.loads(out)
        self.assertEqual((code, report["ok"], report["blockers"]), (0, True, []), report)
        self.assertEqual(report["boards"][0]["path"], str(self.board))
        self.assertEqual(report["boards"][0]["rev"], self.doc["rev"])
        self.assertEqual(codes(report, "warnings"), {"not-on-path", "skills-missing"})
        self.assertEqual(report["installation"]["channel"], "source")
        self.assertEqual(tree_state(self.root), before)

        code, out = self.cli("doctor", "--board", "source")
        self.assertEqual(code, 0)
        self.assertIn(str(self.board), out)
        unlinked = doctor.diagnose()
        self.assertEqual((unlinked["ok"], unlinked["boards"]), (True, []), "an unlinked folder has no board")
        self.assertIn("board-invalid", codes(self.inspect("missing")))
        with mock.patch.object(doctor, "MAX_FILES", 1):
            self.assertFalse(self.inspect()["ok"])
        self.assertEqual(tree_state(self.root), before)

    def test_corrupted_board_json_is_a_blocker(self):
        self.board.write_text('{"rev":1,"rev":2}', encoding="utf-8")
        code, out = self.cli("doctor", "--json", "--board", "source")
        report = json.loads(out)
        self.assertEqual((code, report["ok"]), (1, False))
        self.assertIn("board-invalid", codes(report))
        self.board.write_text("{truncated", encoding="utf-8")
        code, out = self.cli("doctor", "--board", "source")
        self.assertEqual(code, 1)
        self.assertIn("[board-invalid]", out)

        mutations = [
            lambda doc: doc.update(schemaVersion=4),
            lambda doc: doc.update(schemaVersion=True),
            lambda doc: doc.update(schemaVersion="2"),
            lambda doc: doc.update(rev=-1),
            lambda doc: doc.update(nextNum=1),
            lambda doc: doc["cards"].append(copy.deepcopy(doc["cards"][0])),
            lambda doc: doc["cards"].append({**doc["cards"][0], "id": "two"}),
            lambda doc: doc["columns"].append(copy.deepcopy(doc["columns"][0])),
            lambda doc: doc["cards"][0].update(comments=["silently-dropped-comment"]),
            lambda doc: doc["cards"][0].update(priority="unrecognized-but-not-discardable"),
            lambda doc: doc["cards"][0].update(activeOwner="must-not-disappear"),
        ]
        for index, mutation in enumerate(mutations):
            doc = copy.deepcopy(self.doc)
            mutation(doc)
            write_json(self.board, doc)
            with self.subTest(mutation=index):
                self.assertFalse(self.inspect()["ok"])
        write_json(self.board, self.doc)
        with mock.patch.object(doctor, "MAX_JSON_BYTES", 4):
            self.assertFalse(self.inspect()["ok"])
        shutil.rmtree(self.board.parent / wb.BACKUP_DIR)
        (self.board.parent / wb.BACKUP_DIR).write_text("not a storage directory", encoding="utf-8")
        self.assertFalse(self.inspect()["ok"])

    def test_missing_or_damaged_attachment_blob_is_a_blocker(self):
        metadata, blob = attachment(self.board, self.doc)
        write_json(self.board.parent / wb.BACKUP_DIR / "board-4.json", self.doc)
        self.assertTrue(self.inspect()["ok"])
        blob.unlink()
        code, out = self.cli("doctor", "--json", "--board", "source")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn("attachment-integrity", codes(report))
        blob.write_bytes(b"wrong bytes")
        self.assertIn("attachment-integrity", codes(self.inspect()))
        blob.write_bytes(b"required evidence")

        archive = self.board.parent / wb.ARCHIVE_DIR / "board-2026-09.json"
        archived = copy.deepcopy(self.doc["cards"])
        archived[0]["attachments"][0]["sha256"] = "0" * 64
        write_json(archive, {"cards": archived})
        self.assertIn("attachment-integrity", codes(self.inspect()))
        write_json(archive, {"cards": self.doc["cards"]})
        self.assertTrue(self.inspect()["ok"])

        self.doc["cards"][0]["attachments"][0]["size"] = wb.MAX_ATTACHMENT_BYTES + 1
        write_json(self.board, self.doc)
        self.assertFalse(self.inspect()["ok"])
        self.doc["cards"][0]["attachments"][0] = {**metadata, "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
        write_json(self.board, self.doc)
        with blob.open("wb") as stream:
            stream.write(b"x")
            stream.truncate(wb.MAX_ATTACHMENT_BYTES + 1)
        self.assertIn("attachment-integrity", codes(self.inspect()))

    def test_recovery_snapshots_are_validated(self):
        damaged = self.board.parent / wb.BACKUP_DIR / "board-4.json"
        damaged.parent.mkdir(exist_ok=True)
        damaged.write_text("{broken recovery JSON", encoding="utf-8")
        report = self.inspect()
        self.assertTrue(any(item["code"] == "recovery-invalid" and item["path"] == str(damaged)
                            for item in report["blockers"]), report)
        write_json(damaged, {**self.doc, "rev": 3})
        self.assertIn("recovery-invalid", codes(self.inspect()), "filename revision must match")

    def test_attachment_metadata_is_validated_per_reference(self):
        attachment(self.board, self.doc, content=b"x")
        backup = self.board.parent / wb.BACKUP_DIR / "board-4.json"
        broken = [("size", True), ("size", 1.0), ("name", "\ninput.txt"),
                  ("mime", "text/plain\r\nInjected: value"), ("by", {}), ("createdAt", False)]
        for field in ("name", "mime", "by", "createdAt"):
            broken.append((field, None))
        for field, value in broken:
            recovery = copy.deepcopy(self.doc)
            if value is None:
                recovery["cards"][0]["attachments"][0].pop(field)
            else:
                recovery["cards"][0]["attachments"][0][field] = value
            write_json(backup, recovery)
            with self.subTest(field=field, value=value):
                report = self.inspect()
                self.assertTrue(any(item["code"] == "attachment-integrity" and str(backup) in item["path"]
                                    for item in report["blockers"]), report)
        recovery = copy.deepcopy(self.doc)
        recovery["cards"][0]["attachments"][0]["createdAt"] = "2026-09-11 05:00:00 +0000"
        write_json(backup, recovery)
        self.assertTrue(self.inspect()["ok"], "historical timestamp spellings stay valid")

    def test_legacy_schema_and_aliases_warn_but_conflicts_block(self):
        doc = self.doc
        doc.pop("schemaVersion")
        doc["extension"] = {"retain": [1, 2]}
        doc["columns"][0]["extension"] = "column"
        card = doc["cards"][0]
        card["extension"] = {"linkedCards": ["not-a-card-alias"]}
        card["subtasks"] = [{"id": "s1", "text": "work", "done": False, "children": [], "extension": 42}]
        card["linkedCards"] = card.pop("links")
        write_json(self.board, doc)
        report = self.inspect()
        self.assertTrue(report["ok"], report)
        self.assertLessEqual({"legacy-schema", "legacy-alias"}, codes(report, "warnings"))
        card["links"] = ["deliberately-different"]
        write_json(self.board, doc)
        self.assertIn("board-invalid", codes(self.inspect()))

    def test_legacy_stamped_notes_are_an_expected_migration_not_a_loss(self):
        doc = copy.deepcopy(self.doc)
        doc["schemaVersion"] = 2
        card = doc["cards"][0]
        card.pop("log", None)
        card["notes"] = ("## Acceptance criteria\n- parser handles stamps\n\n"
                         "[2026-09-01] Shipped the parser. Tests pass.\n[2026-09-02 ada] Fixed review nits")
        write_json(self.board, doc)
        report = self.inspect()
        self.assertEqual(report["blockers"], [], report)
        self.assertIn("legacy-schema", codes(report, "warnings"))

    def test_v3_timeline_entries_are_validated(self):
        entry = {"id": "a" * 32, "at": "2026-09-24T10:00:00Z", "by": "ada", "summary": "Shipped", "body": ""}
        healthy = [entry, {**entry, "id": "b" * 32, "at": "2026-09-01", "by": None, "body": "- `abc1234`"}]
        for log in ([entry, dict(entry)], [{**entry, "summary": "two\nlines"}], [{**entry, "summary": " padded"}]):
            doc = copy.deepcopy(self.doc)
            doc["cards"][0]["log"] = log
            write_json(self.board, doc)
            with self.subTest(log=log):
                self.assertEqual(codes(self.inspect()), {"log-invalid"})
        doc = copy.deepcopy(self.doc)
        doc["cards"][0]["log"] = healthy
        write_json(self.board, doc)
        report = self.inspect()
        self.assertTrue(report["ok"], report)
        self.assertNotIn("legacy-schema", codes(report, "warnings"))

    def test_links_and_path_traversal_are_rejected(self):
        external = self.root / "same-bytes.json"
        os.link(self.board, external)
        self.assertFalse(self.inspect()["ok"])
        external.unlink()
        attachment(self.board, self.doc)
        self.doc["cards"][0]["attachments"][0]["id"] = "../board.json"
        write_json(self.board, self.doc)
        self.assertIn("attachment-integrity", codes(self.inspect()))
        other, _ = make_board(self.root, "other")
        shutil.rmtree(other.parent)
        with contextlib.suppress(OSError):  # Directory symlinks need a privilege on some Windows hosts.
            other.parent.symlink_to(self.board.parent, target_is_directory=True)
            report = self.inspect("other")
            self.assertLessEqual({"board-invalid", "registered-board-invalid"}, codes(report))


class RegistryTest(DoctorCase):
    def registry(self, value):
        write_json(wb.registry_path(), value)

    def test_registry_shape_and_registered_boards(self):
        project = str(self.root / "source")
        for broken in ([], {"boards": {"source": str(self.board)}},  # The pre-release v1 shape.
                       {"version": 2, "boards": {"source": {"dir": "..", "project": project}}},
                       {"version": 2, "boards": {"source": {"dir": "source", "project": "source"}}}):
            self.registry(broken)
            with self.subTest(registry=broken):
                report = doctor.diagnose()
                self.assertEqual([(item["code"], item["category"]) for item in report["blockers"]],
                                 [("registry-invalid", "registry")])
        self.registry({"version": 2, "boards": {"gone": {"dir": "gone", "project": project}}})
        report = doctor.diagnose()
        self.assertTrue(report["ok"], report)
        self.assertLessEqual({"registered-board-missing", "unregistered-board-dir"}, codes(report, "warnings"))
        self.registry({"version": 2, "boards": {}})
        with mock.patch.object(wb, "registry_load", side_effect=PermissionError("registry inaccessible")):
            self.assertIn("registry-invalid", codes(doctor.diagnose()))

    def test_store_and_project_drift_are_warnings(self):
        project = self.root / "source"
        legacy = project / "board" / "board.json"
        write_json(legacy, self.doc)
        stray = wb.boards_dir() / "stray"
        stray.mkdir()
        report = self.inspect()
        self.assertTrue(report["ok"], report)
        found = {item["code"]: item for item in report["warnings"]}
        self.assertEqual(found["legacy-project-board"]["path"], str(legacy))
        self.assertEqual(found["unregistered-board-dir"]["path"], str(stray))
        self.assertNotIn("project-missing", found)
        shutil.rmtree(project)
        report = self.inspect()
        self.assertTrue(report["ok"], report)
        missing = next(item for item in report["warnings"] if item["code"] == "project-missing")
        self.assertEqual(missing["path"], str(project))
        self.assertIn("workboard link source", missing["message"])

    def test_all_validates_every_registered_board(self):
        other, future = make_board(self.root, "other")
        future["schemaVersion"] = 99
        write_json(other, future)
        self.assertTrue(self.inspect()["ok"], "without --all only the current board is validated")
        code, out = self.cli("doctor", "--all", "--json", "--board", "source")
        report = json.loads(out)
        self.assertEqual(code, 1)
        found = [item for item in report["boards"] if item.get("path") == str(other)]
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["ok"])
        self.assertEqual(len(report["boards"]), 2, "the current board is not validated twice")

    def test_attachment_blobs_are_checked_per_board_storage(self):
        attachment(self.board, self.doc)
        other, _ = make_board(self.root, "other")
        write_json(other, self.doc)  # Same ID/hash/size, but the bytes live only in the source board.
        report = self.inspect(all_registered=True)
        self.assertTrue(any(item["code"] == "attachment-integrity" and str(other) in item["path"]
                            for item in report["blockers"]), report)


class InstallationTest(DoctorCase):
    def test_legacy_viewer_files_warn_without_blocking(self):
        viewers = self.home / ".workboard" / "viewers.json"
        write_json(viewers, {})
        port_file = self.board.parent / ".viewer.port"
        write_json(port_file, {"pid": 1, "port": 7911})
        code, out = self.cli("doctor", "--json", "--board", "source")
        report = json.loads(out)
        self.assertEqual((code, report["ok"]), (0, True))
        legacy = {item["path"] for item in report["warnings"] if item["code"] == "legacy-viewer-file"}
        self.assertEqual(legacy, {str(viewers), str(port_file)})

    def test_skills_service_and_server_drift_are_warnings(self):
        install.skills_install()
        agents = Path(install.skill_targets()[0])
        agents.write_text("---\nname: workboard\n---\nold\n", encoding="utf-8")
        self.run_key[install.RUN_VALUE] = '"C:\\old\\workboardw.exe" serve --service'
        self.server.info = {"app": "workboard", "version": "0.0.1", "pid": 9, "port": 7999}
        report = self.inspect()
        self.assertTrue(report["ok"], report)
        self.assertLessEqual({"skill-stale", "service-stale", "server-version-mismatch"}, codes(report, "warnings"))
        self.assertNotIn("skills-missing", codes(report, "warnings"))
        self.assertEqual(report["installation"]["service"]["running"], True)

    def test_path_check_identifies_this_installation(self):
        def version(executable):
            output = json.dumps({"ok": True, "version": __version__, "channel": "source", "executable": executable})
            return subprocess.CompletedProcess([], 0, output + "\n", "")

        with mock.patch("shutil.which", return_value="/bin/workboard"):
            with mock.patch("subprocess.run", return_value=version(sys.executable)) as runner:
                report = self.inspect()
            self.assertEqual(runner.call_args.args[0], ["/bin/workboard", "version", "--json"])
            self.assertTrue(report["installation"]["path"]["sameInstall"])
            self.assertFalse(codes(report, "warnings") & {"not-on-path", "path-other-install", "path-unverified"})
            with mock.patch("subprocess.run", return_value=version(str(self.root / "elsewhere" / "python"))):
                self.assertIn("path-other-install", codes(self.inspect(), "warnings"))
            with mock.patch("subprocess.run", side_effect=OSError("not executable")):
                self.assertIn("path-unverified", codes(self.inspect(), "warnings"))

    def test_codex_sandbox_that_blocks_the_store_warns(self):
        self.assertEqual(self.inspect()["installation"]["codex"]["state"], "absent")
        config = Path(os.environ["CODEX_HOME"]) / "config.toml"
        config.parent.mkdir()
        config.write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")
        report = self.inspect()
        self.assertTrue(report["ok"], report)
        warning = next(item for item in report["warnings"] if item["code"] == "codex-sandbox")
        self.assertEqual(warning["path"], str(config))
        self.assertEqual(install.grant_codex_writable_root()["state"], "granted")
        self.assertNotIn("codex-sandbox", codes(self.inspect(), "warnings"))


if __name__ == "__main__":
    unittest.main()
