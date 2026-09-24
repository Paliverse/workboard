# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Per-user setup: agent skill installation and the background server service.

`workboard setup` copies the bundled skill into every agent harness and
registers a per-user service that keeps the one local server running:
Windows HKCU Run value, macOS LaunchAgent, Linux systemd --user unit (XDG
autostart fallback). Backends take an injectable registry or command runner
so tests exercise them in a scratch home without touching real registrations.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from . import core as wb

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "WorkBoard"
LAUNCH_LABEL = "io.github.paliverse.workboard"
UNIT_NAME = "workboard.service"
SYSTEMCTL = ("systemctl", "--user")
SKILL_NAME = "workboard"
_WINDOWS = os.name == "nt"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW (literal: subprocess lacks them off Windows)
_DETACHED = 0x00000008 | 0x00000200 | 0x08000000


def _server():
    """The server module, imported lazily so ordinary board commands never load it."""
    from . import server
    return server


# ===== server command and the Windows runtime copy =====

def server_command() -> list[str]:
    """argv that starts `serve --service` for THIS installation."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if _WINDOWS:
            windowed = executable.with_name("workboardw.exe")
            return [str(windowed if windowed.is_file() else executable), "serve", "--service"]
        return [str(executable), "serve", "--service"]
    python = Path(sys.executable)
    if _WINDOWS:
        windowed = python.with_name("pythonw.exe")
        python = windowed if windowed.is_file() else python
    return [str(python), "-m", "workboard", "serve", "--service"]


def _runtime_dir() -> Path:
    return wb.home() / "runtime" / __version__


def in_runtime_copy() -> bool:
    return (os.path.normcase(os.path.realpath(Path(sys.executable).parent))
            == os.path.normcase(os.path.realpath(_runtime_dir())))


def runtime_copy() -> Path:
    """Copy this frozen app directory to home/runtime/<version>/ once; return the copied executable."""
    target = _runtime_dir()
    if not target.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(Path(sys.executable).parent, staging)
        deadline = time.monotonic() + wb.REPLACE_RETRY_SECONDS
        while True:
            try:
                staging.rename(target)  # Atomic: a present version directory is always complete.
                break
            except OSError as exc:
                # Antivirus scanners briefly hold freshly copied executables open on Windows.
                if isinstance(exc, PermissionError) and not target.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                shutil.rmtree(staging, ignore_errors=True)
                if not target.is_dir():
                    raise
                break
    return target / Path(sys.executable).name


def _prune_runtimes() -> None:
    """Best effort: delete older runtime copies. Windows refuses to rename a directory in use."""
    current = _runtime_dir()
    with contextlib.suppress(OSError):
        for entry in list(current.parent.iterdir()):
            if entry.name == current.name or entry.name.startswith(".") or not entry.is_dir():
                continue
            trash = entry.with_name(f".trash-{entry.name}-{os.getpid()}")
            try:
                entry.rename(trash)
            except OSError:
                continue
            shutil.rmtree(trash, ignore_errors=True)


def relaunch_from_runtime_copy() -> bool:
    """Windows + frozen: run the service from a private copy so package managers can replace files."""
    if not (_WINDOWS and getattr(sys, "frozen", False)):
        return False
    if in_runtime_copy():
        _prune_runtimes()
        return False
    import subprocess
    executable = runtime_copy()
    subprocess.Popen([str(executable), *sys.argv[1:]], cwd=str(executable.parent),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=_DETACHED, close_fds=True)
    return True


# ===== file helpers =====

def _write_if_changed(path: Path, data: bytes) -> bool:
    """Atomically replace path (never writing through a hardlink); False when already identical."""
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


# ===== skills =====

def skill_targets() -> list[Path]:
    home = Path.home()
    return [home / ".agents" / "skills" / SKILL_NAME / "SKILL.md",   # Codex, Pi, OMP, Gemini, Cursor, OpenCode, Copilot
            home / ".claude" / "skills" / SKILL_NAME / "SKILL.md"]   # Claude Code


def bundled_skill() -> bytes:
    from importlib import resources
    return resources.files("workboard").joinpath("skills", SKILL_NAME, "SKILL.md").read_bytes()


def _frontmatter_name(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            head = stream.read(65536).decode("utf-8-sig", errors="replace")
    except OSError:
        return None
    match = re.match(r"---\r?\n(.*?)\r?\n---", head, re.S)
    for line in (match[1].splitlines() if match else ()):
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("\"'")
    return None


def _skill_state(path: Path, bundled: bytes) -> str:
    if not path.is_file():
        return "missing"
    if _frontmatter_name(path) != SKILL_NAME:
        return "foreign"
    return "current" if path.read_bytes() == bundled else "stale"


def skills_status() -> list[dict]:
    bundled = bundled_skill()
    return [{"path": str(path), "state": _skill_state(path, bundled)} for path in skill_targets()]


def skills_install(refresh: bool = False) -> list[dict]:
    """Write every target (refresh: only existing ones); never overwrite a foreign skill."""
    bundled = bundled_skill()
    results = []
    for path in skill_targets():
        state = _skill_state(path, bundled)
        if state == "stale" or (state == "missing" and not refresh):
            _write_if_changed(path, bundled)
            action = "updated" if state == "stale" else "installed"
        else:
            action = "current" if state == "current" else "skipped"
        results.append({"path": str(path), "state": state, "action": action})
    return results


def skills_remove() -> list[dict]:
    """Delete only skill directories whose SKILL.md declares `name: workboard`."""
    results = []
    for path in skill_targets():
        if not path.is_file():
            action = "missing"
        elif _frontmatter_name(path) != SKILL_NAME:
            action = "kept"
        else:
            shutil.rmtree(path.parent)
            action = "removed"
        results.append({"path": str(path.parent), "action": action})
    return results


# ===== service backends =====

def _run(argv: list[str]):
    import subprocess
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _check(proc) -> None:
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise wb.WorkflowError(f"`{' '.join(proc.args)}` failed ({proc.returncode}): {detail}", 500, "io")


class _Registry:
    """The real HKCU Run key (Windows only)."""

    def get(self, name: str) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)


class WindowsService:
    kind = "run-key"

    def __init__(self, registry=None):
        self.registry = _Registry() if registry is None else registry

    def location(self) -> str:
        return f"HKCU\\{RUN_KEY}\\{RUN_VALUE}"

    def render(self) -> str:
        import subprocess
        executable, *arguments = server_command()
        return f'"{executable}" {subprocess.list2cmdline(arguments)}'.rstrip()

    def status(self) -> dict:
        value = self.registry.get(RUN_VALUE)
        return {"kind": self.kind, "location": self.location(), "installed": value is not None,
                "current": value == self.render()}

    def install(self) -> bool:
        if self.registry.get(RUN_VALUE) == self.render():
            return False
        self.registry.set(RUN_VALUE, self.render())
        return True

    def remove(self) -> bool:
        if self.registry.get(RUN_VALUE) is None:
            return False
        self.registry.delete(RUN_VALUE)
        return True

    def start(self) -> None:
        _server().start_background()  # The Run value only fires at the next logon.


class LaunchAgent:
    kind = "launchagent"

    def __init__(self, run=None, uid: int | None = None):
        self.run = _run if run is None else run
        self.uid = uid

    def path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"

    def location(self) -> str:
        return str(self.path())

    def _domain(self) -> str:
        return f"gui/{os.getuid() if self.uid is None else self.uid}"

    def _target(self) -> str:
        return f"{self._domain()}/{LAUNCH_LABEL}"

    def render(self) -> dict:
        log = str(wb.logs_dir() / "service.log")
        return {"Label": LAUNCH_LABEL, "ProgramArguments": server_command(), "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": log, "StandardErrorPath": log}

    def _bytes(self) -> bytes:
        import plistlib
        return plistlib.dumps(self.render())

    def status(self) -> dict:
        try:
            stored = self.path().read_bytes()
        except FileNotFoundError:
            stored = None
        return {"kind": self.kind, "location": self.location(), "installed": stored is not None,
                "current": stored == self._bytes()}

    def _loaded(self) -> bool:
        return self.run(["launchctl", "print", self._target()]).returncode == 0

    def install(self) -> bool:
        loaded = self._loaded()
        wb.logs_dir().mkdir(parents=True, exist_ok=True)  # launchd opens the log paths itself.
        changed = _write_if_changed(self.path(), self._bytes())
        if changed and loaded:
            self.run(["launchctl", "bootout", self._target()])
            loaded = False
        if not loaded:
            _check(self.run(["launchctl", "bootstrap", self._domain(), str(self.path())]))
        return changed

    def remove(self) -> bool:
        self.run(["launchctl", "bootout", self._target()])
        if not self.path().exists():
            return False
        self.path().unlink()
        return True

    def start(self) -> None:
        if self._loaded():
            _check(self.run(["launchctl", "kickstart", "-k", self._target()]))
        else:
            _check(self.run(["launchctl", "bootstrap", self._domain(), str(self.path())]))


def _desktop_quote(argument: str) -> str:
    """Desktop Entry Exec quoting; string-level escaping doubles every backslash."""
    if argument and not re.search(r"[\s\"'\\><~|&;$*?#()`%]", argument):
        return argument
    quoted = re.sub(r'(["`$\\])', r"\\\1", argument).replace("\\", "\\\\").replace("%", "%%")
    return f'"{quoted}"'


class LinuxService:
    def __init__(self, run=None):
        self.run = _run if run is None else run

    def _config(self) -> Path:
        configured = os.environ.get("XDG_CONFIG_HOME")
        return Path(configured) if configured and os.path.isabs(configured) else Path.home() / ".config"

    def unit_path(self) -> Path:
        return self._config() / "systemd" / "user" / UNIT_NAME

    def desktop_path(self) -> Path:
        return self._config() / "autostart" / "workboard.desktop"

    def render(self) -> str:
        import shlex
        command = shlex.join(server_command()).replace("%", "%%").replace("$", "$$")
        return ("[Unit]\nDescription=WorkBoard local board server\n\n"
                f"[Service]\nType=simple\nExecStart={command}\nRestart=on-failure\nRestartSec=3\n\n"
                "[Install]\nWantedBy=default.target\n")

    def render_desktop(self) -> str:
        command = " ".join(_desktop_quote(argument) for argument in server_command())
        return ("[Desktop Entry]\nType=Application\nName=WorkBoard\n"
                f"Comment=WorkBoard local board server\nExec={command}\n"
                "Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n")

    def _systemd(self) -> bool:
        return self.run([*SYSTEMCTL, "show-environment"]).returncode == 0

    def status(self) -> dict:
        for kind, path, render in (("systemd", self.unit_path(), self.render),
                                   ("autostart", self.desktop_path(), self.render_desktop)):
            if path.is_file():
                return {"kind": kind, "location": str(path), "installed": True,
                        "current": path.read_text(encoding="utf-8") == render()}
        return {"kind": "systemd", "location": str(self.unit_path()), "installed": False, "current": False}

    def install(self) -> bool:
        if not self._systemd():
            return _write_if_changed(self.desktop_path(), self.render_desktop().encode("utf-8"))
        changed = _write_if_changed(self.unit_path(), self.render().encode("utf-8"))
        _check(self.run([*SYSTEMCTL, "daemon-reload"]))
        _check(self.run([*SYSTEMCTL, "enable", "--now", UNIT_NAME]))
        self.desktop_path().unlink(missing_ok=True)  # Never two autostart mechanisms.
        return changed

    def remove(self) -> bool:
        existed = False
        if self.unit_path().is_file():
            existed = True
            self.run([*SYSTEMCTL, "disable", "--now", UNIT_NAME])
            self.unit_path().unlink()
            self.run([*SYSTEMCTL, "daemon-reload"])
        if self.desktop_path().is_file():
            existed = True
            self.desktop_path().unlink()
        return existed

    def start(self) -> None:
        if self.unit_path().is_file() and self._systemd():
            _check(self.run([*SYSTEMCTL, "start", UNIT_NAME]))
        else:
            _server().start_background()


def _backend():
    if _WINDOWS:
        return WindowsService()
    if sys.platform == "darwin":
        return LaunchAgent()
    return LinuxService()


# ===== service operations =====

def _await_server(timeout: float = 10.0) -> dict:
    import time
    deadline = time.monotonic() + timeout
    while True:
        info = _server().server_info()
        if info is not None:
            return info
        if time.monotonic() >= deadline:
            raise wb.WorkflowError(f"the WorkBoard server did not answer within {timeout:g}s; "
                                   f"see {wb.logs_dir() / 'server.log'}", 503, "state")
        time.sleep(0.2)


def service_status(backend=None) -> dict:
    status = (backend or _backend()).status()
    info = _server().server_info()
    return {**status, "running": info is not None, "server": info,
            "versionMatch": None if info is None else info.get("version") == __version__}


def service_install(backend=None) -> dict:
    """Idempotent: write/register the definition, then make sure the server answers."""
    backend = backend or _backend()
    before = backend.status()
    changed = backend.install()
    if _server().server_info() is None:
        backend.start()
    action = "installed" if not before["installed"] else "updated" if changed else "unchanged"
    return {**backend.status(), "action": action, "running": True, "server": _await_server()}


def service_remove(backend=None) -> dict:
    backend = backend or _backend()
    status = backend.status()
    existed = backend.remove()
    stopped = _server().stop()
    return {**status, "installed": False, "current": False,
            "action": "removed" if existed else "absent", "stopped": stopped}


def service_restart(backend=None) -> dict:
    backend = backend or _backend()
    server = _server()
    if not server.stop():
        raise wb.WorkflowError("the running WorkBoard server did not stop within 10s", 409, "state")
    status = backend.status()
    if status["installed"]:
        backend.start()
    else:
        server.start_background()
    return {**status, "action": "restarted", "running": True, "server": _await_server()}


# ===== commands =====

def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _server_text(info) -> str:
    if not info:
        return "server not running"
    return (f"server v{info.get('version')} at {_server().server_url(info.get('port'))} "
            f"(pid {info.get('pid')})")


def _service_line(result: dict) -> str:
    action = result.get("action")
    where = f"{result['kind']} {result['location']}"
    if action is None:
        state = ("installed" + ("" if result["current"] else " (outdated: run `workboard service install`)")
                 if result["installed"] else "not installed")
        return f"service: {state} · {where} · {_server_text(result.get('server'))}"
    if action == "absent":
        return f"service: not installed · {where}"
    if action == "removed":
        return f"service: removed · {where} · " + ("server stopped" if result["stopped"] else "server still running")
    return f"service: {action} · {where} · {_server_text(result.get('server'))}"


_SKILL_NOTES = {"foreign": " (another skill lives here; left alone)",
                "missing": " (not installed; --refresh only updates existing copies)"}


def _skill_line(target: dict) -> str:
    action = target.get("action", target.get("state"))
    note = _SKILL_NOTES.get(target.get("state"), "") if action == "skipped" else ""
    if action == "kept":
        note = " (SKILL.md is not the workboard skill; left alone)"
    return f"{action:<9} {target['path']}{note}"


def cmd_setup(args) -> None:
    result, lines = {"ok": True}, []
    if not args.no_skills:
        result["skills"] = skills_install()
        lines.append("skills: " + "; ".join(f"{t['action']} {t['path']}" for t in result["skills"]))
    if not args.no_service:
        result["service"] = service_install()
        lines.append(_service_line(result["service"]))
    if args.json:
        _print_json(result)
    else:
        print("\n".join(lines) or "nothing to do: --no-skills and --no-service were both given")


def cmd_skills(args) -> None:
    if args.action == "install":
        targets = skills_install(refresh=args.refresh)
    elif args.action == "remove":
        targets = skills_remove()
    else:
        targets = skills_status()
    if args.json:
        _print_json({"ok": True, "action": args.action, "targets": targets})
    else:
        print("\n".join(_skill_line(target) for target in targets))


def cmd_service(args) -> None:
    operation = {"install": service_install, "remove": service_remove,
                 "status": service_status, "restart": service_restart}[args.action]
    result = operation()
    if args.json:
        _print_json({"ok": True, **result})
    else:
        print(_service_line(result))


def register(add) -> None:
    from .cli import _global_arguments

    p = add("setup", cmd_setup, "install the agent skills and the background server service")
    p.add_argument("--no-skills", action="store_true", help="skip the agent skill install")
    p.add_argument("--no-service", action="store_true", help="skip the background service")

    p = add("skills", cmd_skills, "install, refresh, remove, or inspect the agent skill")
    actions = p.add_subparsers(dest="action", required=True)
    for name, text in (("install", "write the bundled skill for every agent harness"),
                       ("remove", "delete installed workboard skill directories"),
                       ("status", "report each skill target: current, stale, missing, or foreign")):
        action = actions.add_parser(name, help=text, allow_abbrev=False)
        _global_arguments(action)
        if name == "install":
            action.add_argument("--refresh", action="store_true", help="only update copies that already exist")

    p = add("service", cmd_service, "manage the per-user background server service")
    actions = p.add_subparsers(dest="action", required=True)
    for name, text in (("install", "register and start the service (idempotent)"),
                       ("remove", "stop the server and delete the service definition"),
                       ("status", "service definition and running server"),
                       ("restart", "stop the server and start it again")):
        _global_arguments(actions.add_parser(name, help=text, allow_abbrev=False))
