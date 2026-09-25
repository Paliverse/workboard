# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Central board store: registry v2, project lookup, git worktrees, settings and board lifecycle.

Each test gets its own scratch home; in-process checks point os.environ at it and
CLI checks run the real ``workboard`` command against the same home.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import last_json, make_env, run, scratch

from workboard import core


class CentralStore(unittest.TestCase):
    def setUp(self):
        self.base = self.enterContext(scratch("wb-boards-"))
        self.env = make_env(self.base / "home")
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))

    def wb(self, *args, cwd=None, env=None):
        return run(args, cwd=cwd or self.base, env=env or self.env)

    def folder(self, *parts) -> Path:
        path = self.base.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_init_without_git_links_that_folder_and_subfolders_find_it(self):
        project = self.folder("plain")
        created = self.wb("init", "--json", cwd=project)
        self.assertEqual(created.returncode, 0, created.stderr)
        board = core.boards_dir() / "plain" / "board.json"
        self.assertEqual(last_json(created), {"ok": True, "name": "plain", "board": str(board),
                                              "project": str(project), "rev": 1, "actor": "tester"})
        self.assertEqual(core.registry_load(),
                         {"version": 2, "boards": {"plain": {"dir": "plain", "project": str(project)}}})
        self.assertEqual(list(project.iterdir()), [], "init wrote into the project")
        found = last_json(self.wb("which", "--json", cwd=self.folder("plain", "src", "deep")))
        self.assertEqual((found["name"], found["board"], found["project"]), ("plain", str(board), str(project)))
        other = self.folder("other")
        human = self.wb("init", cwd=other)
        self.assertEqual(human.stdout.strip(), f"board created: other for {other} — view it with: workboard open")

    def test_lookup_walks_up_and_the_nearest_linked_project_wins(self):
        outer = self.folder("outer")
        inner = self.folder("outer", "packages", "inner")
        core.create_board("outer", outer)
        core.create_board("inner", inner)
        for start, name in ((self.folder("outer", "packages"), "outer"), (inner, "inner"),
                            (self.folder("outer", "packages", "inner", "src", "deep"), "inner")):
            self.assertEqual(core.resolve_board(str(start)), (name, core.board_file(name)), start)

    def test_a_linked_worktree_shares_the_repo_board(self):
        main = self.folder("repo")
        gitdir = self.folder("repo", ".git", "worktrees", "feature")
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        worktree = self.folder("elsewhere", "feature")
        (worktree / ".git").write_text("gitdir: ../../repo/.git/worktrees/feature\n", encoding="utf-8")
        nested = self.folder("elsewhere", "feature", "src")
        self.assertEqual(core.project_root(nested), main)
        self.assertEqual(core.project_root(self.folder("repo", "src")), main)
        submodule = self.folder("repo", "vendor", "lib")
        self.folder("repo", ".git", "modules", "lib")
        (submodule / ".git").write_text("gitdir: ../../.git/modules/lib\n", encoding="utf-8")
        self.assertEqual(core.project_root(submodule), submodule)
        broken = self.folder("broken")
        (broken / ".git").write_text("gitdir: a\x00b\n", encoding="utf-8")
        self.assertEqual(core.project_root(self.folder("broken", "src")), broken)

        created = last_json(self.wb("init", "--json", cwd=nested))
        self.assertEqual((created["name"], created["project"]), ("repo", str(main)))
        self.assertEqual(last_json(self.wb("which", "--json", cwd=nested))["name"], "repo")
        again = self.wb("init", "again", "--json", cwd=main)
        error = last_json(again)
        self.assertEqual((again.returncode, error["status"], error["code"]), (1, 409, "state"))
        self.assertIn("'repo'", error["error"])

        core.create_board("web", self.folder("repo", "packages", "web"))
        for parts, name in ((("packages", "web", "src"), "web"), (("packages", "api"), "repo")):
            start = self.folder("elsewhere", "feature", *parts)
            self.assertEqual(core.resolve_board(str(start))[0], name, start)
        # Worktrees kept inside the main checkout map the same way instead of stopping at the repo root.
        inside = self.folder("repo", ".git", "worktrees", "inside")
        (inside / "commondir").write_text("../..\n", encoding="utf-8")
        tree = self.folder("repo", ".worktrees", "inside")
        (tree / ".git").write_text("gitdir: ../../.git/worktrees/inside\n", encoding="utf-8")
        for parts, name in ((("packages", "web", "src"), "web"), (("packages", "api"), "repo"), ((), "repo")):
            start = self.folder("repo", ".worktrees", "inside", *parts)
            self.assertEqual(core.resolve_board(str(start))[0], name, start)

    def test_board_option_takes_a_name_or_a_folder(self):
        alpha, docs = self.folder("alpha"), self.folder("beta", "docs")
        core.create_board("alpha", alpha)
        core.create_board("beta", docs.parent)

        def which(*args, cwd=alpha, env=None):
            return last_json(self.wb("which", "--json", *args, cwd=cwd, env=env))

        self.assertEqual(which()["name"], "alpha")
        self.assertEqual(which("--board", "beta")["name"], "beta")
        self.assertEqual(which("--board", str(docs))["name"], "beta")
        self.assertEqual(which(env={**self.env, "WORKBOARD_DEFAULT_BOARD": "beta"})["name"], "beta")
        for value in ("nope", str(core.board_file("beta"))):
            error = which("--board", value)
            self.assertEqual((error["status"], error["code"]), (404, "not_found"), value)
            self.assertIn(f"no board named or linked to {value!r}", error["error"])
        loose = which(cwd=self.folder("loose"))
        self.assertEqual((loose["status"], loose["code"]), (404, "not_found"))
        self.assertIn("workboard init", loose["error"])

    def test_link_after_moving_a_project(self):
        old = self.folder("before")
        board = core.create_board("moved", old)
        new = old.rename(self.base / "after")
        self.assertEqual(last_json(self.wb("which", "--json", cwd=new))["code"], "not_found")
        listed = last_json(self.wb("boards", "--json"))["boards"]
        self.assertEqual([(b["name"], b["exists"], b["projectExists"]) for b in listed], [("moved", True, False)])
        self.assertIn("✗ project missing", self.wb("boards").stdout)

        linked = self.wb("link", "moved", "--json", cwd=new)
        self.assertEqual(last_json(linked), {"ok": True, "name": "moved", "board": str(board), "project": str(new)})
        self.assertEqual(last_json(self.wb("which", "--json", cwd=new))["name"], "moved")
        self.assertEqual(self.wb("link", "moved", "--dir", new).stdout.strip(), f"linked moved → {new}")

        core.create_board("other", self.folder("taken"))
        for args, status, code in ((["ghost"], 404, "not_found"),
                                   (["moved", "--dir", self.base / "taken"], 409, "state"),
                                   (["moved", "--dir", self.base / "absent"], 404, "not_found")):
            error = last_json(self.wb("link", *args, "--json"))
            self.assertEqual((error["status"], error["code"]), (status, code), args)
        self.assertEqual(self.wb("link", "moved", "--board", "moved").returncode, 2)
        self.assertEqual(core.registry_load()["boards"]["moved"]["project"], str(new))

    def test_duplicate_name_or_project_is_a_state_conflict(self):
        first, second = self.folder("one"), self.folder("two")
        core.create_board("board", first)
        before = core.registry_path().read_bytes()
        for name, folder in (("board", second), ("other", first), ("other", self.folder("one", "..", "one"))):
            with self.assertRaises(core.WorkflowError) as caught:
                core.create_board(name, folder)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "state"), name)
        self.assertIn("workboard link board", str(caught.exception))
        self.assertEqual(core.registry_path().read_bytes(), before)
        self.assertEqual([p.name for p in core.boards_dir().iterdir()], ["board"])
        dirs = [core.create_board(name, self.folder(folder)).parent.name
                for name, folder in (("Board!", "three"), ("看板", "four"))]
        self.assertEqual(dirs, ["board-2", "board-3"])

    def test_registry_is_v2_and_rejects_unsafe_or_legacy_shapes(self):
        registry = core.registry_path()
        began = time.monotonic()
        self.assertEqual(core.registry_load(), {"version": 2, "boards": {}})
        self.assertLess(time.monotonic() - began, 2, "a missing registry blocked first use")
        self.assertFalse(registry.exists())
        registry.parent.mkdir(parents=True)
        project, other = str(self.base), str(self.base / "other")
        legacy = {"boards": {"legacy": str(self.base / "board" / "board.json")}}
        cases = [legacy, {"version": 3, "boards": {}}, {"version": 2, "boards": []},
                 {"version": 2, "boards": {"a": "a"}},
                 {"version": 2, "boards": {"a": {"dir": "..", "project": project}}},
                 {"version": 2, "boards": {"a": {"dir": "a/b", "project": project}}},
                 {"version": 2, "boards": {"a": {"dir": "A", "project": project}}},
                 {"version": 2, "boards": {"a": {"dir": "a", "project": "relative"}}},
                 {"version": 2, "boards": {"a": {"dir": "a", "project": project},
                                           "b": {"dir": "a", "project": other}}},
                 {"version": 2, "boards": {"a": {"dir": "a", "project": project},
                                           "b": {"dir": "b", "project": project}}}]
        for text in [*map(json.dumps, cases), "{", "null"]:
            registry.write_text(text, encoding="utf-8")
            with self.assertRaises(core.WorkflowError, msg=text) as caught:
                core.registry_load()
            self.assertEqual((caught.exception.status, caught.exception.code), (422, "invalid"), text)
            self.assertEqual(registry.read_text(encoding="utf-8"), text)
        registry.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaises(core.WorkflowError) as caught:
            core.registry_load()
        self.assertEqual(str(caught.exception), "boards.json uses an unsupported format")
        refused = self.wb("boards", "--json")
        self.assertEqual((refused.returncode, last_json(refused)["code"]), (1, "invalid"))

        valid = {"version": 2, "boards": {"a": {"dir": "a", "project": project}}}
        encoded = json.dumps(valid).encode()
        registry.write_bytes(encoded)
        self.assertEqual(core.registry_load(max_bytes=len(encoded)), valid)
        with self.assertRaises(core.WorkflowError):
            core.registry_load(max_bytes=len(encoded) - 1)

    def test_config_precedence_and_invalid_settings(self):
        config = core.home() / "config.json"
        config.parent.mkdir(parents=True)
        for key in ("WORKBOARD_PORT", "WORKBOARD_ACTOR"):
            os.environ.pop(key)
        self.assertEqual((core.configured_port(), core.actor()), (7891, "agent"))
        config.write_text(json.dumps({"port": 45679, "actor": "config-bot"}), encoding="utf-8")
        self.assertEqual((core.configured_port(), core.actor()), (45679, "config-bot"))
        os.environ.update(WORKBOARD_PORT="45680", WORKBOARD_ACTOR="env-bot")
        self.assertEqual((core.configured_port(), core.actor()), (45680, "env-bot"))

        folder = self.folder("configured")
        core.create_board("configured", folder)
        bare = {key: value for key, value in self.env.items() if key not in ("WORKBOARD_PORT", "WORKBOARD_ACTOR")}
        listed = last_json(self.wb("boards", "--json", env=bare))["boards"]
        self.assertEqual(listed[0]["url"], "http://127.0.0.1:45679/b/configured/")
        by_config = last_json(self.wb("add", "--title", "config", "--json", cwd=folder, env=bare))
        by_env = last_json(self.wb("add", "--title", "env", "--json", cwd=folder,
                                   env={**bare, "WORKBOARD_ACTOR": "env-bot"}))
        by_flag = last_json(self.wb("add", "--title", "flag", "--actor", "flag-bot", "--json", cwd=folder,
                                    env={**bare, "WORKBOARD_ACTOR": "env-bot"}))
        self.assertEqual([by_config["actor"], by_env["actor"], by_flag["actor"]],
                         ["config-bot", "env-bot", "flag-bot"])

        for text in ('{"port": 0}', '{"port": "7891"}', '{"port": true}', '{"actor": " "}',
                     '{"colour": "red"}', "[]", "{"):
            config.write_text(text, encoding="utf-8")
            with self.assertRaises(core.WorkflowError, msg=text) as caught:
                core.configured_port()
            self.assertEqual((caught.exception.status, caught.exception.code), (422, "invalid"), text)
            self.assertIn(str(config), str(caught.exception))
        failed = self.wb("which", "--json", cwd=folder)
        error = last_json(failed)
        self.assertEqual((failed.returncode, error["status"], error["code"]), (1, 422, "invalid"))

    def test_delete_moves_the_board_folder_to_deleted(self):
        folder = self.folder("doomed")
        path = core.create_board("doomed", folder)
        blob = core.attachment_store(path, "kept.bin", b"kept", "tester")
        rev = core.load(path)["rev"]
        for name, expected, base_rev, error in (("ghost", str(path), rev, core.RegistryNotFound),
                                                ("doomed", str(folder), rev, core.RegistryConflict),
                                                ("doomed", str(path), rev + 1, core.RegistryConflict),
                                                ("doomed", str(path), None, core.RegistryConflict)):
            with self.assertRaises(error):
                core.delete_registered_board(name, expected, base_rev)
        self.assertTrue(path.is_file())

        result = core.delete_registered_board("doomed", str(path), rev)
        recovery = Path(result["recoveryPath"])
        self.assertEqual(result["board"], str(path))
        self.assertEqual(recovery.parent, core.deleted_dir())
        self.assertRegex(recovery.name, r"^doomed-\d{8}T\d{6}Z$")
        self.assertFalse(path.parent.exists())
        self.assertEqual(core.load(recovery / "board.json")["rev"], rev)
        self.assertTrue(list((recovery / core.BACKUP_DIR).iterdir()))
        self.assertEqual(core.attachment_path(recovery / "board.json", blob["id"]).read_bytes(), b"kept")
        self.assertEqual(core.registry_load()["boards"], {})

        again = core.create_board("doomed", folder)
        self.assertEqual(again.parent.name, "doomed-2", "a deleted board's dir name was reused")
        shutil.rmtree(again.parent)
        with self.assertRaises(core.RegistryConflict):
            core.delete_registered_board("doomed", str(again), 1)
        self.assertEqual(core.delete_registered_board("doomed", str(again), None),
                         {"recoveryPath": None, "board": str(again)})
        self.assertEqual(core.registry_load()["boards"], {})

    def test_a_failed_delete_keeps_the_board_registered_and_in_place(self):
        path = core.create_board("kept", self.folder("kept"))
        rev, before = core.load(path)["rev"], path.read_bytes()
        real_replace = core.atomic_replace

        def failing_move(src, dst, *args, **kwargs):
            if Path(dst).parent.parent == core.deleted_dir():
                raise OSError("move into deleted/ failed")
            return real_replace(src, dst, *args, **kwargs)

        for target, effect in (("_atomic_write_json", OSError("registry write failed")),
                               ("atomic_replace", failing_move)):
            with mock.patch.object(core, target, side_effect=effect):
                with self.assertRaises(OSError, msg=target):
                    core.delete_registered_board("kept", str(path), rev)
            self.assertEqual(path.read_bytes(), before, target)
            self.assertEqual(core.board_file("kept"), path, target)
            leftovers = list(core.deleted_dir().iterdir()) if core.deleted_dir().exists() else []
            self.assertEqual(leftovers, [], target)

    def test_create_board_leaves_nothing_behind_on_failure(self):
        folder = self.folder("fragile")
        with mock.patch.object(core, "_atomic_write_json", side_effect=OSError("registry write failed")):
            with self.assertRaises(OSError):
                core.create_board("fragile", folder)
        with self.assertRaises(core.WorkflowError):
            core.create_board("fragile", folder, {"schemaVersion": 99})
        for name in ("", " padded ", "x" * 81, "tab\there"):
            with self.assertRaises(core.WorkflowError) as caught:
                core.create_board(name, folder)
            self.assertEqual(caught.exception.status, 422, name)
        self.assertEqual(list(core.boards_dir().iterdir()), [])
        self.assertEqual(core.registry_load()["boards"], {})

        legacy = self.base / "legacy.json"
        legacy.write_text(json.dumps({"schemaVersion": 2, "name": "Old title", "rev": 7,
                                      "cards": [{"id": "keep", "num": 1, "title": "Kept"}]}), encoding="utf-8")
        path = core.create_board("fragile", folder, core.load(legacy))
        saved = core.load(path)
        self.assertEqual((path.parent.name, saved["rev"], saved["name"], saved["cards"][0]["id"]),
                         ("fragile", 8, "Old title", "keep"))


if __name__ == "__main__":
    unittest.main()
