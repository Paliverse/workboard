# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""The usage log: records per request, opt-out, unwritable logs, concurrent appends, redaction."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_env, run, scratch

from workboard import __version__, usage


def records(home: Path) -> list[dict]:
    lines = [line for path in sorted((home / ".workboard" / "logs").glob("usage-*.jsonl"))
             for line in path.read_text(encoding="utf-8").splitlines()]
    return [json.loads(line) for line in lines]


class UsageLog(unittest.TestCase):
    def setUp(self):
        self.base = self.enterContext(scratch("wb-usage-"))
        self.project = self.base / "project"
        self.project.mkdir()

    def board(self, home: Path, **extra) -> dict:
        env = make_env(home, **extra)
        created = run(["init", "usage", "--json"], cwd=self.project, env={**env, "WORKBOARD_USAGE_LOG": "0"})
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        return env

    def commands(self, env: dict) -> list[subprocess.CompletedProcess]:
        requests = "\n".join(json.dumps(request) for request in (
            {"op": "show", "card": 1}, {"op": "show", "card": 1, "text": "x"}))
        return [run(["add", "--title", "Logged", "--json"], cwd=self.project, env=env),
                run(["call"], cwd=self.project, env=env, input=requests)]

    def test_one_shot_and_call_records(self):
        home = self.base / "home"
        launched = time.time_ns() // 100 + 116444736000000000 - 10_000_000  # 1 s before now
        env = self.board(home, WORKBOARD_T0=str(launched))
        self.assertFalse((home / ".workboard" / "logs").exists(), "WORKBOARD_USAGE_LOG=0 made the logs folder")
        added, called = self.commands(env)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(called.returncode, 1, called.stdout + called.stderr)
        cli, show, refused = records(home)
        self.assertEqual([(r["entry"], r["op"], r["seq"], r["ok"]) for r in (cli, show, refused)],
                         [("cli", "add", 0, True), ("call", "show", 0, True), ("call", "show", 1, False)])
        self.assertEqual(cli["v"], __version__)
        self.assertGreaterEqual(cli["t"]["boot"], 1000)
        self.assertNotEqual(cli["pid"], show["pid"])
        self.assertEqual(show["pid"], refused["pid"])
        self.assertNotIn("Logged", json.dumps([cli, show, refused]))

    def test_caller_text_never_reaches_the_log(self):
        home = self.base / "home"
        env = self.board(home)
        run(["add", "--title", "Logged"], cwd=self.project, env={**env, "WORKBOARD_USAGE_LOG": "0"})
        requests = "\n".join(json.dumps(request) for request in (
            {"op": "note 1 rotated prod password hunter2"},
            {"op": "note", "card": 1, "text": "ok", "rotate SECRETTOKEN123 now": 1},
            {"op": "fly", "card": 1, "column": "task", "x": "rotate SECRETTOKEN123"}))
        procs = [run(["note", "1", "Fixed", "the", "login", "bug", "by", "rotating", "SECRETTOKEN123"],
                     cwd=self.project, env=env),
                 run(["fly", "1", "task", "--to=SECRETTOKEN123"], cwd=self.project, env=env),
                 run(["call"], cwd=self.project, env=env, input=requests)]
        self.assertEqual([proc.returncode for proc in procs], [2, 2, 1])
        logged = records(home)
        text = json.dumps(logged)
        for secret in ("SECRETTOKEN123", "hunter2", "login bug", "rotat"):
            self.assertNotIn(secret, text)
        self.assertEqual([(r["op"], r.get("keys", [])) for r in logged],
                         [("note", []), ("fly", ["to"]), ("?", []), ("note", ["card", "text"]),
                          ("fly", ["card", "column", "x"])])
        self.assertEqual([r["err"] for r in logged[:2]], ["unrecognized arguments: …"] * 2)

    def test_opt_out_and_unwritable_logs_change_nothing(self):
        direct = "from workboard import usage\nusage.begin('cli', op='direct')\nusage.end(ok=True)\nprint('{}')\n"
        outcomes = []
        for name in ("plain", "blocked", "off"):
            home = self.base / name
            env = self.board(home, **({"WORKBOARD_USAGE_LOG": "0"} if name == "off" else {}))
            if name == "blocked":
                (home / ".workboard" / "logs").write_text("not a folder", encoding="utf-8")
            procs = [*self.commands(env), subprocess.run([sys.executable, "-c", direct], cwd=str(self.project),
                                                         env=env, capture_output=True, text=True)]
            outcomes.append([(proc.returncode, proc.stderr, [json.loads(line).get("ok")
                                                              for line in proc.stdout.splitlines()])
                             for proc in procs])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0], outcomes[2])
        self.assertEqual([outcome[0] for outcome in outcomes[0]], [0, 1, 0])
        self.assertEqual(records(self.base / "plain")[-1]["op"], "direct")
        self.assertEqual((self.base / "blocked" / ".workboard" / "logs").read_text(encoding="utf-8"),
                         "not a folder")
        self.assertFalse((self.base / "off" / ".workboard" / "logs").exists(), "WORKBOARD_USAGE_LOG=0 wrote")

    def test_concurrent_appends_stay_whole(self):
        home = self.base / "home"
        logs = home / ".workboard" / "logs"
        logs.mkdir(parents=True)
        expired, kept = logs / "usage-2000-01-01.jsonl", logs / time.strftime(
            "usage-%Y-%m-%d.jsonl", time.gmtime(time.time() - 86400))
        expired.write_text("{}\n", encoding="utf-8")
        kept.touch()
        script = ("import os\n"
                  "from workboard import usage\n"
                  "assert 'WORKBOARD_T0' not in os.environ\n"
                  "usage.begin('cli')  # replaced unwritten, as a call batch replaces main's record\n"
                  "for i in range(500):\n"
                  "    usage.begin('call', op='note', board='x' * (i * 7 % 1500))\n"
                  "    usage.end(ok=True)\n")
        launched = time.time_ns() // 100 + 116444736000000000 - 10_000_000  # 1 s before now
        writers = [subprocess.Popen([sys.executable, "-c", script], cwd=str(self.base),
                                    env=make_env(home, WORKBOARD_T0=str(launched))) for _ in range(8)]
        self.assertEqual([writer.wait(timeout=120) for writer in writers], [0] * 8)
        by_pid = {}
        for record in records(home):
            by_pid.setdefault(record["pid"], []).append(record)
        self.assertEqual(len(by_pid), 8)
        for logged in by_pid.values():
            self.assertEqual([record["seq"] for record in logged], list(range(500)))
            self.assertEqual([seq for seq, record in enumerate(logged) if "boot" in record.get("t", {})], [0])
            self.assertGreaterEqual(logged[0]["t"]["boot"], 1000)
        self.assertEqual((expired.exists(), kept.exists()), (False, True), "creating a day file prunes old ones")

    def test_redaction_and_caller_identity(self):
        cases = {"received 'show 12'": "received '…'",
                 '"summary":"Fixed the parser crash"': '"summary":"…"',
                 "no card matching 'Fix login redirect loop'": "no card matching '…'",
                 "invalid choice: 'help'": "invalid choice: …",
                 "argument --to: invalid choice: 'SECRET' (choose from task)": "argument --to: invalid choice: … "
                                                                                "(choose from task)",
                 "argument --rev: invalid int value: \"it's\"": "argument --rev: invalid int value: …",
                 "unrecognized arguments: bug by rotating SECRET": "unrecognized arguments: …",
                 "ambiguous option: --no=SECRET could match --note, --notes": "ambiguous option: …",
                 'unknown op "notehunter2xyz"; did you mean note?': "unknown op …",
                 "unknown fields: hunter2secret; fields take any card key": "unknown fields: …",
                 '"text" (use "terms")': '"text" (use "terms")',
                 "no board named 'ompbridge'": "no board named 'ompbridge'",
                 "card's owner isn't 'Ada'": "card's owner isn't 'Ada'"}
        self.assertEqual({raw: usage._redact(raw) for raw in cases}, cases)
        self.assertEqual(len(usage._redact("x" * 500)), 200)
        sessions = Path("sessions") / "2026-11-14T08-55-10-002Z_01a1d9c0"
        identities = [
            ({"PI_SESSION_FILE": f"{sessions}.jsonl", "PI_TOOL_CALL_ID": "toolu_1", "CLAUDECODE": "1"},
             {"host": "omp", "session": f"{sessions}.jsonl", "role": "main", "agent": None, "toolCall": "toolu_1"}),
            ({"PI_SESSION_FILE": str(sessions / "BugHunter.jsonl")},
             {"host": "omp", "session": str(sessions / "BugHunter.jsonl"), "role": "sub",
              "agent": "BugHunter", "toolCall": None}),
            ({"CODEX_THREAD_ID": "t1", "CODEX_TOOL_CALL_ID": "c1"}, {"host": "codex", "session": "t1", "toolCall": "c1"}),
            ({"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDECODE": "1"}, {"host": "claude", "session": "s1"}),
            ({"OMPCODE": "1", "CLAUDECODE": "1"}, {"host": "omp"}),
            ({"AGENT": "1"}, {"host": "agent"}),
            ({}, {"host": None}),
        ]
        for env, expected in identities:
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(usage._identity(), expected)


if __name__ == "__main__":
    unittest.main()
