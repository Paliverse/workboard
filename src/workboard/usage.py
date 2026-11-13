# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Usage log: one JSON line per request in WORKBOARD_HOME/logs/usage-YYYY-MM-DD.jsonl (UTC day).

cli and server open a request with begin() and write it with end(); core and cli add stage times
and fields with stage() and note(), which do nothing outside a request. Records hold field names,
numbers and redacted errors, never card text. Logging never raises, never fsyncs and never fails
a board call. WORKBOARD_USAGE_LOG=0 writes nothing.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import __version__

KEEP_DAYS, KEEP_BYTES, DAY_BYTES = 60, 200 << 20, 50 << 20
_OP = re.compile(r"[a-z][a-z.\-]{0,31}")  # what a cli or call op may log; anything else is "?"
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{0,31}")  # request keys and options: names, never text
# The caller's text follows these (argparse's echo, an unknown op or field name), or is the value quoted: never logged.
_ECHO = re.compile(r"(unrecognized arguments:|ambiguous option:|unknown op|unknown fields:).*", re.S)
_REFUSED = re.compile(r"(invalid choice:|invalid \S+ value:|ignored explicit argument) "
                      r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")")
_FILETIME_UNIX = 116444736000000000  # 100 ns ticks from 1601-01-01 to 1970-01-01

_local = threading.local()  # .rec (open record), .t (stage ms), .t0 (perf_counter at begin)
_lock = threading.Lock()    # server threads share the handle and the seq counter
_out = {"key": None, "h": None, "seq": 0}
_caller: dict | None = None
_boot: float | None = None  # ms from the launcher to the first request, until a record carries it
_imported: float | None = None  # ms from the package import to the first request, likewise
try:  # The launcher's start (Windows FILETIME, UTC). Popped so child processes don't inherit it.
    _launched: int | None = int(os.environ.pop("WORKBOARD_T0", ""))
except ValueError:
    _launched = None

if os.name == "nt":
    import _winapi

    # FILE_APPEND_DATA without FILE_WRITE_DATA: the kernel appends each WriteFile whole, across
    # processes. Python's append modes seek then write: 16 writers lost 62-67% of lines in testing.
    def _open(path: str, create: bool):
        return _winapi.CreateFile(path, 0x0004, 0x7, 0, 4 if create else 3, 0x80, 0)

    _write, _close = _winapi.WriteFile, _winapi.CloseHandle
else:
    def _open(path: str, create: bool):
        return os.open(path, os.O_WRONLY | os.O_APPEND | (os.O_CREAT if create else 0), 0o600)

    _write, _close = os.write, os.close


def _identity() -> dict:
    """The calling agent harness, session and tool call, from the environment."""
    env = os.environ
    if path := env.get("PI_SESSION_FILE"):  # omp; a subagent's session file is named after it
        stem = os.path.splitext(os.path.basename(path))[0]
        main = stem[:4].isdigit() and "Z_" in stem
        return {"host": "omp", "session": path, "role": "main" if main else "sub",
                "agent": None if main else stem, "toolCall": env.get("PI_TOOL_CALL_ID")}
    if thread := env.get("CODEX_THREAD_ID"):
        return {"host": "codex", "session": thread, "toolCall": env.get("CODEX_TOOL_CALL_ID")}
    if session := env.get("CLAUDE_CODE_SESSION_ID"):
        return {"host": "claude", "session": session}
    # omp also sets CLAUDECODE, so OMPCODE goes first.
    host = "omp" if env.get("OMPCODE") else "claude" if env.get("CLAUDECODE") else "agent" if env.get("AGENT") else None
    return {"host": host}


def _redact(message: str) -> str:
    """argparse's echo of the arguments and the values it quotes become '…', as do quoted spans with
    whitespace or over 24 characters; apostrophes don't pair."""
    message = _REFUSED.sub(r"\1 …", _ECHO.sub(r"\1 …", message))
    return re.sub(r"(?<!\w)'[^'\n]*'(?!\w)|\"[^\"\n]*\"",
                  lambda m: m[0] if len(m[0]) <= 26 and not any(c.isspace() for c in m[0])
                  else m[0][0] + "…" + m[0][0], message)[:200]


def _merge(rec: dict, fields: dict) -> None:
    rec.update((key, value) for key, value in fields.items() if value is not None)


def begin(entry: str, **fields) -> None:
    """Open this thread's request record; entry is "cli", "call" or "http"."""
    global _caller, _launched, _boot, _imported
    _local.rec = _local.t = None
    try:
        if os.environ.get("WORKBOARD_USAGE_LOG") == "0":
            return
        rec, t = {"entry": entry, "pid": os.getpid(), "seq": None}, {}
        if entry != "http":  # The server's environment belongs to whoever started it.
            if _caller is None:
                _caller = _identity()
                from . import _T0
                _imported = (time.perf_counter() - _T0) * 1000
            rec.update(_caller)
            rec["cwd"] = os.getcwd()
            if _launched is not None:  # launcher -> first request; the first record written carries it
                _boot = (time.time_ns() // 100 + _FILETIME_UNIX - _launched) / 1e4
                _launched = None
        _merge(rec, fields)
        _local.rec, _local.t, _local.t0 = rec, t, time.perf_counter()
    except Exception:
        pass


def stage(name: str, t0: float) -> None:
    """Add the milliseconds since perf_counter() value t0 to stage `name` of the open record."""
    t = getattr(_local, "t", None)
    if t is not None:
        try:
            t[name] = t.get(name, 0.0) + (time.perf_counter() - t0) * 1000
        except Exception:
            pass


def note(**fields) -> None:
    """Merge fields into the open record; None values are skipped."""
    rec = getattr(_local, "rec", None)
    if rec is not None:
        _merge(rec, fields)


def _prune(logs: str) -> None:
    files = sorted((entry.name, entry.stat().st_size, entry.path) for entry in os.scandir(logs)
                   if entry.name.startswith("usage-") and entry.name.endswith(".jsonl"))
    cutoff = time.strftime("usage-%Y-%m-%d", time.gmtime(time.time() - KEEP_DAYS * 86400))
    total = sum(size for _, size, _ in files)
    for name, size, path in files[:-1]:  # never the newest: the day file just created
        if name < cutoff or total > KEEP_BYTES:
            total -= size
            try:
                os.remove(path)
            except OSError:
                pass


def _handle(day: str):
    """This process's append handle for the day file; None while logging is off for the day."""
    key = (day, os.environ.get("WORKBOARD_HOME"))
    if _out["key"] == key:
        return _out["h"]
    old = _out["h"]
    _out.update(key=key, h=None)  # One attempt per day and home: a failure never retries per record.
    if old is not None:
        _close(old)
    from .core import logs_dir
    logs = str(logs_dir())
    path = os.path.join(logs, f"usage-{day}.jsonl")
    try:
        if os.stat(path).st_size > DAY_BYTES:  # ponytail: a runaway caller stops logging until the next UTC day
            return None
        _out["h"] = _open(path, False)
    except FileNotFoundError:
        os.makedirs(logs, exist_ok=True)
        _out["h"] = _open(path, True)
        try:
            _prune(logs)  # Only the process that creates a day file prunes.
        except OSError:
            pass
    return _out["h"]


def end(**fields) -> None:
    """Merge fields, set ms, append the record as one JSON line and close it. Never raises."""
    global _boot, _imported
    rec, t = getattr(_local, "rec", None), getattr(_local, "t", None)
    if rec is None:
        return
    _local.rec = _local.t = None
    try:
        ms = (time.perf_counter() - _local.t0) * 1000
        _merge(rec, fields)
        rec["ms"] = round(ms, 2)
        if rec["entry"] != "http":
            if _imported is not None:
                t, _imported = {"import": _imported, **t}, None
            if _boot is not None:
                t, _boot = {"boot": _boot, **t}, None
        if "err" in rec:
            rec["err"] = _redact(str(rec["err"]))
        op = rec.get("op")  # The server builds its op from its routes; cli and call ops come from the caller.
        if rec["entry"] != "http" and op is not None and not (isinstance(op, str) and _OP.fullmatch(op)):
            rec["op"] = "?"
        for name in ("keys", "refused"):
            if name in rec:
                rec[name] = [key for key in rec[name] if isinstance(key, str) and _KEY.fullmatch(key)]
        if t:
            rec["t"] = {name: round(value, 2) for name, value in t.items()}
        now = time.time()
        stamp = time.gmtime(now)
        with _lock:
            if rec["seq"] is None:
                rec["seq"] = _out["seq"]
            _out["seq"] += 1
            line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", stamp) + f".{int(now % 1 * 1000):03d}Z",
                               "v": __version__, **{k: v for k, v in rec.items() if v is not None}},
                              ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            if (h := _handle(time.strftime("%Y-%m-%d", stamp))) is not None:
                _write(h, line.encode("utf-8"))
    except Exception:
        pass  # The usage log never fails a board call.
