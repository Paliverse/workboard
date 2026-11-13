# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""JSON stdin calls and read-only card/subtask waits, including binary contracts."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import command, last_json, make_env, run, scratch
from workboard import cli, core


def old_digest(p, by: str, as_json: bool) -> str:
    """digest as it ran before SQL narrowing, from every card: the output the narrowed digest must match."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        doc = core.load(p)
        ready = core.ready_cards(doc)
        contributions = [{"num": c["num"], "cardId": c["id"], "subtaskId": st["id"],
                          "text": st["text"], "column": c["column"], **st["delegation"]}
                         for c in doc["cards"] for st, _ in core.iter_subtasks(c["subtasks"])
                         if (st.get("delegation") or {}).get("owner") == by
                         and (c["column"] != "done" or st["delegation"]["state"] == "claimed")]
        counts = {}
        for c in doc["cards"]:
            counts[c["column"]] = counts.get(c["column"], 0) + 1
        mine = [card for card in doc["cards"] if core.card_holder(card) == by]
        if as_json:
            print(json.dumps({
                "ok": True, "board": str(p), "name": doc["name"], "schemaVersion": doc["schemaVersion"],
                "rev": doc["rev"], "savedAt": doc["savedAt"], "stats": core.board_stats(doc),
                "columns": [{**column, "count": counts.get(column["id"], 0)} for column in doc["columns"]],
                "mine": [{"num": card["num"], "title": card["title"], "column": card["column"]} for card in mine],
                "attention": [{"id": card["id"], "num": card["num"], "title": card["title"],
                               "owner": card.get("activeOwner"), "attention": attention} for card in doc["cards"]
                              if (attention := core.stage_attention(card))],
                "ready": [card["num"] for card in ready[:5]],
                "contributions": contributions,
            }, ensure_ascii=False))
            return out.getvalue()
        col_line = " · ".join(f"{c['name']}: {counts.get(c['id'], 0)}"
                              for c in doc["columns"] if counts.get(c["id"]))
        print(f"WorkBoard: {doc['name']} — rev {doc['rev']} · {len(doc['cards'])} cards"
              + (f" · saved {doc['savedAt']}" if doc.get("savedAt") else ""))
        if mine:
            print(f"  MINE @{by}:")
            for card in mine[:cli.DIGEST_ROWS]:
                print(f"    {core.fmt_ref(card)} {card['column']} {card['title'][:60]}")
            cli._more(mine, "query --mine", "    ")
        if contributions:
            print(f"  CONTRIBUTIONS @{by}:")
            for item in contributions[:cli.DIGEST_ROWS]:
                print(f"    #{item['num']}/{item['subtaskId']} {item['state']} {item['text'][:60]}"
                      f" (parent {item['column']})")
            cli._more(contributions, "context REF --mine", "    ")
        if col_line:
            print(f"  {col_line}")
        ip = [c for c in doc["cards"] if c["column"] == "inprogress"]
        for c in ip[:cli.DIGEST_ROWS]:
            subs = [s for s, _ in core.iter_subtasks(c["subtasks"])]
            prog = f" ☑{sum(1 for s in subs if s['done'])}/{len(subs)}" if subs else ""
            attention = core.stage_attention(c)
            marker = f" ⚠{attention}" if attention else ""
            owner = f" @{c['activeOwner']}" if c.get("activeOwner") else ""
            print(f"  IN PROGRESS: {core.fmt_ref(c)}{owner} {c['title'][:60]}{prog}"
                  f" ({cli._age(c['updatedAt'])}){marker}")
        cli._more(ip, "query --column inprogress")
        done_cols = {c["id"] for c in doc["columns"] if c["kind"] == "done"} | {"done"}
        shipped = sorted((c for c in doc["cards"] if c["column"] in done_cols and c["doneAt"]
                          and c.get("outcome") != "canceled"),
                         key=lambda c: c["doneAt"], reverse=True)[:3]
        for c in shipped:
            print(f"  SHIPPED: {core.fmt_ref(c)} {c['title'][:58]} ({cli._age(c['doneAt'])} ago)")
        blocked = [c for c in doc["cards"] if c["column"] == "blocked"]
        for c in blocked[:cli.DIGEST_ROWS]:
            reason = cli._short((c.get("blockedReason") or "reason missing").strip())
            until = cli._short((c.get("unblockWhen") or "condition missing").strip())
            owner = f" @{core.card_holder(c)}" if core.card_holder(c) else ""
            print(f"  BLOCKED: {core.fmt_ref(c)}{owner} {c['title'][:48]} — {reason}; until {until}")
        cli._more(blocked, "query --column blocked")
        canceled = [c for c in doc["cards"] if c.get("outcome") == "canceled"]
        for c in canceled[-3:]:
            print(f"  CANCELED: {core.fmt_ref(c)} {c['title'][:48]} — "
                  f"{cli._short((c.get('cancelReason') or '').strip())}")
        refs = " — " + " ".join(core.fmt_ref(card) for card in ready[:5]) if ready else ""
        print(f"  READY: {len(ready)}{refs} · REWORK: "
              f"{sum(bool(c.get('reworkReason')) for c in doc['cards'] if c['column'] != 'done')}")
        if not ip and not shipped:
            print("  (quiet board)")
    return out.getvalue()


class CallAndWait(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = self.stack.enter_context(scratch("wb-call-"))
        self.home = self.base / "home"
        self.home.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        self.env = make_env(self.home, WORKBOARD_ACTOR="Main")
        self.board = last_json(self.cli(["init", "calls", "--json"]))["board"]
        self.card = self.calls({"op": "add", "title": "Parent"})[0]

    def cli(self, args, **kwargs):
        return run(args, cwd=self.project, env=self.env, **kwargs)

    def calls(self, *requests, ok=True, array=False, pretty=False, defaults=()):
        text = (json.dumps(requests, indent=2 if pretty else None) if array else
                "\n".join(json.dumps(request, indent=2 if pretty else None) for request in requests))
        proc = self.cli([*defaults, "call"], input=text)
        self.assertEqual(proc.returncode, 0 if ok else 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), len(requests), proc.stdout)
        return [json.loads(line) for line in lines]

    def json(self, args):
        proc = self.cli([*args, "--json"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return last_json(proc)

    def test_note_text_overflow_edit_and_remove(self):
        long = "-first line " + "word " * 60 + "\nsecond line"
        added, both = self.calls({"op": "note", "card": 1, "text": long},
                                 {"op": "note", "card": 1, "text": "a", "summary": "b"}, ok=False)
        self.assertEqual((set(added["item"]), added["summaryMoved"]), ({"id", "at", "by"}, True))  # No echo.
        stored = self.calls({"op": "note.get", "id": added["item"]["id"][:8]})[0]
        self.assertEqual((stored["num"], stored["item"]["body"]), (1, long))
        self.assertLessEqual(len(stored["item"]["summary"]), 160)
        self.assertTrue(stored["item"]["summary"].endswith("…"))
        self.assertEqual(both["code"], "invalid")
        self.assertIn('"text"', both["error"])
        self.assertIn('"summary"', both["error"])
        words = self.calls({"op": "note", "card": 1, "text": "edit"}, {"op": "note", "card": 1, "text": "rm"})
        words.append(last_json(self.cli(["note", "1", "rm", "--json"])))
        read = self.calls(*[{"op": "note.get", "card": 1, "id": word["item"]["id"]} for word in words])
        self.assertEqual([r["item"]["summary"] for r in read], ["edit", "rm", "rm"])
        for word in words:
            self.calls({"op": "note.rm", "card": 1, "id": word["item"]["id"]})
        note_id = added["item"]["id"]
        edited, reread, foreign, removed, shown = self.calls(
            {"op": "note.edit", "card": 1, "id": note_id, "summary": "Short again"},
            {"op": "note.get", "id": note_id},
            {"op": "note.rm", "card": 1, "id": note_id, "actor": "Grace"},
            {"op": "note.rm", "card": 1, "id": note_id},
            {"op": "show", "card": 1, "full": True}, ok=False)
        self.assertEqual((reread["item"]["summary"], edited["summaryMoved"]), ("Short again", False))
        self.assertIn("editedAt", edited["item"])
        self.assertEqual(foreign["code"], "owned")
        self.assertIn("Main", foreign["error"])
        self.assertEqual((removed["ok"], shown["card"]["log"]), (True, []))
        self.assertEqual([e["ev"] for e in shown["card"]["history"][-2:]], ["note-edited", "note-removed"])

    def test_order_errors_and_card_scoped_stale(self):
        results = self.calls(
            {"op": "show", "card": 1},
            {"op": "start", "card": 404},
            {"op": "note", "card": 1, "summary": "new finding"},
            {"op": "start", "card": 1, "expectedRev": self.card["rev"]},
            {"op": "show", "card": 1}, ok=False)
        self.assertEqual([r["ok"] for r in results], [True, False, True, False, True])
        self.assertEqual(results[1]["code"], "not_found")
        self.assertEqual((results[3]["code"], results[3]["status"]), ("stale", 409))
        self.assertEqual(results[3]["card"]["changedRev"], results[2]["rev"])
        self.assertEqual(results[4]["card"]["log"][-1]["summary"], "new finding")
        self.assertEqual(results[4]["card"]["column"], "task")

    def test_text_options_and_free_positionals_are_lossless(self):
        text = '-heading "quoted" \'single\' 100% ^ & café\nsecond line\nthird line'
        note, comment, subtasks, shown, hits = self.calls(
            {"op": "note", "card": 1, "summary": "Special text", "body": text},
            {"op": "comment.add", "card": 1, "text": text},  # A comment is a note: long text moves to the body.
            {"op": "subtask.add", "card": 1, "texts": [text, "-another"]},
            {"op": "show", "card": 1, "full": True},
            {"op": "search", "terms": ["-heading", "100%"]})
        self.assertEqual([entry["id"] for entry in shown["card"]["log"][-2:]], [note["item"]["id"], comment["item"]["id"]])
        self.assertEqual([st["id"] for st in subtasks["items"]], [st["id"] for st in shown["card"]["subtasks"]])
        self.assertEqual([entry["body"] for entry in shown["card"]["log"][-2:]], [text, text])
        self.assertEqual([st["text"] for st in shown["card"]["subtasks"]], [text, "-another"])
        self.assertEqual([c["num"] for c in hits["cards"]], [1])
        self.calls({"op": "start", "card": 1},
                   {"op": "done", "card": 1, "writeup": text})
        self.assertEqual(self.calls({"op": "context", "card": 1})[0]["card"]["writeup"], text)
        self.calls({"op": "improve", "card": 1, "text": text})  # reopen as improve
        self.assertEqual(self.calls({"op": "context", "card": 1, "full": True})[0]["card"]["reopenReason"], text)
        if sys.version_info >= (3, 12):  # 3.11's argparse drops a literal "--" after "--" (fixed in 3.12).
            self.calls({"op": "comment.add", "card": 1, "text": "--"}, {"op": "subtask.add", "card": 1, "text": "--"})
            card = self.calls({"op": "context", "card": 1, "full": True})[0]["card"]
            self.assertEqual((card["log"][-1]["summary"], card["subtasks"][-1]["text"]), ("--", "--"))

    def test_arrays_pretty_stream_and_boolean_list_options(self):
        started, added = self.calls({"op": "start", "card": 1},
                                   {"op": "subtask.add", "card": 1, "text": "Worker", "delegated": True},
                                   array=True, pretty=True)
        sid = added["item"]["id"]
        configured, rescoped, note1, note2 = self.calls(
            {"op": "subtask.configure", "card": 1, "subtask": sid,
             "scope": ["src/a.py", "src/b.py"], "expectedRev": added["rev"]},
            {"op": "subtask.configure", "card": 1, "subtask": sid,
             "clearScope": False, "scope": ["src/c.py", "tests"], "reason": None},
            {"op": "note", "card": 1, "actor": "Peer", "summary": "one", "body": "first"},
            {"op": "note", "card": 1, "actor": "Peer", "summary": "two", "body": "second"}, pretty=True)
        self.assertEqual(configured["item"]["delegation"]["scope"], ["src/a.py", "src/b.py"])
        self.assertEqual(rescoped["item"]["delegation"]["scope"], ["src/c.py", "tests"])

    def test_defaults_and_request_actor_board_overrides(self):
        other = self.base / "other"
        other.mkdir()
        created = run(["init", "other", "--json"], cwd=other, env=self.env)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        added, overridden, defaulted = self.calls(
            {"op": "add", "board": "other", "actor": "Worker", "title": "Elsewhere"},
            {"op": "note", "card": 1, "board": "calls", "actor": "Peer", "summary": "override"},
            {"op": "note", "card": 1, "summary": "default"}, defaults=["--actor=Leader", "--board=calls"])
        self.assertEqual([r["actor"] for r in (added, overridden, defaulted)], ["Worker", "Peer", "Leader"])
        here = self.calls({"op": "show", "card": 1})[0]["card"]
        self.assertEqual([n["by"] for n in here["log"]], ["Peer", "Leader"])
        there = self.calls({"op": "show", "card": 1, "board": "other"})[0]["card"]
        self.assertEqual(there["title"], "Elsewhere")

    def test_invalid_request_and_argparse_envelopes(self):
        cli_only = ("init", "link", "serve", "open", "setup", "skills", "service", "upgrade", "version",
                    "doctor", "recover", "columns-core", "call")
        requests = [{"op": op} for op in (*cli_only, "wip")]
        requests += [
            {"op": "done", "card": 1, "writeupStdin": True},
            {"op": "note", "card": 1, "stdin": False},
            {"op": "note", "card": 1, "body-stdin": True},
            {"op": "note", "card": 1},
            {"op": "update", "card": 1, "priority": "wrong"},
            {"op": "note", "card": 1, "summary": "bad", "body": {"text": "not scalar"}},
            {"op": "start"}, "not an object", {"card": 1},
            {"op": "show", "card": 1, "notAnOption": True},
        ]
        results = self.calls(*requests, ok=False)
        for result in results:
            self.assertEqual((result["ok"], result["status"], result["code"], result["rev"]),
                             (False, 422, "invalid", None), result)
            self.assertNotIn("usage:", result["error"])
            self.assertTrue(result["error"])
        for request, result in zip(requests, results[:len(cli_only)]):
            self.assertTrue(result["error"].startswith(f"op {request['op']} runs only on the command line"), result)
        wip, stdin, missing, no_op, extra = (results[len(cli_only) + i] for i in (0, 1, 4, 9, 10))
        self.assertTrue(wip["error"].startswith('unknown op "wip"'), wip)
        self.assertTrue(wip["error"].endswith('{"op":"help"} lists every op'), wip)
        self.assertIn("text goes in the request", stdin["error"])
        self.assertEqual(missing["error"], 'note needs "text"')
        self.assertEqual(missing["usage"]["required"], ["card", "text"])
        self.assertIn('"op"', no_op["error"])
        self.assertEqual(extra["error"], 'context doesn\'t take "notAnOption"')

    def test_single_value_lists_empty_lists_and_missing_keys_name_request_keys(self):
        self.calls({"op": "start", "card": 1})
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["One", "Two"]})[0]
        one, two = [item["id"] for item in added["items"]]
        before = added["rev"]
        cases = [
            ({"op": "update", "card": 1, "notes": ["a", "b"]}, "notes"),
            ({"op": "note", "card": 1, "summary": "s", "subtask": [one, two]}, "subtask"),
            ({"op": "show", "card": [1, 2]}, "card"),
            ({"op": "subtask.configure", "card": 1, "subtask": one, "scope": []}, "clearScope"),
            ({"op": "update", "card": 1, "addTag": []}, "addTag"),
            ({"op": "subtask.claim", "card": 1}, "subtask"),
            ({"op": "subtask.add", "card": 1}, "text"),
            ({"op": "start"}, "card"),
            ({"op": "comment.add", "card": 1}, "text"),
            ({"op": "block", "card": 1, "until": "later"}, "text"),
            ({"op": "note", "card": 1}, "text"),
            ({"op": "context", "card": 1, "id": one}, "subtask"),
            ({"op": "wait", "card": 1, "subtasks": [one]}, "subtask"),
            ({"op": "context", "card": "-h"}, "card"),
            ({"op": "attachment.get", "card": 1, "id": "-h", "out": "copy.txt"}, "id"),
            ({"op": "subtask.configure", "card": 1, "subtask": one, "reviewRequired": True}, "reviewRequired"),
            ({"op": "subtask.add", "card": 1, "text": "Child", "parent": one}, "parent"),
            ({"op": "subtask.edit", "card": 1, "subtask": "s-1", "text": "x"}, "subtask.edit"),
            ({"op": "comment.5", "card": 1, "text": "x"}, "comment.5"),
            ({"op": "done", "card": 1}, '"text"'),
        ]
        *results, shown = self.calls(*[request for request, _ in cases], {"op": "show", "card": 1}, ok=False)
        for (request, key), result in zip(cases, results):
            self.assertEqual((result["ok"], result["status"], result["code"], result["rev"]),
                             (False, 422, "invalid", None), (request, result))
            self.assertIn(key, result["error"], request)
            self.assertNotIn("--", result["error"], request)
        self.assertTrue(shown["ok"], "a value starting with '-' must not end the stream")
        moved = next(result for (request, _), result in zip(cases, results) if request.get("id") == one)
        self.assertEqual(moved["fix"], {"op": "context", "card": 1, "subtask": one})
        self.assertEqual(shown["rev"], before)

    def test_query_and_list_columns_take_aliases_and_refuse_unknown_names(self):
        self.calls({"op": "start", "card": 1}, {"op": "add", "title": "Still waiting"})
        for column in ("in-progress", "In Progress", "#inprogress"):
            queried = self.calls({"op": "query", "column": column})[0]
            self.assertEqual([card["num"] for card in queried["cards"]], [1], column)
            listed = self.json(["list", "--column", column])
            self.assertEqual([card["num"] for card in listed["cards"]], [1], column)
        for command in ("query", "list"):
            proc = self.cli([command, "--column", "doing", "--json"])
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual((last_json(proc)["status"], last_json(proc)["code"]), (422, "invalid"))

    def test_query_and_list_match_a_boards_own_legacy_column(self):
        with core.board_transaction(self.board) as doc:
            doc["columns"].append({"id": "review", "name": "Review"})
            doc["cards"][0]["column"] = "review"
            core.save(self.board, doc)
        self.assertEqual([card["num"] for card in self.calls({"op": "query", "column": "review"})[0]["cards"]], [1])
        self.assertEqual([card["num"] for card in self.json(["list", "--column", "review"])["cards"]], [1])

    def test_add_into_in_progress_runs_start_checks(self):
        started = self.calls({"op": "add", "title": "Started", "column": "inprogress"})[0]
        self.assertEqual((started["column"], started["activeOwner"]), ("inprogress", "Main"))
        before = core.board_rev(self.board)
        waiting, shipped = self.calls({"op": "add", "title": "Waiting", "column": "in-progress", "on": 1},
                                      {"op": "add", "title": "Shipped", "column": "done"}, ok=False)
        self.assertEqual((waiting["status"], waiting["code"]), (409, "deps"))
        self.assertEqual((shipped["status"], shipped["code"]), (409, "state"))
        self.assertEqual(core.board_rev(self.board), before)

    def test_add_pins_notes_from_the_request_or_stdin(self):
        added = self.calls({"op": "add", "title": "Pinned", "notes": "Acceptance: one\nAcceptance: two"})[0]
        piped = self.cli(["add", "--title", "Piped", "--notes-stdin", "--json"], input="Acceptance: piped\n")
        self.assertEqual(piped.returncode, 0, piped.stdout + piped.stderr)
        request, stdin = self.calls({"op": "show", "card": added["num"], "full": True},
                                    {"op": "show", "card": last_json(piped)["num"], "full": True})
        self.assertEqual(request["card"]["notes"], "Acceptance: one\nAcceptance: two")
        self.assertEqual(stdin["card"]["notes"], "Acceptance: piped")
        both = self.cli(["add", "--title", "Both", "--origin-stdin", "--notes-stdin"], input="text")
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)

    def test_search_projects_fields_and_keeps_the_newest_hits(self):
        self.calls(*[{"op": "add", "title": f"Needle {n}"} for n in range(3)])
        default, limited, unknown, zero = self.calls(
            {"op": "search", "terms": ["needle"]},
            {"op": "search", "terms": ["needle"], "limit": 2, "fields": "num,id,owner"},
            {"op": "search", "terms": ["needle"], "fields": "num,nope"},
            {"op": "search", "terms": ["needle"], "limit": 0}, ok=False)
        self.assertEqual(default["cards"], [{"num": n + 2, "title": f"Needle {n}", "column": "task"}
                                            for n in range(3)])
        self.assertEqual([card["num"] for card in limited["cards"]], [3, 4])
        self.assertEqual(set(limited["cards"][0]), {"num", "id", "owner"})
        for result in (unknown, zero):
            self.assertEqual((result["status"], result["code"]), (422, "invalid"))

    def test_handoff_is_slim_takes_over_and_runs_through_call(self):
        added = self.calls({"op": "start", "card": 1},
                           {"op": "subtask.add", "card": 1, "text": "Review", "delegated": True})[1]
        sid = added["item"]["id"]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Ada"})
        request = {"op": "handoff", "card": 1, "subtask": sid, "worker": "Grace"}
        handoff = self.calls(request)[0]
        self.assertEqual(set(handoff), {"ok", "rev", "num", "worker", "previousOwner", "writeScope",
                                        "scopeWarnings", "requests", "prompt"})
        self.assertEqual((handoff["previousOwner"], handoff["writeScope"]), ("Ada", []))
        self.assertEqual(handoff["requests"]["claim"], {"op": "subtask.takeover", "card": 1, "subtask": sid,
                                                        "text": "{reason}", "board": "calls", "actor": "Grace"})
        for text in ("read-only: edit no files", "{reason}", "`workboard call`"):
            self.assertIn(text, handoff["prompt"])

        def printed(handoff):  # Each request is its own prompt line, in order, as call takes it.
            lines = [line for line in handoff["prompt"].splitlines() if line.startswith("{")]
            self.assertEqual(lines, [json.dumps(r, ensure_ascii=False) for r in handoff["requests"].values()])
            return dict(zip(handoff["requests"], lines))

        steps = printed(handoff)
        text = self.cli(["handoff", "1", "--subtask", sid, "--worker", "Grace"])
        self.assertEqual(text.stdout, handoff["prompt"])
        for option, value in (("--write-scope", "src"), ("--fields", "prompt"), ("--peer", "Ada=agent://x")):
            removed = self.cli(["handoff", "1", "--subtask", sid, "--worker", "Grace", option, value])
            self.assertEqual(removed.returncode, 2, option)
        worker = ["call"]  # The requests name their board and actor; no shell-quoted names on the command line.
        pasted = (steps["read"], steps["claim"].replace("{reason}", "Main reassigned the review"),
                  steps["publish"].replace("{summary}", "Checked").replace("{body}", "Evidence"))
        taken = self.cli(worker, input="\n".join(pasted) + "\n")
        self.assertEqual(taken.returncode, 0, taken.stdout + taken.stderr)
        self.assertEqual(json.loads(taken.stdout.splitlines()[1])["item"]["delegation"]["owner"], "Grace")
        again = self.calls(request)[0]
        self.assertIsNone(again["previousOwner"])
        self.assertEqual(again["requests"]["claim"],
                         {"op": "subtask.claim", "card": 1, "subtask": sid, "board": "calls", "actor": "Grace"})
        self.assertNotIn("{reason}", again["prompt"])
        self.assertLess(len(again["prompt"]), 1500)
        complete = printed(again)["complete"].replace("{result}", "Verified\\nby the review")
        completed = self.cli(worker, input=complete + "\n")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(last_json(completed)["item"]["delegation"]["state"], "completed")
        done = self.calls({"op": "context", "card": 1, "subtask": sid})[0]["card"]["subtasks"][0]
        self.assertEqual(done["delegation"]["result"], "Verified\nby the review")

    def test_empty_and_malformed_input(self):
        for text in ("", " \n\t", '{"op":', '{"op":"show"}\nnot JSON', "{op:context,card:1}"):
            proc = self.cli(["call"], input=text)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            result = last_json(proc)
            self.assertEqual((result["code"], result["status"], result["rev"]), ("invalid", 422, None))
            self.assertEqual(len(result["error"].splitlines()), 1)
            if '"' not in text:  # Nothing arrived, or a shell ate the quotes: show the heredoc form.
                self.assertIn("workboard call <<'EOF'", result["error"])
        stripped = last_json(self.cli(["call"], input="{op:context,card:1}"))["error"]
        self.assertIn("'{op:context,card:1}'", stripped)
        excerpt = last_json(self.cli(["call"], input='{"op":"' + "x" * 200))["error"]
        self.assertIn("'" + '{"op":"' + "x" * 53 + "'", excerpt)
        self.assertNotIn("<<'EOF'", excerpt)

    def test_guessed_keys_and_ops_run_or_get_a_fix(self):
        self.calls({"op": "add", "title": "Needle in a haystack"})
        search, query, add, show, labelled = self.calls(
            {"op": "search", "query": "needle haystack"},
            {"op": "query", "q": "needle", "fields": ["num", "rev", "tag"]},
            {"op": "create", "title": "t", "tag": "x"},
            {"cmd": "show", "id": 2, "nothing": None},
            {"op": "get", "ref": "#2", "label": "Ada"})
        self.assertEqual(search["cards"], [{"num": 2, "title": "Needle in a haystack", "column": "task"}])
        self.assertEqual(query["cards"], [{"num": 2, "rev": search["rev"], "tag": []}])
        self.assertEqual(show["card"]["title"], "Needle in a haystack")
        self.assertEqual((labelled["num"], add["actor"]), (2, "Main"))
        self.assertEqual(self.calls({"op": "query", "card": add["num"], "fields": "tags"})[0]["cards"], [{"tags": ["x"]}])
        rev = self.calls({"op": "which"})[0]["rev"]
        body, move, claim, high, both = self.calls(
            {"op": "add", "title": "t", "body": "y"},
            {"op": "move", "card": 1, "column": "done"},
            {"op": "claim", "card": 1},
            {"op": "update", "card": 1, "priority": "high"},
            {"op": "show", "id": 1, "label": "Ada", "actor": "Grace"}, ok=False)
        self.assertEqual(body["error"], 'add doesn\'t take "body"')
        self.assertNotIn("fix", body)
        self.assertIn("notes", body["usage"]["optional"])
        self.assertEqual(move["code"], "state")  # move runs as fly, which leaves Done to done.
        self.assertIn('did you mean "start" (or "subtask.claim")?', claim["error"])
        self.assertEqual(claim["fix"], {"op": "start", "card": 1})
        self.assertEqual(high["fix"], {"op": "update", "card": 1, "priority": "critical"})
        self.assertIn('"label" and "actor" differ', both["error"])
        self.assertEqual(self.calls({"op": "which"})[0]["rev"], rev)
        proc = self.cli(["move", "1", "done"])
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("invalid choice", proc.stderr)
        self.assertEqual(proc.stderr.splitlines()[-1], "did you mean workboard fly?")

    def test_stream_flushes_before_stdin_closes(self):
        proc = subprocess.Popen([*command(), "call"], cwd=self.project, env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")
        try:
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: lines.put(proc.stdout.readline()))
            reader.start()
            proc.stdin.write(json.dumps({"op": "show", "card": 1}) + "\n")
            proc.stdin.flush()
            result = json.loads(lines.get(timeout=10))
            reader.join(timeout=1)
            self.assertTrue(result["ok"])
            self.assertIsNone(proc.poll(), "call must remain available for the next request")
            proc.stdin.write(json.dumps({"op": "note", "card": 1, "summary": "second request"}) + "\n")
            proc.stdin.close()
            self.assertEqual(json.loads(proc.stdout.readline())["action"], "note added: second request")
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

    def test_every_allowed_operation_has_one_json_object(self):
        self.calls(*[{"op": op, **({"card": 1} if op in ("context", "show", "inbox") else {})}
                     for op in ("digest", "context", "show", "query", "inbox", "boards", "which")])
        self.calls({"op": "fly", "card": 1, "column": "backlog"},
                   {"op": "fly", "card": 1, "column": "task"},
                   {"op": "add", "title": "Dependency"},
                   {"op": "depends", "card": 1, "on": [2]},
                   {"op": "depends", "card": 1, "clear": True},
                   {"op": "update", "card": 1, "addTag": ["a", "b"]},
                   {"op": "start", "card": 1})
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["Worker", "Legacy"]})[0]
        sid, legacy = [item["id"] for item in added["items"]]
        self.calls({"op": "subtask.delegate", "card": 1, "subtask": sid},
                   {"op": "subtask.configure", "card": 1, "subtask": sid, "scope": ["src"]},
                   {"op": "handoff", "card": 1, "subtask": sid, "worker": "Worker"},
                   {"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Worker"},
                   {"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Worker"},
                   {"op": "subtask.takeover", "card": 1, "subtask": sid, "actor": "Peer", "reason": "asked"},
                   {"op": "subtask.done", "card": 1, "subtask": sid, "actor": "Peer", "result": "evidence"},
                   {"op": "subtask.undone", "card": 1, "subtask": sid, "actor": "Peer"},
                   {"op": "subtask.done", "card": 1, "subtask": sid, "actor": "Peer", "result": "evidence"},
                   {"op": "subtask.request-changes", "card": 1, "subtask": sid, "reason": "one gap"},
                   {"op": "subtask.done", "card": 1, "subtask": sid, "actor": "Peer", "result": "verified"},
                   {"op": "subtask.accept", "card": 1, "subtask": sid},
                   {"op": "subtask.done", "card": 1, "subtasks": [legacy]},
                   {"op": "subtask.undone", "card": 1, "subtasks": [legacy]},
                   {"op": "subtask.rm", "card": 1, "subtasks": [legacy]},
                   {"op": "block", "card": 1, "reason": "pause", "until": "ready"},
                   {"op": "resume", "card": 1, "note": "ready"},
                   {"op": "takeover", "card": 1, "actor": "Peer", "reason": "asked"},
                   {"op": "done", "card": 1, "actor": "Peer", "writeup": "verified"},
                   {"op": "bug", "card": 1, "reason": "regression"},
                   {"op": "done", "card": 1, "writeup": "fixed"},
                   {"op": "improve", "card": 1, "text": "-new improvement"},
                   {"op": "done", "card": 1, "writeup": "improved"},
                   {"op": "reopen", "card": 1, "reason": "more work"},
                   {"op": "cancel", "card": 1, "reason": "not needed"},
                   {"op": "rework", "card": 1, "reason": "needed again"})
        self.calls({"op": "comment.add", "card": 1, "text": "discussion"},
                   {"op": "search", "terms": ["Parent"]},
                   {"op": "wait", "card": 1, "after": 0, "timeout": 1})
        file = self.base / "evidence.txt"
        file.write_text("evidence", encoding="utf-8")
        attachment = self.calls({"op": "attachment.add", "card": 1, "file": str(file)})[0]["item"]
        out = self.base / "exported.txt"
        listed, exported, removed = self.calls(
            {"op": "attachment.list", "card": 1},
            {"op": "attachment.get", "card": 1, "id": attachment["id"], "out": str(out)},
            {"op": "attachment.remove", "card": 1, "id": attachment["id"]})
        self.assertEqual(listed["attachments"][0]["id"], attachment["id"])
        self.assertEqual(out.read_text(encoding="utf-8"), "evidence")
        self.assertEqual(removed["item"]["id"], attachment["id"])

    def wait_with_writer(self, wait_args, write, expected=0):
        results = queue.Queue()

        def writer():
            try:
                time.sleep(0.6)
                write()
            except BaseException as exc:
                results.put(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = self.cli(["wait", "1", *wait_args, "--json"], timeout=10)
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        if not results.empty():
            raise results.get()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return last_json(result)

    def test_wait_observes_other_process_card_write(self):
        result = self.wait_with_writer(["--after", self.card["rev"], "--timeout", 3],
                                      lambda: self.calls({"op": "note", "card": 1, "summary": "writer"}))
        self.assertTrue(result["changed"])
        self.assertEqual(result["last"]["ev"], "note")
        self.assertEqual(result["last"]["by"], "Main")
        self.assertEqual(result["rev"], result["changedRev"])
        self.assertGreater(result["waited"], 0)
        self.assertNotIn("subtask", result)
        self.assertEqual(set(result), {"ok", "changed", "num", "id", "rev", "changedRev", "last", "waited"})

    def test_wait_times_out_without_a_write(self):
        result = self.json(["wait", "1", "--timeout", 1])
        self.assertFalse(result["changed"])
        self.assertEqual(result["rev"], self.card["rev"])
        self.assertGreaterEqual(result["waited"], 1)
        self.assertLess(result["waited"], 2)
        human = self.cli(["wait", "1", "--timeout", 1])
        self.assertEqual(human.returncode, 0, human.stdout + human.stderr)
        self.assertRegex(human.stdout, r"#1 unchanged after [0-9.]+s")

    def test_wait_disappearing_board_keeps_the_normal_error_envelope(self):
        result = self.wait_with_writer(["--timeout", 2], lambda: Path(self.board).unlink(), expected=1)
        self.assertEqual((result["ok"], result["status"], result["code"], result["rev"]),
                         (False, 404, "io", None))

    def test_wait_subtask_ignores_siblings_and_observes_itself(self):
        added = self.calls({"op": "subtask.add", "card": 1, "texts": ["Watched", "Sibling"]})[0]
        watched, sibling = [item["id"] for item in added["items"]]
        sibling_only = self.wait_with_writer(
            ["--subtask", watched, "--after", added["rev"], "--timeout", 2],
            lambda: self.calls({"op": "subtask.done", "card": 1, "subtask": sibling}))
        self.assertFalse(sibling_only["changed"])
        self.assertFalse(sibling_only["subtask"]["done"])
        self.assertEqual(sibling_only["notes"], [])
        baseline = sibling_only["rev"]
        own = self.wait_with_writer(
            ["--subtask", watched, "--after", baseline, "--timeout", 3],
            lambda: self.calls({"op": "subtask.done", "card": 1, "subtask": watched}))
        self.assertTrue(own["changed"])
        self.assertEqual(own["subtask"], {"id": watched, "done": True, "delegation": None})

    def test_wait_subtask_returns_the_new_notes_on_that_subtask(self):
        added = self.calls({"op": "start", "card": 1}, {"op": "subtask.add", "card": 1, "text": "Asked"})[1]
        sid = added["item"]["id"]
        self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Worker's question"})
        answered = self.wait_with_writer(
            ["--subtask", sid, "--timeout", 3],
            lambda: self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Main's answer"}))
        self.assertTrue(answered["changed"])
        self.assertEqual(answered["subtask"]["id"], sid)
        self.assertEqual([(note["summary"], note["by"]) for note in answered["notes"]], [("Main's answer", "Main")])

    def test_wait_subtask_catching_up_returns_notes_posted_after_your_own(self):
        sid = self.calls({"op": "start", "card": 1},
                         {"op": "subtask.add", "card": 1, "text": "Asked", "delegated": True})[1]["item"]["id"]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": sid, "actor": "Ada"})
        asked = self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Worker's question", "actor": "Ada"})[0]
        self.calls({"op": "note", "card": 1, "subtask": sid, "summary": "Main's answer"})  # Before Ada waits.
        caught = self.calls({"op": "wait", "card": 1, "subtask": sid, "after": asked["rev"], "timeout": 1,
                             "actor": "Ada"})[0]
        self.assertTrue(caught["changed"])
        self.assertEqual([note["summary"] for note in caught["notes"]], ["Main's answer"])

    def test_wait_subtask_ignores_notes_on_other_subtasks(self):
        added = self.calls({"op": "start", "card": 1},
                           {"op": "subtask.add", "card": 1, "texts": ["Watched", "Other"], "delegated": True})[1]
        watched, other = [item["id"] for item in added["items"]]
        self.calls({"op": "subtask.claim", "card": 1, "subtask": watched, "actor": "Ada"})
        done = self.calls({"op": "subtask.done", "card": 1, "subtask": watched, "result": "Verified",
                           "actor": "Ada"})[0]
        self.calls({"op": "note", "card": 1, "subtask": other, "summary": "About the other step"})
        wait = {"op": "wait", "card": 1, "subtask": watched, "after": done["rev"], "timeout": 1, "actor": "Ada"}
        quiet = self.calls(wait)[0]
        self.assertFalse(quiet["changed"])
        self.assertGreaterEqual(quiet["waited"], 1)
        self.calls({"op": "note", "card": 1, "subtask": watched, "summary": "Main's review"})
        answered = self.calls(wait)[0]
        self.assertTrue(answered["changed"])
        self.assertLess(answered["waited"], 0.5)
        self.assertEqual([note["summary"] for note in answered["notes"]], ["Main's review"])

    def test_wait_immediate_after_bounds_and_missing_subtask(self):
        note, added = self.calls({"op": "note", "card": 1, "summary": "already changed"},
                                {"op": "subtask.add", "card": 1, "text": "Watch"})
        sid = added["item"]["id"]
        for extra in ([], ["--subtask", sid]):
            result = self.json(["wait", "1", "--after", self.card["rev"], "--timeout", 1, *extra])
            self.assertTrue(result["changed"])
            self.assertLess(result["waited"], 0.5)
        bounds = self.calls(*[{"op": "wait", "card": 1, "timeout": value}
                              for value in (0, 0.5, 3601, -1)], ok=False)
        for result in bounds:
            self.assertEqual((result["status"], result["code"]), (422, "invalid"))
        maximum = self.calls({"op": "wait", "card": 1, "after": 0, "timeout": 3600})[0]
        self.assertTrue(maximum["changed"])
        missing = self.calls({"op": "wait", "card": 1, "subtask": "missing", "timeout": 1}, ok=False)[0]
        self.assertEqual((missing["status"], missing["code"]), (404, "not_found"))
        human = self.cli(["wait", "1", "--after", 0, "--timeout", 1])
        self.assertEqual(human.returncode, 0, human.stdout + human.stderr)
        self.assertIn("(last: subtask-add by Main)", human.stdout)

    def test_query_reads_chosen_cards_fields_and_full_log(self):
        self.calls({"op": "add", "title": "Second"}, {"op": "add", "title": "Third", "tags": ["a", "b"]})
        notes = self.calls(*[{"op": "note", "card": 1, "text": f"Finding {n}", "body": "x" * 400} for n in range(12)])
        chosen, tagged, words, phrase, absent = self.calls(
            {"op": "query", "card": ["#3", 1], "fields": "num,rev,log,origin,activeOwner"},
            {"op": "query", "tags": ["a", "b"], "fields": ["num"]},
            {"op": "query", "text": "finding 11"},
            {"op": "query", "text": ["11 finding"]},
            {"op": "search", "card": 2, "text": "finding"})
        self.assertEqual([card["num"] for card in chosen["cards"]], [1, 3])
        self.assertEqual(len(chosen["cards"][0]["log"]), 12)  # The whole timeline, bodies uncut.
        self.assertEqual(chosen["cards"][0]["log"][-1]["body"], "x" * 400)
        self.assertEqual((chosen["cards"][0]["rev"], set(chosen["cards"][1])),
                         (notes[-1]["rev"], {"num", "rev", "log", "origin", "activeOwner"}))
        self.assertEqual(tagged["cards"], [{"num": 3}])
        self.assertEqual([card["num"] for card in words["cards"]], [1])
        self.assertEqual((phrase["cards"], absent["cards"]), ([], []))
        unknown = self.calls({"op": "query", "fields": "num,status"}, ok=False)[0]
        self.assertIn("unknown fields: status", unknown["error"])
        shorthand = self.json(["search", "finding", "11"])
        self.assertEqual(shorthand, words)

    def test_query_narrowing_in_sql_never_drops_a_match(self):
        """query reads only the cards SQL finds may match; each case loses its card if that narrowing is wrong."""
        self.calls({"op": "add", "title": "Log only"},                                       # 2
                   {"op": "add", "title": "Temperature", "notes": "\u212aelvin scale"},       # 3: lower() → kelvin
                   {"op": "add", "title": "Dotted", "notes": "Capital X\u0130"},              # 4: lower() → xi̇
                   {"op": "add", "title": "Quoted", "notes": 'Say "hi" twice'},               # 5: JSON escapes "
                   {"op": "add", "title": "Windows path", "notes": "Path C:\\Temp"},          # 6: and \
                   {"op": "add", "title": "City", "notes": "ZÜRICH ÖL"},                      # 7: non-ASCII case
                   {"op": "add", "title": "Card alpha", "origin": "beta", "notes": "one\ntwo"},  # 8: phrases
                   {"op": "add", "title": "Shared word", "column": "backlog"},                # 9
                   {"op": "add", "title": "Shared word"},                                     # 10
                   {"op": "add", "title": "Held"}, {"op": "add", "title": "Parked"},          # 11, 12
                   {"op": "note", "card": 2, "text": "Plain", "body": "only the body says Needle"},
                   {"op": "start", "card": 11}, {"op": "start", "card": 12},
                   {"op": "block", "card": 12, "text": "waiting on review", "until": "review lands"})
        with core.board_transaction(self.board) as doc:  # An extension key on one card only.
            core.resolve_ref(doc, "10")["estimate"] = 3
            core.save(self.board, doc)
        cases = [({"text": "needle"}, [2]), ({"text": "KELVIN"}, [3]), ({"text": "xi"}, [4]),
                 ({"text": '"hi"'}, [5]), ({"text": "c:\\temp"}, [6]), ({"text": "zürich öl"}, [7]),
                 ({"text": ["alpha beta"]}, [8]), ({"text": ["one\ntwo"]}, [8]),
                 ({"column": "backlog", "text": "shared"}, [9]), ({"column": "task", "text": "WORD"}, [10]),
                 ({"mine": True}, [11, 12]), ({"owner": "Main", "text": "parked"}, [12])]
        results = self.calls(*[{"op": "query", **query} for query, _ in cases])
        self.assertEqual([[card["num"] for card in result["cards"]] for result in results],
                         [nums for _, nums in cases])
        subset, typo = self.calls({"op": "query", "text": "needle", "fields": "num,estimate"},
                                  {"op": "query", "text": "absent", "fields": "num,estimat"}, ok=False)
        self.assertEqual(subset["cards"], [{"num": 2, "estimate": None}])
        self.assertIn("unknown fields: estimat", typo["error"])

    def test_digest_matches_the_full_load(self):
        def ago(days):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))

        def step(state, owner="Main"):
            return [{"id": "s-1", "text": "Slice", "done": state != "claimed", "children": [],
                     "delegation": {"state": state, "owner": owner, "claimedAt": ago(3),
                                    "result": "" if state == "claimed" else "Verified"}}]

        def card(num, column, **fields):
            return core.normalize_card({"num": num, "id": f"d-{num}", "title": f"Card {num}", "column": column,
                                        "createdAt": ago(30), "updatedAt": ago(2), **fields})
        tie = ago(4)
        with core.board_transaction(self.board) as doc:  # Board order differs from number order.
            doc["cards"] += [
                card(17, "inprogress", activeOwner="Grace", subtasks=step("claimed")),
                card(2, "inprogress", activeOwner="Main", subtasks=step("claimed")),
                card(3, "blocked", blockedReason="API down", unblockWhen="it is up",
                     history=[{"at": ago(2), "ev": "blocked", "by": "Main"}]),
                card(4, "task", dependsOn=["d-5"], priority="critical"),
                card(6, "done", doneAt=tie), card(5, "done", doneAt=tie), card(7, "done", doneAt=ago(1)),
                card(8, "done"),
                *[card(n, "done", outcome="canceled", cancelReason=f"dropped {n}", doneAt=ago(n - 8))
                  for n in (9, 12, 10, 11)],
                card(13, "done", doneAt=ago(6), subtasks=step("claimed")),
                card(14, "done", doneAt=ago(5), subtasks=step("completed")),
                card(15, "task", reworkReason="redo", dependsOn=["d-9"]),
                card(16, "backlog", priority="low")]
            doc["nextNum"] = 18
            core.save(self.board, doc, by="Main")
        outputs = {}
        for actor in ("Main", "Grace"):
            for flag in ([], ["--json"]):
                new = self.cli(["--actor", actor, "digest", *flag])
                self.assertEqual(new.returncode, 0, new.stdout + new.stderr)
                board = json.loads(new.stdout)["board"] if flag else self.board
                self.assertEqual(new.stdout, old_digest(board, actor, bool(flag)))
                outputs[actor, bool(flag)] = new.stdout
        lines = [line.split(" — ")[0] for line in outputs["Main", False].splitlines()]  # The fixture reaches them.
        for expected in ("  SHIPPED: #7 Card 7 (1d ago)", "  SHIPPED: #6 Card 6 (4d ago)", "  CANCELED: #12 Card 12",
                         "    #13/s-1 claimed Slice (parent done)", "    #17/s-1 claimed Slice (parent inprogress)"):
            self.assertIn(expected, lines)
        self.assertEqual(json.loads(outputs["Main", True])["stats"]["byOwner"], {"Grace": 1, "Main": 1})

    def test_help_answers_without_a_board_and_matches_the_cli(self):
        whole, one = self.calls({"op": "help"}, {"op": "help", "for": "search"})
        self.assertEqual(list(whole["ops"]), list(cli.OPS))
        self.assertEqual(list(one["ops"]), ["query"])
        self.assertEqual(one["ops"]["query"]["example"], {"op": "query", "text": "login timeout",
                                                          "fields": "num,title,column,owner"})
        self.assertLess(len(json.dumps(whole)), 14000)
        nowhere = run(["call"], cwd=self.base, env=self.env, input='{"op":"help","for":"note"}\n')
        self.assertEqual(nowhere.returncode, 0, nowhere.stdout + nowhere.stderr)
        self.assertEqual(last_json(nowhere), last_json(self.cli(["help", "note", "--json"])))
        unknown = self.calls({"op": "help", "for": "frob"}, ok=False)[0]
        self.assertIn('{"op":"help"} lists every op', unknown["error"])
        for args, text in ((["help"], "note.get"), (["help", "subtask.done"], "usage: workboard subtask"),
                           (["help", "attachment.add"], "--file FILE"), (["help", "get"], "usage: workboard context"),
                           (["call", "--help"], '{"op":"help"} lists every op')):
            proc = self.cli(args)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(text, proc.stdout, args)

    def test_call_takes_arguments_prints_ascii_and_fixes_command_lines(self):
        proc = self.cli(["call", json.dumps({"op": "note", "card": 1, "text": "Café → done"}), '{"op":"show","card":1}'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(proc.stdout.isascii(), proc.stdout)  # Any wrapper's codec decodes it.
        noted, shown = map(json.loads, proc.stdout.splitlines())
        self.assertEqual(shown["card"]["log"][-1]["summary"], "Café → done")
        line = self.cli(["call"], input="show 1 --full\n")
        self.assertEqual(line.returncode, 1, line.stdout + line.stderr)
        self.assertEqual((last_json(line)["error"], last_json(line)["fix"]),
                         ("call reads JSON requests, not commands", {"op": "context", "card": 1, "full": True}))


# Requests agents really sent from 2026-10-28 to 11-13 that call refused (build/friction/calls.jsonl).
REPLAYED = [
    {"op": "search", "text": "autocorrect"},
    {"op": "search", "q": "gwd", "fields": "num,title,column,owner,updatedAt"},
    {"op": "search", "card": 2, "query": "MB"},
    {"op": "search", "query": "passkey", "fields": "num,title,column,updatedAt"},
    {"op": "query", "fields": "num,title,column,owner,tag,updatedAt", "q": "codex"},
    {"op": "query", "fields": "num,title,column,outcome,updatedAt", "search": "Phone path down"},
    {"op": "query", "fields": "num,title,column,owner", "terms": "1726"},
    {"op": "query", "fields": "num,title,origin", "card": 1906},
    {"op": "query", "fields": "num,title,column,owner,updatedAt,rev", "card": [2279, 2280, 2292]},
    {"op": "query", "cards": [1673, 1679, 1685, 1687, 1699, 1740], "fields": "num,title,column,owner,updatedAt"},
    {"op": "query", "fields": "num,title,column,owner,rev", "scope": None, "search": None, "cards": None,
     "num": [2000, 2001]},
    {"op": "query", "fields": "num,rev,column,owner", "scope": ["2339"]},
    {"op": "query", "column": "backlog", "limit": 400,
     "fields": ["num", "title", "tags", "priority", "owner", "activeOwner", "createdAt", "column"]},
    {"op": "query", "fields": "num,title,column,owner,tags", "tag": ["codex"]},
    {"op": "show", "id": 2171, "label": "AgentLink2171-3"},
    {"op": "show", "ref": "2156", "full": True},
    {"op": "show", "num": 241},
    {"op": "note", "id": 2000, "text": "Server half for main: agents/host-return-server 82f761564"},
    {"op": "note", "subtask": 2130, "text": "Cause found"},
    {"op": "digest", "label": "Main"},
    {"op": "help"},
    {"label": "AskCard2169-2", "op": "help"},
    {"op": "get", "card": 2, "board": "AgentPadAI", "subtask": "s-9"},
    {"op": "card.get", "id": 2326},
    {"op": "create", "title": "LED and Haptics pages forget the selected section", "origin": "Owner 2026-11-03"},
    {"op": "create", "title": "Closing DSX while it waits for ownership stops an Engine it didn't start",
     "priority": "medium", "tags": ["bug", "open-beta"], "origin": "StartupVerify 2026-10-28"},
    {"op": "add", "column": "task", "title": "unit-server gate flake", "priority": "high", "tag": "bug",
     "origin": "Main via BugHunter-10"},
    {"op": "add", "column": "backlog", "priority": "mid", "tags": ["bug"], "title": "Gate flake: gwd-job.ps1"},
    {"op": "list", "column": "doing"},
    {"op": "comment", "card": 1849, "text": "Fix: fix/slow-first-start 9e575e8b4 (from main 50cbfaffd)."},
    {"op": "card.note", "card": 1734, "text": "Method report written"},
    {"op": "attachment.add", "card": 1820, "path": "C:/Users/Paliverse/AppData/Local/Temp/a11y/composer-stop.png"},
    {"op": "done", "card": 2167, "note": "Landed cc9abefa01 on gwd/main."},
    {"op": "note", "card": 2169, "note": "AskCard2169-4: WD4-3's two fixes in, plus both wording fixes"},
    {"op": "takeover", "card": 2309, "note": "FixAppState4 \u2192 FixAppState5: FixAppState4 ended (budget)"},
    {"cmd": "show", "id": 2235, "full": True},
    {"op": "context", "card": 2073, "fields": "num,title,column"},
    {"op": "subtask.done", "card": 2670, "id": "s-8"},
    {"op": "bug", "title": "Telemetry: an iPhone fling's slow tail counts as jumps", "priority": "mid",
     "tags": ["phone", "telemetry"], "notes": "When an iPhone fling decelerates"},
    {"op": "claim", "card": 2182},
]


class Vocabulary(unittest.TestCase):
    """The OPS table, validated without a board."""

    def runs(self, request) -> list:
        op, argv = cli._call_argv(request)
        cli.build_parser(argv[0]).parse_args(argv)  # argparse takes the argv call runs.
        return argv

    def test_real_refused_requests_now_run_or_carry_a_fix_that_passes(self):
        fixed = 0
        for request in REPLAYED:
            with self.subTest(request=request):
                try:
                    self.runs(request)
                except cli.CallRefused as refused:
                    self.assertIsNotNone(refused.fix, str(refused))
                    self.assertIn(refused.fix["op"], cli.OPS)  # A fix uses canonical names.
                    self.runs(refused.fix)
                    fixed += 1
        self.assertEqual(fixed, 8)

    def test_ops_table_and_argparse_agree(self):
        commands = cli._commands(cli.build_parser())
        verbs = {spec["argv"][0] for spec in cli.OPS.values()}
        self.assertEqual(set(commands) - verbs - cli.CLI_ONLY, {"show", "search", "list", "bug", "improve", "rework"})
        self.assertLessEqual(verbs | cli.CLI_ONLY, set(commands))
        self.assertLessEqual(set().union(*(spec["keys"] for spec in cli.OPS.values())), set(cli.KEYS))
        reached = {}
        for op, spec in cli.OPS.items():
            parser = commands[spec["argv"][0]]
            parser = cli._commands(parser)[spec["argv"][2]] if spec["argv"][0] == "attachment" else parser
            positional = {token.strip("{}.") for token in spec["argv"] if token.startswith("{")}
            for key in spec["keys"] - positional:
                name = spec["text"] if key == "text" and spec["text"] else "tag" if key == "tags" else key
                action = parser._option_string_actions.get("--" + cli._kebab(name))
                self.assertIsNotNone(action, (op, key))
                self.assertEqual(isinstance(action, argparse._AppendAction), key in spec["lists"], (op, key))
                reached.setdefault(parser, set()).add(action)
            reached.setdefault(parser, set())
            for word in ("argv", "required", "optional"):
                self.assertTrue(all(isinstance(item, str) for item in spec[word]), op)
            example = cli._help_entry(op)["example"]
            self.assertEqual(self.runs(example)[0], spec["argv"][0], op)
        for parser, actions in reached.items():  # Every request-shaped option is some op's key.
            unreached = {name for action in parser._actions if action not in actions for name in action.option_strings
                         if name.startswith("--") and name not in ("--help", "--json") and not name.endswith("stdin")}
            self.assertEqual(unreached, set(), parser.prog)

    def test_note_get_refuses_an_ambiguous_prefix_listing_the_matches(self):
        entry = {"at": "2026-11-13T00:00:00Z", "by": "Ada", "summary": "s", "body": ""}
        doc = {"rev": 7, "cards": [{"num": 1, "id": "one", "log": [{**entry, "id": "abcd1" + "0" * 27}]},
                                   {"num": 2, "id": "two", "log": [{**entry, "id": "abcd2" + "0" * 27}]}]}
        args = argparse.Namespace(id="ABCD", card=None, board=None, json=True)
        with mock.patch.object(cli.wb, "load", return_value=doc), mock.patch.object(cli.wb, "find_board"):
            with self.assertRaisesRegex(cli.wb.WorkflowError, "matches 2 notes: #1 abcd10+, #2 abcd20+"):
                cli.cmd_note_get(args)
            with self.assertRaisesRegex(cli.wb.WorkflowError, "at least 4"):
                cli.cmd_note_get(argparse.Namespace(**{**vars(args), "id": "abc"}))



if __name__ == "__main__":
    unittest.main()
