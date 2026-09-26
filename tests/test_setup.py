"""Setup, skills, service definitions, channels, version, and upgrade plans.

Everything runs against scratch homes. Service backends get a fake registry or
a fake launchctl/systemctl runner and the server module is replaced by a fake,
so no test registers a real service, starts a server, or touches the network.
"""
from __future__ import annotations

import configparser
import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tomllib
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.support import last_json, make_env, run, scratch

from workboard import __version__, cli, install, update
from workboard import core as wb

BASE = Path()
RUNNING = {"ok": True, "app": "workboard", "version": __version__, "pid": 4321, "port": 7999}
ARGV = [r"C:\Program Files\WorkBoard\workboardw.exe", "serve", "--service"]
POSIX_ARGV = ["/opt/Work Board/100%/workboard", "serve", "--service"]


def setUpModule():
    global BASE
    BASE = unittest.enterModuleContext(scratch("wb-setup-"))
    (BASE / "cwd").mkdir()
    unittest.enterModuleContext(contextlib.chdir(BASE / "cwd"))
    unittest.enterModuleContext(mock.patch.dict(os.environ, make_env(BASE / "home"), clear=True))


class FakeServer:
    def __init__(self, running=False):
        self.info = dict(RUNNING) if running else None
        self.calls = []

    def server_info(self, timeout=1.0):
        return self.info

    def stop(self, timeout=10.0):
        self.calls.append("stop")
        self.info = None
        return True

    def start_background(self, timeout=10.0):
        self.calls.append("start_background")
        self.info = dict(RUNNING)
        return self.info

    def server_url(self, port=None):
        return f"http://127.0.0.1:{port}/"


class FakeRegistry(dict):
    def set(self, name, value):
        self[name] = value

    def delete(self, name):
        del self[name]


class Runner:
    """Fake launchctl/systemctl: records argv and flips the fake server's state."""

    def __init__(self, server, *, systemd=True):
        self.server, self.systemd, self.loaded, self.calls = server, systemd, False, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        code = 0
        if argv[0] == "launchctl":
            verb = argv[1]
            if verb == "print":
                code = 0 if self.loaded else 113
            elif verb == "bootstrap":
                self.loaded, self.server.info = True, dict(RUNNING)
            elif verb == "bootout":
                self.loaded, self.server.info = False, None
            elif verb == "kickstart":
                self.server.info = dict(RUNNING)
        elif not self.systemd:
            code = 1
        elif argv[2:4] == ["enable", "--now"] or argv[2] == "start":
            self.server.info = dict(RUNNING)
        return subprocess.CompletedProcess(argv, code, "", "" if code == 0 else "Failed to connect to bus")


def invoke(*argv):
    """Run the CLI in-process; return (exit code, stdout)."""
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            cli.main([str(arg) for arg in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


def invoke_json(*argv):
    code, out = invoke(*argv)
    return code, json.loads(out.strip().splitlines()[-1])


class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.home = BASE / self.id().rsplit(".", 1)[-1]
        self.home.mkdir()
        self.env = make_env(self.home)
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        self.server = FakeServer()
        self.enterContext(mock.patch.object(install, "_server", lambda: self.server))

    def targets(self):
        return [self.home / ".agents" / "skills" / "workboard" / "SKILL.md",
                self.home / ".claude" / "skills" / "workboard" / "SKILL.md"]


class SkillsTest(ScratchCase):
    def test_install_refresh_status_remove_cycle(self):
        agents, claude = self.targets()
        bundled = install.bundled_skill()
        self.assertIn(b"name: workboard", bundled)
        _, status = invoke_json("skills", "status", "--json")
        self.assertEqual([t["state"] for t in status["targets"]], ["missing", "missing"])

        code, result = invoke_json("skills", "install", "--refresh", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([t["action"] for t in result["targets"]], ["skipped", "skipped"])
        self.assertFalse(agents.exists() or claude.exists())

        _, result = invoke_json("--json", "skills", "install")
        self.assertEqual([t["action"] for t in result["targets"]], ["installed", "installed"])
        self.assertEqual(agents.read_bytes(), bundled)
        self.assertEqual(claude.read_bytes(), bundled)
        _, result = invoke_json("skills", "install", "--json")
        self.assertEqual([t["action"] for t in result["targets"]], ["current", "current"])

        agents.write_text("---\nname: workboard\ndescription: old\n---\nold body\n", encoding="utf-8")
        claude.unlink()
        _, status = invoke_json("skills", "status", "--json")
        self.assertEqual([t["state"] for t in status["targets"]], ["stale", "missing"])
        _, result = invoke_json("skills", "install", "--refresh", "--json")
        self.assertEqual([t["action"] for t in result["targets"]], ["updated", "skipped"])
        self.assertEqual(agents.read_bytes(), bundled)
        self.assertFalse(claude.exists())

        (agents.parent / "notes.md").write_text("extra", encoding="utf-8")
        _, result = invoke_json("skills", "remove", "--json")
        self.assertEqual([t["action"] for t in result["targets"]], ["removed", "missing"])
        self.assertFalse(agents.parent.exists())
        self.assertTrue(agents.parent.parent.is_dir(), "the shared skills root must survive")

    def test_foreign_skill_directory_is_never_overwritten_or_deleted(self):
        agents, _ = self.targets()
        agents.parent.mkdir(parents=True)
        foreign = "---\nname: workboard-helper\ndescription: someone else's\n---\nkeep me\n"
        agents.write_text(foreign, encoding="utf-8")
        (agents.parent / "data.txt").write_text("keep", encoding="utf-8")
        _, result = invoke_json("skills", "install", "--json")
        self.assertEqual([(t["state"], t["action"]) for t in result["targets"]],
                         [("foreign", "skipped"), ("missing", "installed")])
        _, result = invoke_json("skills", "remove", "--json")
        self.assertEqual([t["action"] for t in result["targets"]], ["kept", "removed"])
        self.assertEqual(agents.read_text(encoding="utf-8"), foreign)
        self.assertTrue((agents.parent / "data.txt").is_file())

    def test_install_replaces_a_hardlinked_copy_without_writing_through(self):
        _, claude = self.targets()
        claude.parent.mkdir(parents=True)
        outside = self.home / "dotfiles-skill.md"
        original = b"---\nname: workboard\ndescription: pinned elsewhere\n---\nold\n"
        outside.write_bytes(original)
        os.link(outside, claude)
        install.skills_install()
        self.assertEqual(claude.read_bytes(), install.bundled_skill())
        self.assertEqual(outside.read_bytes(), original)
        self.assertFalse(os.path.samefile(outside, claude))

    def test_setup_no_service_installs_both_skills_through_the_real_cli(self):
        proc = run(["setup", "--no-service", "--json"], cwd=self.home, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = last_json(proc)
        self.assertNotIn("service", result)
        self.assertEqual([t["action"] for t in result["skills"]], ["installed", "installed"])
        proc = run(["skills", "status", "--json"], cwd=self.home, env=self.env)
        self.assertEqual([t["state"] for t in last_json(proc)["targets"]], ["current", "current"])
        self.assertTrue(all(path.is_file() for path in self.targets()))


class CodexSandboxTest(ScratchCase):
    """`setup` adds home() to Codex's writable_roots only through edits it can verify."""

    def config(self, text=None) -> Path:
        path = Path(os.environ["CODEX_HOME"]) / "config.toml"
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        return path

    def grant(self, text) -> tuple[dict, str]:
        """Grant twice; the second run must report the same state and change nothing."""
        path = self.config(text)
        first = install.grant_codex_writable_root()
        edited = path.read_bytes()
        self.assertEqual(install.grant_codex_writable_root()["state"], first["state"])
        self.assertEqual(path.read_bytes(), edited, "a second run must change nothing")
        return first, edited.decode("utf-8")

    def assert_granted(self, text, expected):
        result, edited = self.grant(text)
        self.assertEqual(result, {"state": "granted", "config": str(self.config())})
        self.assertEqual(edited, expected)
        self.assertIn(str(wb.home()), tomllib.loads(edited)["sandbox_workspace_write"]["writable_roots"])
        self.assertEqual(self.config().with_name("config.toml.workboard-bak").read_bytes(), text.encode("utf-8"))

    def assert_untouched(self, text, state):
        result, edited = self.grant(text)
        self.assertEqual(result["state"], state)
        self.assertEqual(edited, text)
        self.assertFalse(self.config().with_name("config.toml.workboard-bak").exists())
        return result

    def test_absent_config_is_never_created(self):
        self.assertEqual(install.grant_codex_writable_root(), {"state": "absent", "config": str(self.config())})
        self.assertFalse(self.config().parent.exists())

    def test_full_access_and_profiles_are_left_alone(self):
        self.assert_untouched('sandbox_mode = "danger-full-access"\n', "full-access")
        manual = self.assert_untouched('default_permissions = "workspace"\n', "manual")
        self.assertEqual(tomllib.loads(manual["snippet"]), {"sandbox_workspace_write": {
            "writable_roots": [str(wb.home())]}})

    def test_missing_table_is_appended(self):
        text = 'model = "gpt-5"\n'
        self.assert_granted(text, f"{text}\n[sandbox_workspace_write]\nwritable_roots = ['{wb.home()}']\n")

    def test_table_without_roots_gets_the_line_after_its_header(self):
        text = '[sandbox_workspace_write]\r\nnetwork_access = true\r\n\r\n[tools]\r\nweb_search = true\r\n'
        self.assert_granted(text, text.replace(
            "]\r\n", f"]\r\nwritable_roots = ['{wb.home()}']\r\n", 1))

    def test_single_line_roots_are_extended(self):
        text = '[sandbox_workspace_write]\nwritable_roots = ["/srv/shared", ]  # mine\n'
        self.assert_granted(text, f'[sandbox_workspace_write]\nwritable_roots = ["/srv/shared", \'{wb.home()}\']  # mine\n')
        text = '[sandbox_workspace_write]\nwritable_roots = ["/a"]  # see [docs]\n'
        self.assert_granted(text, f'[sandbox_workspace_write]\nwritable_roots = ["/a", \'{wb.home()}\']  # see [docs]\n')

    def test_a_home_no_literal_string_can_hold_is_manual_before_any_write(self):
        text = 'sandbox_mode = "workspace-write"\n'
        for folder in ("O'Brien", "del\x7fname"):
            home = self.home / folder / ".workboard"
            with self.subTest(folder=folder), mock.patch.dict(os.environ, {"WORKBOARD_HOME": str(home)}):
                result = self.assert_untouched(text, "manual")
                self.assertEqual(tomllib.loads(result["snippet"])["sandbox_workspace_write"]["writable_roots"],
                                 [str(home)])

    def test_multi_line_roots_need_a_manual_edit(self):
        result = self.assert_untouched('[sandbox_workspace_write]\nwritable_roots = [\n  "/srv/shared",\n]\n', "manual")
        self.assertEqual(tomllib.loads(result["snippet"])["sandbox_workspace_write"]["writable_roots"],
                         [str(wb.home())])

    def test_already_granted_is_unchanged(self):
        self.assert_untouched(f"[sandbox_workspace_write]\nwritable_roots = ['{wb.home()}']\n", "granted")

    def test_an_edit_that_does_not_parse_is_restored(self):
        text = "sandbox_workspace_write.network_access = true\n"  # A dotted-key table: a header would redefine it.
        result, edited = self.grant(text)
        self.assertEqual((result["state"], edited), ("manual", text))
        self.assertEqual(self.config().with_name("config.toml.workboard-bak").read_text(encoding="utf-8"), text)

    def test_setup_reports_the_codex_step_and_no_codex_skips_it(self):
        text = 'sandbox_mode = "workspace-write"\n'
        self.config(text)
        code, result = invoke_json("setup", "--no-skills", "--no-service", "--no-codex", "--json")
        self.assertEqual((code, result), (0, {"ok": True}))
        self.assertEqual(self.config().read_text(encoding="utf-8"), text)
        code, out = invoke("setup", "--no-skills", "--no-service")
        self.assertEqual((code, out.strip()), (0, "codex: granted"))
        code, result = invoke_json("setup", "--no-skills", "--no-service", "--json")
        self.assertEqual((code, result["codex"]), (0, {"state": "granted", "config": str(self.config())}))


class ServerCommandTest(unittest.TestCase):
    def app(self, *names):
        root = Path(self.enterContext(scratch("wb-app-")))
        for name in names:
            (root / name).write_bytes(b"")
        return root

    def patch(self, *, windows, frozen, executable):
        self.enterContext(mock.patch.object(install, "_WINDOWS", windows))
        self.enterContext(mock.patch.object(sys, "frozen", frozen, create=True))
        self.enterContext(mock.patch.object(sys, "executable", str(executable)))

    def test_frozen_prefers_the_windowed_sibling_only_on_windows(self):
        root = self.app("workboard.exe")
        self.patch(windows=True, frozen=True, executable=root / "workboard.exe")
        self.assertEqual(install.server_command(), [str(root / "workboard.exe"), "serve", "--service"])
        (root / "workboardw.exe").write_bytes(b"")
        self.assertEqual(install.server_command(), [str(root / "workboardw.exe"), "serve", "--service"])
        self.patch(windows=False, frozen=True, executable=root / "workboard.exe")
        self.assertEqual(install.server_command(), [str(root / "workboard.exe"), "serve", "--service"])

    def test_source_install_runs_the_module_with_pythonw_on_windows(self):
        root = self.app("python.exe")
        self.patch(windows=True, frozen=False, executable=root / "python.exe")
        module = ["-m", "workboard", "serve", "--service"]
        self.assertEqual(install.server_command(), [str(root / "python.exe"), *module])
        (root / "pythonw.exe").write_bytes(b"")
        self.assertEqual(install.server_command(), [str(root / "pythonw.exe"), *module])
        self.patch(windows=False, frozen=False, executable=root / "python3")
        self.assertEqual(install.server_command(), [str(root / "python3"), *module])


class RuntimeCopyTest(ScratchCase):
    def app(self, name="WorkBoard", html=b"<html>"):
        app = self.home / "Programs" / name
        (app / "_internal" / "workboard" / "web").mkdir(parents=True)
        (app / "workboardw.exe").write_bytes(b"exe")
        (app / "_internal" / "python3.dll").write_bytes(b"dll")
        (app / "_internal" / "workboard" / "web" / "board.html").write_bytes(html)
        return app

    def frozen_windows(self, executable):
        self.enterContext(mock.patch.object(install, "_WINDOWS", True))
        self.enterContext(mock.patch.object(sys, "frozen", True, create=True))
        self.enterContext(mock.patch.object(sys, "executable", str(executable)))

    def test_fingerprint_follows_the_build_bytes(self):
        first, same, rebuilt = self.app("a"), self.app("b"), self.app("c", html=b"<html>new")
        fingerprint = install.runtime_fingerprint(first)
        self.assertRegex(fingerprint, r"^[0-9a-f]{12}$")
        self.assertEqual(install.runtime_fingerprint(same), fingerprint)
        self.assertNotEqual(install.runtime_fingerprint(rebuilt), fingerprint)
        (same / "workboardw.exe").write_bytes(b"exe2")
        self.assertNotEqual(install.runtime_fingerprint(same), fingerprint)

    def test_frozen_windows_service_relaunches_from_a_build_copy_and_prunes_stale_ones(self):
        app = self.app()
        self.frozen_windows(app / "workboardw.exe")
        self.enterContext(mock.patch.object(sys, "argv", [str(app / "workboardw.exe"), "serve", "--service"]))
        popen = self.enterContext(mock.patch("subprocess.Popen"))
        copy = wb.home() / "runtime" / f"{__version__}-{install.runtime_fingerprint(app)}"
        self.assertFalse(install.in_runtime_copy())
        self.assertTrue(install.relaunch_from_runtime_copy())
        self.assertEqual((copy / "_internal" / "python3.dll").read_bytes(), b"dll")
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [str(copy / "workboardw.exe"), "serve", "--service"])
        self.assertEqual(popen.call_args.kwargs["creationflags"], install._DETACHED)

        stale = wb.home() / "runtime" / f"{__version__}-000000000000"
        stale.mkdir()
        sys.executable = str(copy / "workboardw.exe")
        self.assertTrue(install.in_runtime_copy())
        popen.reset_mock()
        self.assertFalse(install.relaunch_from_runtime_copy())
        popen.assert_not_called()
        self.assertFalse(stale.exists())
        self.assertTrue(copy.is_dir())

    def test_a_rebuilt_same_version_package_gets_a_fresh_copy(self):
        app = self.app()
        self.frozen_windows(app / "workboardw.exe")
        popen = self.enterContext(mock.patch("subprocess.Popen"))
        self.assertTrue(install.relaunch_from_runtime_copy())
        old = Path(popen.call_args.args[0][0]).parent
        (app / "_internal" / "workboard" / "web" / "board.html").write_bytes(b"<html>rebuilt")
        self.assertTrue(install.relaunch_from_runtime_copy())
        new = Path(popen.call_args.args[0][0]).parent
        self.assertNotEqual(new, old)
        self.assertEqual((new / "_internal" / "workboard" / "web" / "board.html").read_bytes(), b"<html>rebuilt")
        sys.executable = str(new / "workboardw.exe")
        self.assertTrue(install.in_runtime_copy())
        (new / "workboardw.exe").write_bytes(b"tampered")  # Bytes no longer match the directory's name.
        self.assertFalse(install.in_runtime_copy())

    def test_everything_else_runs_in_place(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertFalse(install.relaunch_from_runtime_copy())
        with mock.patch.object(install, "_WINDOWS", False), mock.patch.object(sys, "frozen", True, create=True):
            self.assertFalse(install.relaunch_from_runtime_copy())


class ServiceTest(ScratchCase):
    def test_windows_run_value_lifecycle(self):
        registry = FakeRegistry()
        backend = install.WindowsService(registry=registry)
        self.enterContext(mock.patch.object(install, "_backend", lambda: backend))
        self.enterContext(mock.patch.object(install, "server_command", return_value=ARGV))
        _, status = invoke_json("service", "status", "--json")
        self.assertEqual((status["installed"], status["running"]), (False, False))

        _, result = invoke_json("service", "install", "--json")
        self.assertEqual(registry[install.RUN_VALUE], r'"C:\Program Files\WorkBoard\workboardw.exe" serve --service')
        self.assertEqual((result["action"], result["running"]), ("installed", True))
        self.assertEqual(self.server.calls, ["start_background"])
        _, result = invoke_json("service", "install", "--json")
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.server.calls, ["start_background"], "a running server is left alone")
        _, status = invoke_json("service", "status", "--json")
        self.assertEqual((status["installed"], status["current"], status["versionMatch"]), (True, True, True))

        _, result = invoke_json("service", "restart", "--json")
        self.assertEqual(self.server.calls[1:], ["stop", "start_background"])
        _, result = invoke_json("service", "remove", "--json")
        self.assertEqual((result["action"], result["stopped"]), ("removed", True))
        self.assertNotIn(install.RUN_VALUE, registry)
        self.assertIsNone(self.server.info)
        _, result = invoke_json("service", "remove", "--json")
        self.assertEqual(result["action"], "absent")

    def test_setup_reports_skills_and_service(self):
        backend = install.WindowsService(registry=FakeRegistry())
        self.enterContext(mock.patch.object(install, "_backend", lambda: backend))
        code, result = invoke_json("setup", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([t["action"] for t in result["skills"]], ["installed", "installed"])
        self.assertEqual((result["service"]["action"], result["service"]["kind"]), ("installed", "run-key"))
        self.assertEqual(result["codex"]["state"], "absent")
        code, out = invoke("setup")
        self.assertIn("codex: absent", out.splitlines())
        self.assertEqual(len(out.strip().splitlines()), 3, out)

    def test_launch_agent_plist_and_launchctl_lifecycle(self):
        runner = Runner(self.server)
        backend = install.LaunchAgent(run=runner, uid=501)
        self.enterContext(mock.patch.object(install, "server_command", return_value=POSIX_ARGV))
        result = install.service_install(backend)
        path = self.home / "Library" / "LaunchAgents" / "io.github.paliverse.workboard.plist"
        log = str(wb.logs_dir() / "service.log")
        self.assertEqual(plistlib.loads(path.read_bytes()), {
            "Label": "io.github.paliverse.workboard", "ProgramArguments": POSIX_ARGV, "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": log, "StandardErrorPath": log})
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(path)], runner.calls)
        self.assertEqual((result["action"], result["location"]), ("installed", str(path)))
        self.assertTrue(wb.logs_dir().is_dir())

        runner.calls.clear()
        self.assertEqual(install.service_install(backend)["action"], "unchanged")
        self.assertEqual(runner.calls, [["launchctl", "print", "gui/501/io.github.paliverse.workboard"]])
        install.service_restart(backend)
        self.assertEqual(self.server.calls, ["stop"])
        self.assertIn(["launchctl", "kickstart", "-k", "gui/501/io.github.paliverse.workboard"], runner.calls)
        self.assertEqual(install.service_remove(backend)["action"], "removed")
        self.assertIn(["launchctl", "bootout", "gui/501/io.github.paliverse.workboard"], runner.calls)
        self.assertFalse(path.exists())

    def test_systemd_user_unit(self):
        runner = Runner(self.server)
        backend = install.LinuxService(run=runner)
        self.enterContext(mock.patch.object(install, "server_command", return_value=POSIX_ARGV))
        result = install.service_install(backend)
        unit = self.home / ".config" / "systemd" / "user" / "workboard.service"
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read_string(unit.read_text(encoding="utf-8"))
        self.assertEqual(parsed["Service"]["ExecStart"], "'/opt/Work Board/100%%/workboard' serve --service")
        self.assertEqual(parsed["Service"]["Restart"], "on-failure")
        self.assertEqual(parsed["Install"]["WantedBy"], "default.target")
        self.assertIn(["systemctl", "--user", "daemon-reload"], runner.calls)
        self.assertIn(["systemctl", "--user", "enable", "--now", "workboard.service"], runner.calls)
        self.assertEqual((result["kind"], result["action"], result["current"]), ("systemd", "installed", True))
        self.assertEqual(self.server.calls, [], "systemd started it; no second server was spawned")

        self.assertEqual(install.service_remove(backend)["action"], "removed")
        self.assertIn(["systemctl", "--user", "disable", "--now", "workboard.service"], runner.calls)
        self.assertFalse(unit.exists())

    def test_xdg_autostart_fallback_without_a_user_systemd(self):
        backend = install.LinuxService(run=Runner(self.server, systemd=False))
        self.enterContext(mock.patch.object(install, "server_command", return_value=POSIX_ARGV))
        result = install.service_install(backend)
        desktop = self.home / ".config" / "autostart" / "workboard.desktop"
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read_string(desktop.read_text(encoding="utf-8"))
        self.assertEqual(parsed["Desktop Entry"]["Exec"], '"/opt/Work Board/100%%/workboard" serve --service')
        self.assertEqual(parsed["Desktop Entry"]["Type"], "Application")
        self.assertEqual((result["kind"], result["action"]), ("autostart", "installed"))
        self.assertEqual(self.server.calls, ["start_background"])
        self.assertFalse((self.home / ".config" / "systemd").exists())


class ChannelTest(unittest.TestCase):
    TABLE = [
        (r"C:\Users\u\AppData\Roaming\npm\node_modules\workboard\node_modules\@paliverse\workboard-win32-x64\workboard\workboard.exe", "npm"),
        ("/usr/local/lib/node_modules/workboard/node_modules/@paliverse/workboard-darwin-arm64/workboard/workboard", "npm"),
        ("/home/u/.local/share/workboard/workboard", "unknown"),
        (r"C:\Users\u\AppData\Local\Programs\WorkBoard\workboard.exe", "unknown"),
    ]

    def test_path_heuristics(self):
        for executable, expected in self.TABLE:
            with self.subTest(executable=executable):
                self.assertEqual(update.channel_for(executable), expected)

    def test_install_receipt_marks_a_script_install_and_src_tree_marks_source(self):
        root = Path(self.enterContext(scratch("wb-receipt-")))
        executable = root / "node_modules" / "workboard.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(executable)):
            self.assertEqual(update.detect_channel(), "npm")
            (executable.parent / "install-receipt.json").write_text("{}", encoding="utf-8")
            self.assertEqual(update.detect_channel(), "script")
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(update.detect_channel(), "source")  # These tests import src/workboard.


class VersionTest(ScratchCase):
    def test_version_reports_channel(self):
        code, result = invoke_json("version", "--json")
        self.assertEqual(code, 0)
        self.assertEqual((result["version"], result["channel"]), (__version__, "source"))
        self.assertEqual(result["executable"], sys.executable)
        self.assertTrue(result["python"] and result["platform"])
        self.assertEqual(invoke("version")[1].strip(), f"workboard {__version__} (source)")

    def test_check_queries_github_and_reports_network_failure_as_io(self):
        seen = []

        def urlopen(request, timeout):
            seen.append((request.full_url, request.get_header("User-agent"), timeout))
            return io.BytesIO(json.dumps({"tag_name": "v99.0.0"}).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            _, result = invoke_json("version", "--check", "--json")
        self.assertEqual((result["latest"], result["updateAvailable"]), ("99.0.0", True))
        self.assertEqual(seen, [(update.LATEST_RELEASE_API, f"workboard/{__version__}", 5.0)])
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            code, error = invoke_json("version", "--check", "--json")
        self.assertEqual((code, error["ok"], error["code"]), (1, False, "io"))


class UpgradeTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.registry = FakeRegistry()
        backend = install.WindowsService(registry=self.registry)
        self.enterContext(mock.patch.object(install, "_backend", lambda: backend))

    def forbid_execution(self):
        self.enterContext(mock.patch("subprocess.run", side_effect=AssertionError("dry run executed")))
        self.enterContext(mock.patch("subprocess.Popen", side_effect=AssertionError("dry run spawned")))

    def test_dry_run_prints_the_exact_plan_per_channel(self):
        self.forbid_execution()
        downloads = "https://github.com/Paliverse/workboard/releases/latest/download"
        cases = [
            ("npm", False, ["npm", "install", "-g", "@paliverse/workboard@latest"]),
            ("script", True, ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                              f"irm {downloads}/install.ps1 | iex"]),
            ("script", False, ["sh", "-c", f"curl -fsSL {downloads}/install.sh | sh"]),
        ]
        for channel, windows, command in cases:
            with self.subTest(channel=channel, windows=windows), mock.patch.object(install, "_WINDOWS", windows):
                code, result = invoke_json("upgrade", "--dry-run", "--channel", channel, "--json")
                self.assertEqual(code, 0)
                self.assertEqual((result["channel"], result["dryRun"], result["command"]), (channel, True, command))
                self.assertEqual(result["steps"], [update.display(command), "workboard skills install --refresh"])
        with mock.patch.object(install, "_WINDOWS", True):
            _, out = invoke("upgrade", "--dry-run", "--channel", "script")
        self.assertIn('powershell -NoProfile -ExecutionPolicy Bypass -Command "irm '
                      f'{downloads}/install.ps1 | iex"', out)
        with mock.patch.object(install, "_WINDOWS", False):
            _, out = invoke("upgrade", "--dry-run", "--channel", "script")
        self.assertIn(f"sh -c 'curl -fsSL {downloads}/install.sh | sh'", out)
        self.assertTrue(out.rstrip().endswith("dry run: nothing was executed"))

    def test_dry_run_includes_server_stop_and_service_restart_without_running_them(self):
        self.forbid_execution()
        self.server.info = dict(RUNNING)
        self.registry[install.RUN_VALUE] = "anything"
        _, result = invoke_json("upgrade", "--dry-run", "--channel", "npm", "--json")
        self.assertEqual(result["steps"], [
            f"stop the running server (v{__version__}, pid 4321)", "npm install -g @paliverse/workboard@latest",
            "workboard skills install --refresh", "workboard service restart"])
        self.assertEqual(self.server.calls, [])

    def test_source_and_unknown_installs_explain_and_fail(self):
        for channel in ("source", "unknown"):
            with self.subTest(channel=channel):
                code, error = invoke_json("upgrade", "--dry-run", "--channel", channel, "--json")
                self.assertEqual((code, error["code"]), (1, "state"))
        code, error = invoke_json("upgrade", "--dry-run", "--json")  # This checkout is `source`.
        self.assertIn("git pull", error["error"])
        site = BASE / "site-packages" / "workboard" / "update.py"
        with mock.patch.object(update, "__file__", str(site)):
            self.assertEqual(update.detect_channel(), "unknown")
            code, error = invoke_json("upgrade", "--dry-run", "--json")
        self.assertEqual((code, error["code"]), (1, "state"))
        self.assertIn("npm install -g @paliverse/workboard", error["error"])

    def test_runtime_copy_waits_for_the_original_then_runs_the_plan(self):
        events = []
        self.server.info = dict(RUNNING)
        self.server.stop = lambda timeout=10.0: events.append("stop") or True
        self.enterContext(mock.patch.object(update, "_wait_for_exit", lambda pid: events.append(("wait", pid))))
        self.enterContext(mock.patch.object(update, "_run_step", lambda argv, stdout: events.append(argv)))
        self.enterContext(mock.patch.object(update, "_new_version", lambda new: "0.2.0"))
        self.enterContext(mock.patch("shutil.which", return_value="/new/workboard"))
        code, out = invoke("upgrade", "--channel", "npm", "--after-pid", "4242")
        self.assertEqual(code, 0)
        self.assertEqual(events, [("wait", 4242), "stop", ["npm", "install", "-g", "@paliverse/workboard@latest"],
                                  ["/new/workboard", "skills", "install", "--refresh"],
                                  ["/new/workboard", "service", "restart"]])
        self.assertTrue(out.startswith("upgrade plan (npm):"), out)
        self.assertEqual(out.strip().splitlines()[-1], f"upgrade complete: {__version__} -> 0.2.0")

    def test_failed_channel_command_restarts_the_stopped_server(self):
        self.server.info = dict(RUNNING)

        def fail(argv, stdout):
            raise wb.WorkflowError("`npm install -g @paliverse/workboard@latest` failed with exit code 1", 500, "io")

        self.enterContext(mock.patch.object(update, "_run_step", fail))
        code, error = invoke_json("upgrade", "--channel", "npm", "--json")
        self.assertEqual((code, error["code"]), (1, "io"))
        self.assertEqual(self.server.calls, ["stop", "stop", "start_background"])
        self.assertIsNotNone(self.server.info)

    def test_frozen_windows_hands_off_to_a_runtime_copy_and_exits(self):
        app = self.home / "Programs" / "WorkBoard"
        app.mkdir(parents=True)
        (app / "workboard.exe").write_bytes(b"exe")
        (app / "install-receipt.json").write_text('{"channel": "script"}', encoding="utf-8")
        self.enterContext(mock.patch.object(install, "_WINDOWS", True))
        self.enterContext(mock.patch.object(sys, "frozen", True, create=True))
        self.enterContext(mock.patch.object(sys, "executable", str(app / "workboard.exe")))
        popen = self.enterContext(mock.patch("subprocess.Popen"))
        popen.return_value.pid = 777
        code, out = invoke("upgrade")
        copy = wb.home() / "runtime" / f"{__version__}-{install.runtime_fingerprint(app)}" / "workboard.exe"
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args.args[0], [str(copy), "upgrade", "--channel", "script",
                                                   "--after-pid", str(os.getpid())])
        self.assertEqual(set(popen.call_args.kwargs) & {"creationflags", "stdout", "stderr"}, set(),
                         "the copy shares this console")
        self.assertEqual(copy.read_bytes(), b"exe")
        self.assertEqual(out.strip(), "continuing upgrade from a temporary copy (pid 777)…")
        self.assertEqual(self.server.calls, [])


if __name__ == "__main__":
    unittest.main()
