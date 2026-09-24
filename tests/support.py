"""Shared helpers for WorkBoard's stdlib integration tests.

Every test runs against a scratch home: HOME, USERPROFILE, WORKBOARD_HOME,
APPDATA, LOCALAPPDATA and the XDG directories all point inside a temporary
directory, so no test can read or write the real user's registry, skills or
service definitions.

Set WORKBOARD_TEST_COMMAND (for example to a built binary path) to run the
CLI-level tests against something other than ``python -m workboard``.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_CLEARED = ("WORKBOARD_SCOPE_ROOT", "WORKBOARD_DEFAULT_BOARD", "WORKBOARD_ACTOR",
            "WORKBOARD_PORT", "WORKBOARD_HOME")


def _idle_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Scratch environments never default to 7891, where the developer's own server may run.
PORT = _idle_port()


def command() -> list[str]:
    override = os.environ.get("WORKBOARD_TEST_COMMAND")
    if override:
        return shlex.split(override, posix=os.name != "nt")
    return [sys.executable, "-m", "workboard"]


def make_env(home: Path, **extra: str) -> dict:
    """Environment for a child process confined to the scratch ``home``."""
    home = Path(home)
    env = os.environ.copy()
    for key in _CLEARED:
        env.pop(key, None)
    env.update(
        HOME=str(home), USERPROFILE=str(home), WORKBOARD_HOME=str(home / ".workboard"),
        APPDATA=str(home / "AppData" / "Roaming"), LOCALAPPDATA=str(home / "AppData" / "Local"),
        XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        WORKBOARD_ACTOR="tester", WORKBOARD_PORT=str(PORT),
    )
    if not os.environ.get("WORKBOARD_TEST_COMMAND"):
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(SRC), os.environ.get("PYTHONPATH"))))
    env.update({key: str(value) for key, value in extra.items()})
    return env


@contextlib.contextmanager
def scratch(prefix: str = "wb-test-"):
    """Yield a resolved temporary directory and always remove it."""
    base = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run(args, *, cwd, env: dict, input: str | None = None,
        timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run the WorkBoard CLI; capture UTF-8 text output."""
    return subprocess.run(
        [*command(), *(str(arg) for arg in args)], cwd=str(cwd), env=env,
        input=input, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
        stdin=None if input is not None else subprocess.DEVNULL,
    )


def last_json(proc: subprocess.CompletedProcess) -> dict:
    """Parse the last non-empty stdout line as JSON (--json postconditions)."""
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"no stdout; stderr={proc.stderr!r}")
    return json.loads(lines[-1])
