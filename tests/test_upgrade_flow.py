# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Upgrade postconditions against a selected executable in a scratch home."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import make_env, scratch
from workboard import core, install, update


class StoppingServer:
    """Answers until stopped; the selected executable's `service restart` marks it restarted."""

    def __init__(self, restarted):
        self.restarted, self.stopped = restarted, False

    def server_info(self, timeout=1.0):
        if self.stopped and not self.restarted.exists():
            return None
        return {"version": "0.1.2", "pid": 4321}

    def stop(self, timeout=10.0):
        self.stopped = True
        return True


class Backend:
    def status(self):
        return {"installed": True}


class UpgradeFlow(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.base = self.stack.enter_context(scratch("wb-upgrade-flow-"))
        self.home = self.base / "home"
        self.home.mkdir()
        self.restarted = self.base / "restarted"
        self.env = make_env(self.home, WORKBOARD_TEST_RESTARTED=str(self.restarted))
        self.stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
        self.skills = install.skill_targets()
        for skill in self.skills:
            skill.parent.mkdir(parents=True)
            skill.write_bytes(b"---\nname: workboard\ndescription: stale\n---\nstale skill bytes")
        self.selected = self.base / "selected.py"
        self.stack.enter_context(mock.patch.object(update, "upgrade_command",
                                                    lambda channel: [sys.executable, "-c", ""]))
        self.stack.enter_context(mock.patch.object(update, "_new_workboard",
                                                    lambda: [sys.executable, str(self.selected)]))
        self.stack.enter_context(mock.patch.object(install, "_backend", lambda: Backend()))

    def tearDown(self):
        self.stack.close()

    def select(self, *, restart_exit=0):
        # An 0.1.x-shaped `version --json`: no runtime compatibility metadata.
        self.selected.write_text(
            "import os, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "if args == ['version', '--json']:\n"
            "    print('{\"ok\": true, \"version\": \"99.0.0\", \"channel\": \"npm\"}')\n"
            "elif args == ['skills', 'install', '--refresh']:\n"
            "    raise SystemExit(subprocess.run([sys.executable, '-m', 'workboard', *args]).returncode)\n"
            "elif args == ['service', 'restart']:\n"
            f"    if {restart_exit}: raise SystemExit({restart_exit})\n"
            "    open(os.environ['WORKBOARD_TEST_RESTARTED'], 'wb').close()\n",
            encoding="utf-8")

    def upgrade(self, server):
        out = io.StringIO()
        with mock.patch.object(install, "_server", lambda: server), contextlib.redirect_stdout(out):
            update.cmd_upgrade(argparse.Namespace(channel="npm", dry_run=False, after_pid=None, json=True))
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def test_upgrade_refreshes_stale_skills_and_restarts_through_the_new_executable(self):
        self.select()
        server = StoppingServer(self.restarted)
        result = self.upgrade(server)
        self.assertEqual((result["ok"], result["to"]), (True, "99.0.0"))
        self.assertTrue(server.stopped)
        self.assertEqual([skill.read_bytes() for skill in self.skills],
                         [install.bundled_skill()] * len(self.skills))
        self.assertTrue(self.restarted.exists())

    def test_failed_restart_names_the_stopped_server(self):
        self.select(restart_exit=1)
        with self.assertRaises(core.WorkflowError) as failed:
            self.upgrade(StoppingServer(self.restarted))
        self.assertEqual(failed.exception.code, "io")
        self.assertIn("pid 4321", str(failed.exception))
        self.assertIn("workboard service restart", str(failed.exception))

    def test_failed_executable_probe_cannot_succeed(self):
        self.select()
        for returncode, metadata in ((1, {"ok": True, "version": "99.0.0"}), (0, {"ok": True, "version": ""}),
                                     (0, ["99.0.0"])):
            with self.subTest(returncode=returncode, metadata=metadata):
                result = subprocess.CompletedProcess([], returncode, json.dumps(metadata), "probe failed")
                with mock.patch.object(subprocess, "run", return_value=result), \
                        self.assertRaises(core.WorkflowError) as failed:
                    update._new_version([sys.executable, str(self.selected)])
                self.assertEqual(failed.exception.code, "io")


if __name__ == "__main__":
    unittest.main()
