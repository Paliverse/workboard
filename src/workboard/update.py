# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Version reporting, update checks, and channel-aware upgrades."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from . import core as wb
from . import install

REPO = "Paliverse/workboard"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_DOWNLOADS = f"https://github.com/{REPO}/releases/latest/download"
RECEIPT = "install-receipt.json"
CHANNELS = ("npm", "brew", "winget", "scoop", "pipx", "uv", "pip", "script", "source", "unknown")
_COMMANDS = {
    "npm": ["npm", "install", "-g", "workboard@latest"],
    "brew": ["brew", "upgrade", "workboard"],
    "winget": ["winget", "upgrade", "--id", "Paliverse.WorkBoard", "--exact"],
    "scoop": ["scoop", "update", "workboard"],
    "pipx": ["pipx", "upgrade", "workboard"],
    "uv": ["uv", "tool", "upgrade", "workboard"],
}
_UNSUPPORTED = {
    "source": "this WorkBoard runs from a source checkout; update it with `git pull` instead",
    "unknown": ("cannot tell how this WorkBoard was installed; reinstall it from "
                f"https://github.com/{REPO}/releases or through your package manager"),
}


# ===== channels =====

def channel_for(location: str, *, frozen: bool) -> str:
    """Classify an install by path: the executable when frozen, else the package directory."""
    path = "/" + location.replace("\\", "/").lower().strip("/") + "/"
    if frozen:
        if "/node_modules/" in path:  # Before brew: npm's global prefix may live under Homebrew.
            return "npm"
        if "/microsoft/winget/" in path:
            return "winget"
        if "/scoop/apps/" in path:
            return "scoop"
        if "/cellar/" in path or "homebrew" in path or "linuxbrew" in path:
            return "brew"
        return "unknown"
    if "/pipx/venvs/" in path:
        return "pipx"
    if "/uv/tools/" in path:
        return "uv"
    return "pip"


def detect_channel() -> str:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if (executable.parent / RECEIPT).is_file():
            return "script"
        return channel_for(str(executable), frozen=True)
    package = Path(__file__).resolve().parent
    if package.parent.name == "src" and (package.parent.parent / "pyproject.toml").is_file():
        return "source"
    return channel_for(str(package), frozen=False)


def upgrade_command(channel: str) -> list[str] | None:
    if channel == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", "workboard"]
    if channel == "script":
        if install._WINDOWS:
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                    f"irm {RELEASE_DOWNLOADS}/install.ps1 | iex"]
        return ["sh", "-c", f"curl -fsSL {RELEASE_DOWNLOADS}/install.sh | sh"]
    return _COMMANDS.get(channel)


def display(argv: list[str]) -> str:
    if install._WINDOWS:
        import subprocess
        return subprocess.list2cmdline(argv)
    import shlex
    return shlex.join(argv)


# ===== version =====

def _version_key(value: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def latest_version(timeout: float = 5.0) -> str:
    import http.client
    import urllib.request
    request = urllib.request.Request(LATEST_RELEASE_API, headers={
        "User-Agent": f"workboard/{__version__}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            release = json.loads(response.read(1024 * 1024))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise wb.WorkflowError(f"cannot check GitHub for the latest release: {exc}", 503, "io") from exc
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        raise wb.WorkflowError("GitHub's latest-release response has no tag", 502, "io")
    return tag.strip().removeprefix("v")


def cmd_version(args) -> None:
    channel = detect_channel()
    result = {"ok": True, "version": __version__, "channel": channel}
    if args.json:
        import platform
        result.update(python=platform.python_version(), platform=platform.platform(),
                      executable=sys.executable)
    if args.check:
        latest = latest_version()
        result.update(latest=latest, updateAvailable=_version_key(latest) > _version_key(__version__))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return
    line = f"workboard {__version__} ({channel})"
    if args.check:
        line += f" · latest {result['latest']} · " + (
            "update available: run `workboard upgrade`" if result["updateAvailable"] else "up to date")
    print(line)


# ===== upgrade =====

def _wait_for_exit(pid: int, timeout: float = 60.0) -> None:
    """Block until pid exits so a package manager can replace the files it had locked."""
    if install._WINDOWS:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE; None once it has exited
        if handle:
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            kernel32.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def _run_step(argv: list[str], stdout) -> None:
    import subprocess
    executable = shutil.which(argv[0])
    if executable is None:
        raise wb.WorkflowError(f"`{argv[0]}` is not on PATH; cannot run `{display(argv)}`", 404, "not_found")
    code = subprocess.run([executable, *argv[1:]], stdout=stdout).returncode
    if code:
        raise wb.WorkflowError(f"`{display(argv)}` failed with exit code {code}", 500, "io")


def _new_workboard() -> list[str]:
    found = shutil.which("workboard")
    if found:
        return [found]
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "workboard"]
    raise wb.WorkflowError("upgraded, but `workboard` is not on PATH; open a new terminal and run "
                           "`workboard skills install --refresh`", 404, "not_found")


def _new_version(new: list[str]) -> str:
    import subprocess
    try:
        proc = subprocess.run([*new, "version", "--json"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
        version = json.loads(proc.stdout.strip().splitlines()[-1])["version"]
    except (OSError, ValueError, IndexError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise wb.WorkflowError(f"the upgraded `{display(new)}` did not report its version: {exc}", 500, "io")
    return str(version)


def _hand_off(channel: str, args) -> None:
    """Windows + frozen: this process locks its own files, so a runtime copy runs the upgrade."""
    import subprocess
    executable = install.runtime_copy()
    argv = [str(executable), "upgrade", "--channel", channel, "--after-pid", str(os.getpid())]
    if args.json:
        argv.append("--json")
    child = subprocess.Popen(argv)  # Same console: the copy's output continues here.
    print(f"continuing upgrade from a temporary copy (pid {child.pid})…",
          file=sys.stderr if args.json else sys.stdout, flush=True)
    raise SystemExit(0)


def cmd_upgrade(args) -> None:
    channel = args.channel or detect_channel()
    command = upgrade_command(channel)
    if command is None:
        raise wb.WorkflowError(_UNSUPPORTED[channel], 422, "state")
    if (not args.dry_run and install._WINDOWS and getattr(sys, "frozen", False)
            and not install.in_runtime_copy()):
        _hand_off(channel, args)
    if args.after_pid:
        _wait_for_exit(args.after_pid)
    server = install._server()
    running = server.server_info()
    restart = install._backend().status()["installed"] or running is not None
    steps = ([f"stop the running server (v{running.get('version')}, pid {running.get('pid')})"]
             if running else [])
    steps += [display(command), "workboard skills install --refresh"]
    if restart:
        steps.append("workboard service restart")
    result = {"ok": True, "channel": channel, "dryRun": bool(args.dry_run), "command": command, "steps": steps}
    if not args.json:
        print(f"upgrade plan ({channel}):")
        print("\n".join(f"  {number}. {step}" for number, step in enumerate(steps, 1)))
    if args.dry_run:
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("dry run: nothing was executed")
        return
    stdout = sys.stderr if args.json else None  # Keep stdout to the final JSON line.
    if running and not server.stop():
        raise wb.WorkflowError("the running WorkBoard server did not stop; close it and retry", 409, "state")
    try:
        _run_step(command, stdout)
    except wb.WorkflowError:
        if running:  # Nothing changed: bring back what we stopped.
            with contextlib.suppress(wb.WorkflowError, OSError):
                install.service_restart()
        raise
    new = _new_workboard()
    version = _new_version(new)
    _run_step([*new, "skills", "install", "--refresh"], stdout)
    if restart:
        _run_step([*new, "service", "restart"], stdout)
    if args.json:
        print(json.dumps({**result, "from": __version__, "to": version}, ensure_ascii=False))
    else:
        print(f"upgrade complete: {__version__} -> {version}")


def register(add) -> None:
    p = add("version", cmd_version, "print the installed version and install channel")
    p.add_argument("--check", action="store_true", help="compare with the latest GitHub release")
    p = add("upgrade", cmd_upgrade, "upgrade WorkBoard through the channel that installed it")
    p.add_argument("--dry-run", action="store_true", help="print the plan without running anything")
    p.add_argument("--channel", choices=CHANNELS, help=argparse.SUPPRESS)
    p.add_argument("--after-pid", type=int, help=argparse.SUPPRESS)
