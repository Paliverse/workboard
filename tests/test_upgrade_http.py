# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Live scratch-server upgrade boundary contract."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.support import SRC, make_env, scratch
from workboard import core, doctor, install, server, update


class UpgradeHttpBoundary(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.old = None
        self.base = self.stack.enter_context(scratch("wb-upgrade-http-"))
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = make_env(self.home)
        self.env["PYTHONPATH"] = str(SRC) + (os.pathsep + self.env["PYTHONPATH"]
                                              if self.env.get("PYTHONPATH") else "")
        self.stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
        self.stack.callback(self._cleanup)
        self.wrapper = self.base / "workboard-wrapper.py"
        self.child_pid = self.base / "server.pid"
        self.wrapper.write_text(
            "import os, subprocess, sys\n"
            "from workboard import cli, install\n"
            "class Backend:\n"
            "  def status(self): return {'kind':'test','location':'scratch','installed':True,'current':True}\n"
            "  def start(self):\n"
            "    options={'stdin':subprocess.DEVNULL, 'stdout':subprocess.DEVNULL, 'stderr':subprocess.DEVNULL, 'env':os.environ.copy()}\n"
            "    if os.name == 'nt': options['creationflags']=subprocess.CREATE_NO_WINDOW\n"
            "    p=subprocess.Popen([sys.executable, os.environ['WB_UPGRADE_WRAPPER'], 'serve', '--port', '0', '--json'], **options)\n"
            "    open(os.environ['WB_UPGRADE_CHILD_PID'], 'w', encoding='utf-8').write(str(p.pid))\n"
            "install._backend=lambda: Backend()\n"
            "if os.environ.get('WB_OLD_SERVER') == '1':\n"
            "  from workboard import core\n"
            "  current=core.runtime_info()\n"
            "  core.runtime_info=lambda: {**current, 'schemaVersion':3, 'supportedSchemaVersions':[1,2,3], 'capabilities':['context']}\n"
            "cli.main(sys.argv[1:])\n",
            encoding="utf-8")
        self.env.update(WB_UPGRADE_WRAPPER=str(self.wrapper), WB_UPGRADE_CHILD_PID=str(self.child_pid))
        os.environ.update(WB_UPGRADE_WRAPPER=str(self.wrapper), WB_UPGRADE_CHILD_PID=str(self.child_pid))
        for skill in install.skill_targets():
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_bytes(b"---\nname: workboard\ndescription: stale\n---\nstale")
        old_env = {**self.env, "WB_OLD_SERVER": "1"}
        options = {"cwd": self.base, "env": old_env, "stdin": subprocess.DEVNULL,
                   "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.old = subprocess.Popen([sys.executable, str(self.wrapper), "serve", "--port", "0", "--json"],
                                    **options)
        self.old_health = self._await_health()

    def tearDown(self):
        self.stack.close()

    def _cleanup(self):
        # The shutdown endpoint must only ever use this test's isolated home.
        if os.environ.get("WORKBOARD_HOME") != self.env["WORKBOARD_HOME"]:
            raise AssertionError("scratch server cleanup lost its isolated environment")
        try:
            info = server.server_info(0.2)
            if info:
                owned = {self.old.pid} if self.old is not None else set()
                if self.child_pid.is_file():
                    owned.add(int(self.child_pid.read_text()))
                self.assertIn(info["pid"], owned, "cleanup found an unexpected server")
                self.assertTrue(server.stop(), "scratch server did not stop")
                if self.old is not None and info["pid"] == self.old.pid:
                    self.old.wait(timeout=10)  # Reap our direct child before POSIX PID checks.
                else:
                    self._wait_for_pid_exit(info["pid"])
        finally:
            if self.old is not None:
                try:
                    self.old.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.old.terminate()  # This exact Popen belongs to the fixture.
                    self.old.wait(timeout=10)

    def _await_health(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = server.server_info(0.2)
            if info:
                return info
            time.sleep(.05)
        self.fail("scratch server did not answer /health")

    def _wait_for_pid_exit(self, pid: int):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.name == "nt":
                import ctypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
                handle = kernel.OpenProcess(0x00100000, False, pid)
                if not handle:
                    return
                exited = kernel.WaitForSingleObject(handle, 0) == 0
                kernel.CloseHandle(handle)
                if exited:
                    return
            else:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            time.sleep(.05)
        self.fail(f"server pid {pid} did not exit")

    def test_cleanup_refuses_to_touch_a_different_home(self):
        with mock.patch.dict(os.environ, {"WORKBOARD_HOME": str(self.base / "other-home")}), \
                mock.patch.object(server, "stop") as stop:
            with self.assertRaisesRegex(AssertionError, "lost its isolated environment"):
                self._cleanup()
            stop.assert_not_called()

    def test_upgrade_refreshes_skills_and_restarts_actual_new_http_runtime(self):
        self.assertEqual(self.old_health["schemaVersion"], 3)
        self.assertEqual(self.old_health["capabilities"], ["context"])

        class Backend:
            def status(self):
                return {"kind": "test", "location": "scratch", "installed": True, "current": True}

        with mock.patch.object(update, "upgrade_command", lambda channel: [sys.executable, "-c", ""]), \
                mock.patch.object(update, "_new_workboard", lambda: [sys.executable, str(self.wrapper)]), \
                mock.patch.object(install, "_backend", lambda: Backend()):
            update.cmd_upgrade(argparse.Namespace(channel="npm", dry_run=False, after_pid=None, json=True))

        new_health = self._await_health()
        self.assertNotEqual(new_health["pid"], self.old_health["pid"])
        self.assertEqual(new_health["schemaVersion"], core.SCHEMA_VERSION)
        self.assertEqual(set(new_health["capabilities"]), set(core.runtime_info()["capabilities"]))
        self.assertTrue(self.child_pid.is_file())
        self.assertNotEqual(int(self.child_pid.read_text()), self.old.pid)
        self.old.wait(timeout=10)
        self.assertIsNotNone(self.old.returncode)
        self.assertEqual([item.read_bytes() for item in install.skill_targets()],
                         [install.bundled_skill()] * len(install.skill_targets()))
        new_pid = new_health["pid"]
        self.assertTrue(server.stop())
        self._wait_for_pid_exit(new_pid)

    def test_status_doctor_and_restart_flag_a_server_that_cannot_read_current_boards(self):
        self.assertNotIn(core.SCHEMA_VERSION, self.old_health["supportedSchemaVersions"])

        class Backend:
            def status(self):
                return {"kind": "test", "location": "scratch", "installed": True, "current": True}

        with mock.patch.object(install, "_backend", lambda: Backend()), \
                mock.patch("shutil.which", return_value=None):
            self.assertIs(install.service_status()["schemaMatch"], False)
            report = doctor.diagnose()
        self.assertIn("server-schema-mismatch", {item["code"] for item in report["blockers"]})

        # The registered service still starts the old installation: restart must say so, not succeed.
        proc = subprocess.run([sys.executable, str(self.wrapper), "service", "restart", "--json"],
                              cwd=self.base, env={**self.env, "WB_OLD_SERVER": "1"}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        error = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual((proc.returncode, error["code"]), (1, "state"), proc.stderr)
        self.assertIn(sys.executable, error["error"])
        restarted = self._await_health()
        self.assertEqual(restarted["pid"], int(self.child_pid.read_text()), "the stale server is left running")
        self.old.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
