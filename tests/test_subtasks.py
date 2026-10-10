# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Delegated subtask CLI contracts."""
from __future__ import annotations

import json
import io
import copy
import threading
import unittest
import contextlib
from pathlib import Path
from unittest import mock

from tests.support import last_json, make_env, run, scratch
from workboard import cli, core

# board.json as WorkBoard 0.1.2 writes it after `init legacy` and one `add` (schema 3).
V012_BOARD = {
    "name": "legacy", "rev": 2, "nextNum": 2,
    "columns": [{"id": column, "name": name, "kind": kind, "stackUnder": None} for column, name, kind in (
        ("backlog", "Backlog", "todo"), ("task", "Task", "todo"), ("inprogress", "In Progress", "active"),
        ("done", "Done", "done"), ("blocked", "Blocked", "blocked"))],
    "cards": [{"num": 1, "id": "old-card-d71267eb5d7a466cbf9c5061694d5175", "code": "", "title": "Old card",
               "column": "task", "priority": None, "tags": [], "origin": "", "notes": "", "log": [],
               "writeup": "", "subtasks": [], "links": [],
               "history": [{"at": "2026-10-10T09:29:57Z", "ev": "created", "by": "Main"}], "cycles": [],
               "createdAt": "2026-10-10T09:29:57Z", "updatedAt": "2026-10-10T09:29:57Z", "doneAt": None,
               "reopenReason": None, "blockedReason": None, "unblockWhen": None, "blockedAt": None,
               "dependsOn": [], "activeOwner": None, "claimedAt": None, "outcome": None, "cancelReason": None,
               "reworkReason": None, "comments": [], "attachments": [], "verification": [], "reviews": [],
               "changedRev": 2}],
    "schemaVersion": 3, "savedAt": "2026-10-10T09:29:57Z", "savedBy": "Main",
}


def schema(path):
    return json.loads(Path(path).read_bytes())["schemaVersion"]



class DelegatedSubtasks(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.base = self.stack.enter_context(scratch("wb-subtasks-"))
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = make_env(self.home)
        self.stack.enter_context(mock.patch.dict(core.os.environ, self.env, clear=True))
        self.project = self.base / "project"
        self.project.mkdir()
        self.call(["init", "delegated"])
        self.card = last_json(self.call(["add", "--title", "Parent", "--json"]))

    def tearDown(self):
        self.stack.close()

    def call(self, args, actor="Main"):
        return run([*args, "--actor", actor], cwd=self.project,
                   env=make_env(self.home, WORKBOARD_ACTOR=actor))

    def json(self, args, actor="Main"):
        result = self.call([*args, "--json"], actor)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return last_json(result)

    def rejected(self, args, actor, code):
        path = core.board_file("delegated")
        before = path.read_bytes()
        result = self.call([*args, "--json"], actor)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(last_json(result)["code"], code, result.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_claim_race_release_takeover_and_evidence(self):
        added = self.json(["subtask", "1", "add", "Investigate", "--delegated"])
        subtask = added["item"]["id"]
        self.json(["start", "1"], "Main")
        barrier, outcomes = threading.Barrier(2), {}

        def claim(actor):
            barrier.wait()
            outcomes[actor] = self.call(["subtask", "1", "claim", subtask, "--json"], actor)

        threads = [threading.Thread(target=claim, args=(actor,)) for actor in ("Ada", "Grace")]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        winner = next(actor for actor, result in outcomes.items() if result.returncode == 0)
        loser = next(actor for actor in outcomes if actor != winner)
        self.assertNotEqual(outcomes[loser].returncode, 0)
        state = self.json(["context", "1", "--full"])["card"]["subtasks"][0]["delegation"]
        self.assertEqual((state["state"], state["owner"]), ("claimed", winner))
        self.assertNotEqual(self.call(["subtask", "1", "release", subtask, "--reason", "no"], loser).returncode, 0)
        self.json(["subtask", "1", "release", subtask, "--reason", "blocked"], winner)
        self.json(["subtask", "1", "claim", subtask], loser)
        self.json(["subtask", "1", "takeover", subtask, "--reason", "handoff"], winner)
        done = self.json(["subtask", "1", "done", subtask, "--result", "tested evidence"], winner)
        self.assertTrue(done["item"]["done"])
        self.assertEqual(done["item"]["delegation"]["result"], "tested evidence")
        self.json(["subtask", "1", "undone", subtask], winner)
        context = self.json(["context", "1", "--full"])["card"]
        self.assertEqual(context["column"], "inprogress")
        self.assertTrue(any(entry["summary"] == f"result {subtask}" for entry in context["log"]))

    def test_legacy_nested_extensions_and_stale_guard(self):
        added = self.json(["subtask", "1", "add", "Legacy"])
        subtask = added["item"]["id"]
        self.json(["subtask", "1", "done", subtask], "Main")
        shown = self.json(["show", "1", "--full"])
        rev = shown["rev"]
        self.json(["subtask", "1", "undone", subtask], "Main")
        stale = self.call(["subtask", "1", "delegate", subtask, "--expected-rev", str(rev), "--json"], "Main")
        self.assertNotEqual(stale.returncode, 0)
        path = core.board_file("delegated")
        doc = core.load(path)
        doc["cards"][0]["subtasks"][0]["vendorSubtask"] = {"keep": True}
        core.save(path, doc)
        self.assertEqual(core.load(core.board_file("delegated"))["cards"][0]["subtasks"][0]["vendorSubtask"], {"keep": True})

    def test_complementary_claims_publication_and_parent_integration(self):
        added = self.json(["subtask", "1", "add", "Produce", "Consume", "--delegated"])
        one, two = [item["id"] for item in added["items"]]
        started = self.json(["start", "1"])
        claimed = self.json(["subtask", "1", "claim", one, "--expected-rev", started["rev"]], "Ada")
        self.rejected(["subtask", "1", "claim", two, "--expected-rev", started["rev"]], "Grace", "stale")
        reviewed = self.json(["context", "1"], "Grace")
        self.assertEqual(reviewed["card"]["subtasks"][0]["delegation"]["owner"], "Ada")
        self.json(["subtask", "1", "claim", two, "--expected-rev", reviewed["rev"]], "Grace")
        self.json(["update", "1", "--title", "Parent, retitled by a reviewer"], "Ada")
        self.rejected(["subtask", "1", "done", two, "--result", "not mine"], "Ada", "owned")
        self.rejected(["subtask", "1", "release", two, "--reason", "not mine"], "Ada", "owned")
        self.rejected(["done", "1", "--writeup", "unfinished work"], "Main", "state")
        self.json(["note", "1", "--summary", "finding: producer interface", "--body",
                   f"Kind: finding; Subtask: {one}; Scope: consumer; Evidence: exported API; Limitations: implementation pending"], "Ada")
        self.assertIn("exported API", self.json(["context", "1"], "Grace")["card"]["log"][-1]["body"])
        pulse = self.json(["digest"], "Grace")
        self.assertEqual([entry["subtaskId"] for entry in pulse["contributions"]], [two])
        for actor, sid in (("Ada", one), ("Grace", two)):
            self.json(["subtask", "1", "done", sid, "--result", "verified contribution; see notes"], actor)
        context = self.json(["context", "1", "--full"])
        self.assertEqual(context["card"]["activeOwner"], "Main")
        self.assertEqual(context["card"]["column"], "inprogress")
        self.rejected(["done", "1", "--writeup", "worker integration"], "Ada", "owned")
        self.json(["done", "1", "--writeup", "Main integrated and verified", "--expected-rev", context["rev"]])

    def test_nested_assignment_boundaries_and_independent_children(self):
        root = self.json(["subtask", "1", "add", "Delegated tree", "--delegated"])["item"]["id"]
        child = self.json(["subtask", "1", "add", "Legacy detail", "--parent", root])["item"]["id"]
        independent = self.json(["subtask", "1", "add", "Independent child", "--parent", root, "--delegated"])["item"]["id"]
        self.json(["start", "1"])
        self.json(["subtask", "1", "claim", root], "Ada")
        self.json(["subtask", "1", "claim", independent], "Grace")
        # Legacy checklist items inside a claimed subtree stay editable by anyone, the claimant included.
        self.json(["subtask", "1", "done", child], "Ada")
        nested = self.json(["subtask", "1", "add", "nested expansion", "--parent", child])["item"]["id"]
        self.json(["subtask", "1", "rm", nested], "Grace")
        self.rejected(["subtask", "1", "rm", root], "Main", "owned")
        self.rejected(["subtask", "1", "rm", independent], "Ada", "owned")
        self.rejected(["subtask", "1", "delegate", child], "Ada", "owned")
        self.json(["subtask", "1", "done", independent, "--result", "independent child verified"], "Grace")
        self.json(["subtask", "1", "done", root, "--result", "tree contribution verified"], "Ada")
        saved = self.json(["context", "1", "--full"])["card"]["subtasks"][0]
        self.assertEqual(saved["by"], "Main")
        self.assertEqual([item["id"] for item in saved["children"]], [child, independent])
        self.assertEqual((saved["children"][0]["done"], saved["children"][0]["doneBy"]), (True, "Ada"))
        self.assertNotIn("delegation", saved["children"][0])

    def test_paused_and_canceled_work_keeps_claims_recoverable(self):
        sid = self.json(["subtask", "1", "add", "Work", "--delegated"])["item"]["id"]
        self.json(["start", "1"])
        self.json(["subtask", "1", "claim", sid], "Ada")
        self.json(["block", "1", "--reason", "external wait", "--until", "input arrives"])
        self.rejected(["subtask", "1", "done", sid, "--result", "out of scope"], "Ada", "state")
        self.assertEqual(self.json(["context", "1"])["card"]["subtasks"][0]["delegation"]["owner"], "Ada")
        self.json(["subtask", "1", "release", sid, "--reason", "partial findings in note"], "Ada")
        self.json(["resume", "1", "--note", "input arrived"])
        self.json(["subtask", "1", "claim", sid], "Grace")
        self.json(["cancel", "1", "--reason", "request withdrawn"])
        self.rejected(["subtask", "1", "done", sid, "--result", "out of scope"], "Grace", "state")
        self.json(["subtask", "1", "takeover", sid, "--reason", "recover interrupted worker"], "Main")
        self.json(["subtask", "1", "release", sid, "--reason", "request canceled; partial state documented"])
        # Handoffs remain understandable after capped activity has rolled over.
        path = core.board_file("delegated")
        with core.board_transaction(path) as doc:
            for i in range(core.HISTORY_CAP + 1):
                core.hist(doc["cards"][0], "test-event", note=str(i))
            core.save(path, doc)
        log = self.json(["context", "1", "--full"])["card"]["log"]
        self.assertTrue(any("Grace → Main" in note["body"] for note in log))
        self.assertTrue(any("partial findings in note" in note["body"] for note in log))

    def test_same_second_writes_and_malformed_or_ambiguous_actions(self):
        path = core.board_file("delegated")
        # Separate CLI writes in one clock tick must all persist, while an
        # idempotent claim must not bump the revision.
        with mock.patch.object(core, "now_iso", return_value="2026-10-09T00:00:00Z"):
            def invoke(args, actor="Main"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    cli.main(["--board", "delegated", "--actor", actor, *args, "--json"])
                return json.loads(output.getvalue())
            invoke(["start", "1"])
            added = invoke(["subtask", "1", "add", "A", "B", "--delegated"])
            a, b = [s["id"] for s in added["items"]]
            claimed = invoke(["subtask", "1", "claim", a], "Ada")
            self.assertGreater(claimed["rev"], added["rev"])
            repeated = invoke(["subtask", "1", "claim", a], "Ada")
            self.assertEqual(repeated["rev"], claimed["rev"])
            other = invoke(["subtask", "1", "claim", b], "Grace")
            self.assertGreater(other["rev"], repeated["rev"])
        self.assertEqual([s["delegation"]["owner"] for s in self.json(["context", "1"])["card"]["subtasks"]], ["Ada", "Grace"])
        doc = core.load(path)
        for value in ("yes", 1, [], None):
            with self.assertRaises(core.WorkflowError):
                core.subtask_action(doc, doc["cards"][0], "add", {"texts": ["bad"], "delegated": value}, "Main")
        doc["cards"][0]["subtasks"].append(dict(doc["cards"][0]["subtasks"][0]))
        core.save(path, doc)
        self.rejected(["subtask", "1", "takeover", "s-1", "--reason", "ambiguous target"], "Main", "invalid")
        self.assertEqual(self.json(["inbox"])["items"], [])

    def test_schema_1_to_3_boards_read_unchanged_and_save_as_3(self):
        path = core.board_file("delegated")
        raw = {
            "schemaVersion": 3, "name": "legacy delegated", "rev": 7, "nextNum": 2,
            "vendorDocument": {"preserve": ["all", "extensions"]},
            "cards": [{"id": "legacy", "num": 1, "title": "Legacy", "column": "task",
                       "vendorCard": {"keep": True}, "subtasks": [{"id": "parent", "text": "parent",
                       "vendorSubtask": {"root": True}, "children": [{"id": "child", "text": "child",
                       "vendorSubtask": {"nested": True}, "children": []}]}]}],
        }
        for version in (1, 2, 3):
            candidate = {**copy.deepcopy(raw), "schemaVersion": version}
            encoded = json.dumps(candidate, ensure_ascii=False).encode("utf-8")
            path.write_bytes(encoded)
            loaded = core.load(path)
            self.assertEqual(path.read_bytes(), encoded, "reads do not rewrite legacy boards")
            self.assertEqual(loaded["schemaVersion"], 3)
            child = loaded["cards"][0]["subtasks"][0]["children"][0]
            self.assertEqual((loaded["vendorDocument"], loaded["cards"][0]["vendorCard"],
                              child["vendorSubtask"]),
                             (candidate["vendorDocument"], candidate["cards"][0]["vendorCard"],
                              candidate["cards"][0]["subtasks"][0]["children"][0]["vendorSubtask"]))
        core.save(path, loaded, by="Main")
        self.assertEqual(schema(path), 3, "a board without delegation stays readable by 0.1.x")
        loaded["cards"][0]["subtasks"][0]["children"][0]["delegation"] = {"state": "available"}
        core.save(path, loaded, by="Main")
        self.assertEqual(schema(path), 4, "delegation at any depth needs schema 4")

        malformed = json.loads(json.dumps(raw))
        malformed["cards"][0]["subtasks"][0]["delegation"] = {
            "state": "claimed", "owner": None, "claimedAt": None, "result": "",
        }
        path.write_text(json.dumps(malformed), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(core.WorkflowError):
            core.load(path)
        self.assertEqual(path.read_bytes(), before, "malformed v3 delegation is never repaired on read")

    def plain_lifecycle(self, board):
        """Ordinary writes on a new card #2, then export and import; each must keep schema 3."""
        path = core.board_file(board)
        self.assertEqual(schema(path), 3)
        for step in (["add", "--title", "Plain"], ["start", "2"], ["note", "2", "--summary", "Plain note"],
                     ["comment", "2", "add", "Plain comment"], ["subtask", "2", "add", "Plain step"],
                     ["subtask", "2", "done", "s-1"], ["update", "2", "--title", "Plain renamed"],
                     ["done", "2", "--writeup", "Finished plainly"]):
            self.json([*step, "--board", board])
            self.assertEqual(schema(path), 3, step)
        bundle, project = self.base / f"{board}.zip", self.base / f"{board}-copy"
        project.mkdir()
        self.json(["export", "--out", str(bundle), "--board", board])
        self.json(["import", str(bundle), "--name", f"{board}-copy", "--dir", str(project), "--apply"])
        self.assertEqual(schema(core.board_file(f"{board}-copy")), 3)
        return path

    def test_boards_switch_to_schema_4_only_when_they_use_delegation(self):
        path = self.plain_lifecycle("delegated")
        sid = self.json(["subtask", "1", "add", "Delegated piece", "--delegated"])["item"]["id"]
        self.assertEqual(schema(path), 4)
        self.json(["subtask", "1", "rm", sid])
        self.json(["note", "1", "--summary", "No delegation left"])
        self.assertEqual(schema(path), 4, "a schema 4 board is never downgraded")

        legacy = self.base / "legacy"
        legacy.mkdir()
        old = core.create_board("legacy", legacy)
        old.write_text(json.dumps(V012_BOARD, indent=2), encoding="utf-8")
        self.plain_lifecycle("legacy")
        self.json(["start", "1", "--board", "legacy"])
        finding = self.json(["note", "1", "--summary", "Peer finding", "--board", "legacy"], "Ada")["item"]["id"]
        self.assertEqual(schema(old), 3)
        self.json(["ack", "1", finding, "--board", "legacy"])
        self.assertEqual(schema(old), 4)
        self.json(["update", "1", "--title", "Plain edit", "--board", "legacy"])
        self.assertEqual(schema(old), 4)

    def test_wrong_actor_is_owned_before_wrong_state(self):
        sid = self.json(["subtask", "1", "add", "Work", "--delegated"])["item"]["id"]
        self.json(["start", "1"])
        self.rejected(["subtask", "1", "release", sid, "--reason", "nothing held"], "Ada", "owned")
        self.json(["subtask", "1", "claim", sid], "Ada")
        self.rejected(["subtask", "1", "claim", sid], "Grace", "owned")
        for args in (["resume", sid], ["undone", sid]):
            self.rejected(["subtask", "1", *args], "Ada", "state")
        self.json(["subtask", "1", "block", sid, "--reason", "wait", "--until", "input arrives"], "Ada")
        self.rejected(["subtask", "1", "block", sid, "--reason", "again", "--until", "input arrives"], "Ada", "state")
        self.rejected(["subtask", "1", "done", sid, "--result", "while blocked"], "Ada", "state")
        self.rejected(["subtask", "1", "resume", sid], "Grace", "owned")
        self.json(["subtask", "1", "resume", sid], "Ada")
        done = self.json(["subtask", "1", "done", sid, "--result", "verified"], "Ada")
        self.assertEqual(self.json(["subtask", "1", "done", sid, "--result", "verified"], "Ada")["rev"], done["rev"])
        self.rejected(["subtask", "1", "done", sid, "--result", "a different result"], "Ada", "state")
        self.rejected(["subtask", "1", "release", sid, "--reason", "after done"], "Ada", "state")
        self.rejected(["subtask", "1", "claim", sid], "Ada", "state")
        self.rejected(["subtask", "1", "undone", sid], "Grace", "owned")

    def test_generic_replacement_edits_checklists_but_not_delegated_state(self):
        root = self.json(["subtask", "1", "add", "Delegated tree", "--delegated"])["item"]["id"]
        self.json(["subtask", "1", "add", "Legacy detail", "--parent", root])
        self.json(["start", "1"])
        self.json(["subtask", "1", "claim", root], "Ada")
        current = core.load(core.board_file("delegated"))["cards"][0]["subtasks"]
        edited = copy.deepcopy(current)
        edited[0]["collapsed"] = True
        edited[0]["children"][0].update(done=True, text="Legacy detail, clarified")
        core.guard_subtask_replacement(current, edited, "Main")
        for change, code in ((lambda items: items[0].update(done=True), "state"),
                             (lambda items: items[0]["delegation"].update(owner="Main"), "state"),
                             (lambda items: items.pop(0), "owned"),
                             (lambda items: items[0].update(text="Rewritten assignment"), "owned")):
            candidate = copy.deepcopy(current)
            change(candidate)
            with self.assertRaises(core.WorkflowError) as caught:
                core.guard_subtask_replacement(current, candidate, "Main")
            self.assertEqual(caught.exception.code, code)

    def test_twelve_workers_race_for_six_subtasks_with_one_attributed_winner_each(self):
        ids = [item["id"] for item in self.json(
            ["subtask", "1", "add", *[f"Assignment {i}" for i in range(6)], "--delegated"])["items"]]
        started = self.json(["start", "1"])
        jobs = [(f"Worker {i:02}", ids[i // 2]) for i in range(12)]  # two workers contend per subtask

        def race(argv):
            barrier, results = threading.Barrier(len(jobs)), [None] * len(jobs)

            def invoke(index, actor, sid):
                barrier.wait(timeout=30)
                results[index] = self.call([*argv(sid, actor), "--json"], actor)

            threads = [threading.Thread(target=invoke, args=(index, *job)) for index, job in enumerate(jobs)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            return results

        winners = {}
        for (actor, sid), result in zip(jobs, race(lambda sid, actor: ["subtask", "1", "claim", sid])):
            payload = last_json(result)
            if result.returncode == 0:
                self.assertNotIn(sid, winners)
                self.assertEqual(payload["item"]["delegation"]["owner"], actor)
                winners[sid] = actor
            else:
                self.assertEqual(payload["code"], "owned", result.stdout + result.stderr)
        self.assertEqual(set(winners), set(ids))

        def only_winners(results):
            for (actor, sid), result in zip(jobs, results):
                payload = last_json(result)
                if winners[sid] == actor:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertEqual(payload["code"], "owned", result.stdout + result.stderr)

        only_winners(race(lambda sid, actor: ["note", "1", "--subtask", sid, "--summary", f"finding: {actor}",
                                              "--body", f"Evidence for {sid}"]))
        only_winners(race(lambda sid, actor: ["subtask", "1", "done", sid, "--result", f"Verified by {actor}"]))
        context = self.json(["context", "1", "--full"])
        self.assertEqual(context["rev"], started["rev"] + 18, "losers never write")
        for item in context["card"]["subtasks"]:
            actor = winners[item["id"]]
            self.assertEqual((item["done"], item["doneBy"], item["delegation"]["owner"], item["delegation"]["result"]),
                             (True, actor, actor, f"Verified by {actor}"))
        findings = [entry for entry in context["card"]["log"] if entry["summary"].startswith("finding:")]
        self.assertEqual(sorted((entry["subtaskId"], entry["by"]) for entry in findings), sorted(winners.items()))
        self.json(["done", "1", "--writeup", "Main integrated six results", "--expected-rev", context["rev"]])


class DelegationWorkflow(unittest.TestCase):
    """Reviews, blockers, prerequisites, write scopes and the inbox."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = self.stack.enter_context(scratch("wb-workflow-"))
        self.home, self.project = self.base / "home", self.base / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.env = make_env(self.home)
        self.stack.enter_context(mock.patch.dict(core.os.environ, self.env, clear=True))
        self.json(["init", "workflow"])
        self.path = core.board_file("workflow")
        self.ref = self.parent("Integration")

    def call(self, args, actor="Main"):
        return run([*args, "--actor", actor], cwd=self.project, env=self.env)

    def json(self, args, actor="Main"):
        result = self.call([*args, "--json"], actor)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        return last_json(result)

    def reject(self, args, code, actor="Main"):
        before = self.path.read_bytes()
        result = self.call([*args, "--json"], actor)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        error = last_json(result)
        self.assertEqual(error["code"], code, result.stdout + result.stderr)
        self.assertEqual(error["rev"], json.loads(before)["rev"])
        self.assertEqual(self.path.read_bytes(), before)
        return error

    def parent(self, title):
        ref = self.json(["add", "--title", title])["id"]
        self.json(["start", ref])
        return ref

    def add(self, *titles, ref=None):
        return [item["id"] for item in self.json(
            ["subtask", ref or self.ref, "add", *titles, "--delegated"])["items"]]

    def action(self, op, sid, *args, actor="Main", ref=None):
        return self.json(["subtask", ref or self.ref, op, sid, *args], actor)

    def context(self, ref=None):
        return self.json(["context", ref or self.ref, "--full"])

    def inbox(self, actor="Main"):
        result = self.json(["inbox", self.ref], actor)
        self.assertEqual(result["actor"], actor)
        return result["items"]

    def test_review_request_changes_recompletion_and_acceptance_keep_evidence(self):
        sid, = self.add("Reviewed contribution")
        configured = self.action("configure", sid, "--review-required")
        self.assertTrue(configured["item"]["delegation"]["review"]["required"])
        self.action("claim", sid, actor="Ada")
        first = self.action("done", sid, "--result", "First result with test evidence", actor="Ada")
        self.assertEqual(first["item"]["delegation"]["review"]["state"], "pending")
        self.reject(["done", self.ref, "--writeup", "Premature integration"], "state")
        self.reject(["subtask", self.ref, "accept", sid], "owned", "Ada")
        changed = self.action("request-changes", sid, "--reason", "Cover empty input")
        item = changed["item"]
        self.assertFalse(item["done"])
        self.assertEqual((item["delegation"]["state"], item["delegation"]["owner"]), ("claimed", "Ada"))
        self.assertEqual(item["delegation"]["review"]["state"], "changes_requested")
        inbox = self.inbox("Ada")
        self.assertEqual([(row["kind"], row["subtaskId"]) for row in inbox], [("changes_requested", sid)])
        completed = self.action("done", sid, "--result", "Second result; empty input verified", actor="Ada")
        self.assertEqual(completed["item"]["delegation"]["review"]["state"], "pending")
        reviews = [row for row in self.inbox() if row["kind"] == "review"]
        self.assertEqual([row["subtaskId"] for row in reviews], [sid])
        accepted = self.action("accept", sid)
        self.assertEqual((accepted["item"]["delegation"]["review"]["state"],
                          accepted["item"]["delegation"]["review"]["by"]), ("accepted", "Main"))
        repeated = self.action("accept", sid)
        self.assertEqual(repeated["rev"], accepted["rev"])
        context = self.context()
        results = [entry["body"] for entry in context["card"]["log"]
                   if entry["summary"] == f"result {sid}"]
        self.assertEqual(results, ["First result with test evidence", "Second result; empty input verified"])
        self.assertFalse(self.inbox())
        self.json(["done", self.ref, "--writeup", "Integrated both revisions and verified empty input",
                   "--expected-rev", context["rev"]])

    def test_review_gate_is_opt_in_and_available_configuration_is_owned(self):
        sid, = self.add("Existing worker flow")
        self.reject(["subtask", self.ref, "configure", sid, "--review-required"], "owned", "Ada")
        self.action("configure", sid, "--scope", "src/worker.py", "--review-required")
        cleared = self.action("configure", sid, "--clear-scope", "--clear-deps", "--no-review-required")
        self.assertEqual(cleared["item"]["delegation"]["scope"], [])
        self.assertFalse(cleared["item"]["delegation"]["review"]["required"])
        self.action("claim", sid, actor="Ada")
        self.reject(["subtask", self.ref, "configure", sid, "--review-required"], "state")
        self.action("done", sid, "--result", "Legacy worker behavior verified", actor="Ada")
        result = self.json(["done", self.ref, "--writeup", "Integration remains compatible"])
        self.assertEqual(result["column"], "done")

    def test_parent_owner_rescopes_and_annotates_running_work(self):
        sid, other = self.add("Running work", "Prerequisite")
        self.action("configure", sid, "--scope", "src/a.py", "--review-required")
        claimed = self.action("claim", sid, actor="Ada")["item"]["delegation"]
        widened = self.action("configure", sid, "--scope", "src/a.py", "--scope", "src/b.py")
        delegation = widened["item"]["delegation"]
        self.assertEqual(delegation["scope"], ["src/a.py", "src/b.py"])
        self.assertEqual({**delegation, "scope": claimed["scope"]}, claimed)
        last = self.context()["card"]["history"][-1]
        self.assertEqual((last["ev"], last["by"]), ("subtask-configure", "Main"))
        focused = self.json(["context", self.ref, "--subtask", sid], "Ada")
        item = next(st for st, _ in core.iter_subtasks(focused["card"]["subtasks"]) if st["id"] == sid)
        self.assertEqual(item["delegation"]["scope"], ["src/a.py", "src/b.py"])
        for extra in (["--on", other], ["--clear-deps"], ["--review-required"], ["--no-review-required"]):
            self.reject(["subtask", self.ref, "configure", sid, "--scope", "src/c.py", *extra], "state")
        self.reject(["subtask", self.ref, "configure", sid, "--scope", "src/c.py"], "owned", "Ada")
        self.action("block", sid, "--reason", "Needs a schema", "--until", "Schema lands", actor="Ada")
        cleared = self.action("configure", sid, "--clear-scope")["item"]["delegation"]
        self.assertEqual((cleared["scope"], cleared["state"], cleared["owner"], cleared["blocker"]["reason"]),
                         ([], "blocked", "Ada", "Needs a schema"))
        note = ["note", self.ref, "--subtask", sid, "--summary", "Approved src/b.py"]
        self.reject(note, "owned", "Grace")
        entry = self.json(note)["item"]
        self.assertEqual((entry["subtaskId"], entry["by"]), (sid, "Main"))
        self.assertEqual([row["kind"] for row in self.inbox()], ["blocker"])
        self.action("resume", sid, actor="Ada")
        self.action("done", sid, "--result", "Both files verified", actor="Ada")
        self.reject(["subtask", self.ref, "configure", sid, "--scope", "src/c.py"], "state")
        self.assertEqual(self.json([*note[:-1], "Reviewing the result"])["item"]["subtaskId"], sid)
        self.json(["note", self.ref, "--subtask", other, "--summary", "Unclaimed slice note"])
        self.assertEqual([row["kind"] for row in self.inbox()], ["review"])

    def test_block_resume_and_release_preserve_ownership_and_observable_exit(self):
        sid, = self.add("Waiting for producer")
        self.action("claim", sid, actor="Ada")
        self.reject(["subtask", self.ref, "block", sid, "--reason", "Waiting", "--until", ""], "invalid", "Ada")
        blocked = self.action("block", sid, "--reason", "Need producer output", "--until",
                              "Producer publishes verified schema", actor="Ada")
        assignment = blocked["item"]["delegation"]
        self.assertEqual((assignment["state"], assignment["owner"]), ("blocked", "Ada"))
        self.assertEqual((assignment["blocker"]["reason"], assignment["blocker"]["until"],
                          assignment["blocker"]["by"]),
                         ("Need producer output", "Producer publishes verified schema", "Ada"))
        for actor in ("Main", "Grace"):
            self.reject(["subtask", self.ref, "resume", sid], "owned", actor)
            self.reject(["subtask", self.ref, "release", sid, "--reason", "Hand off"], "owned", actor)
        self.assertEqual([row["kind"] for row in self.inbox()], ["blocker"])
        self.assertEqual([row["kind"] for row in self.inbox("Ada")], ["blocker"])
        self.reject(["done", self.ref, "--writeup", "Still blocked"], "state")
        resumed = self.action("resume", sid, actor="Ada")
        self.assertEqual(resumed["item"]["delegation"]["state"], "claimed")
        self.assertIsNone(resumed["item"]["delegation"]["blocker"])
        self.action("block", sid, "--reason", "Wait again", "--until", "Fresh input arrives", actor="Ada")
        released = self.action("release", sid, "--reason", "Handoff recorded", actor="Ada")
        self.assertEqual(released["item"]["delegation"]["state"], "available")
        self.assertIsNone(released["item"]["delegation"]["owner"])
        self.assertIsNone(released["item"]["delegation"]["blocker"])
        self.action("claim", sid, actor="Grace")
        self.action("done", sid, "--result", "Input integrated", actor="Grace")

    def test_dependency_validation_rejects_missing_legacy_self_cycles_and_removal(self):
        first, second = self.add("First", "Second")
        legacy = self.json(["subtask", self.ref, "add", "Checklist"])["item"]["id"]
        for dependency in ("missing", first, legacy):
            self.reject(["subtask", self.ref, "configure", first, "--on", dependency], "invalid")
        self.action("configure", second, "--on", first)
        self.reject(["subtask", self.ref, "configure", first, "--on", second], "invalid")
        error = self.reject(["subtask", self.ref, "rm", first], "state")
        self.assertIn(second, error["error"])
        self.action("configure", second, "--clear-deps")
        self.action("rm", first)

    def test_card_completion_does_not_recheck_reopened_prerequisite_cards(self):
        prerequisite = self.parent("Prerequisite")
        self.json(["done", prerequisite, "--writeup", "Shipped"])
        dependent = self.json(["add", "--title", "Dependent", "--on", prerequisite])["id"]
        self.json(["start", dependent])
        self.json(["rework", prerequisite, "--reason", "Regression found"])
        self.assertEqual(self.json(["done", dependent, "--writeup", "Verified on its own"])["column"], "done")

    def test_transitive_prerequisites_gate_claims_and_parent_completion(self):
        producer, middle, consumer, unrelated = self.add("Producer", "Middle", "Consumer", "Unrelated")
        self.action("configure", producer, "--review-required")
        self.action("configure", middle, "--on", producer)
        self.action("configure", consumer, "--on", middle)
        self.reject(["subtask", self.ref, "claim", middle], "deps", "Bert")
        self.action("claim", producer, actor="Ada")
        self.action("done", producer, "--result", "Producer verified", actor="Ada")
        self.reject(["subtask", self.ref, "claim", middle], "deps", "Bert")
        self.action("accept", producer)
        self.action("claim", middle, actor="Bert")
        self.action("done", middle, "--result", "Middle verified", actor="Bert")
        self.action("request-changes", producer, "--reason", "Producer contract changed")
        self.reject(["subtask", self.ref, "claim", consumer], "deps", "Chen")
        self.action("claim", unrelated, actor="Dara")
        self.action("done", producer, "--result", "Producer reverified", actor="Ada")
        self.action("accept", producer)
        self.action("claim", consumer, actor="Chen")
        self.action("done", consumer, "--result", "Consumer verified", actor="Chen")
        self.action("done", unrelated, "--result", "Unrelated verified", actor="Dara")
        self.action("request-changes", producer, "--reason", "New integration requirement")
        self.reject(["done", self.ref, "--writeup", "Cannot integrate stale prerequisites"], "state")

    def test_saved_scope_overlap_uses_path_boundaries_and_cross_card_identity(self):
        parent_scope, nested, adjacent = self.add("Directory owner", "Nested file", "Adjacent directory")
        for sid, scope in ((parent_scope, "src/pkg"), (nested, "src/pkg/file.py"), (adjacent, "src/pkg_extra")):
            self.action("configure", sid, "--scope", scope)
        self.action("claim", parent_scope, actor="Ada")
        doc = core.load(self.path)
        card = core.resolve_ref(doc, self.ref)
        self.assertEqual([row["subtaskId"] for row in core.scope_conflicts(doc, card, nested)], [parent_scope])
        self.assertEqual(core.scope_conflicts(doc, card, adjacent), [])
        self.action("configure", adjacent, "--scope", ".")
        doc = core.load(self.path)
        self.assertEqual([row["subtaskId"] for row in core.scope_conflicts(
            doc, core.resolve_ref(doc, self.ref), adjacent)], [parent_scope])
        other_ref = self.parent("Other card in this repository")
        other_sid, = self.add("Same directory elsewhere", ref=other_ref)
        self.action("configure", other_sid, "--scope", "src/pkg/file.py", ref=other_ref)
        doc = core.load(self.path)
        other = core.resolve_ref(doc, other_ref)
        conflicts = core.scope_conflicts(doc, other, other_sid)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual((conflicts[0]["cardId"], conflicts[0]["subtaskId"], conflicts[0]["owner"]),
                         (self.ref, parent_scope, "Ada"))
        handoff = self.json(["handoff", other_ref, "--subtask", other_sid, "--worker", "Grace"])
        self.assertEqual(handoff["scopeWarnings"], conflicts)
        self.action("block", parent_scope, "--reason", "Waiting", "--until", "Input arrives", actor="Ada")
        doc = core.load(self.path)
        self.assertEqual(core.scope_conflicts(doc, core.resolve_ref(doc, other_ref), other_sid)[0]["state"], "blocked")
        self.action("release", parent_scope, "--reason", "Scope relinquished", actor="Ada")
        doc = core.load(self.path)
        self.assertEqual(core.scope_conflicts(doc, core.resolve_ref(doc, other_ref), other_sid), [])

    def test_scope_configuration_rejects_ambiguous_paths_without_writing(self):
        sid, = self.add("Portable scope")
        for scope in ("../outside", "/absolute", "C:/absolute", "C:\\absolute", "\\\\server\\share", "src//file.py",
                      "src/./file.py", "src/*.py"):
            self.reject(["subtask", self.ref, "configure", sid, "--scope", scope], "invalid")
        configured = self.action("configure", sid, "--scope", "src/file.py", "--scope", "src/file.py")
        self.assertEqual(configured["item"]["delegation"]["scope"], ["src/file.py"])

    def test_inbox_reviews_findings_and_batched_acknowledgement_per_parent_owner(self):
        worker, own, unreviewed = self.add("Worker finding", "Main finding", "Unreviewed contribution")
        for sid in (worker, own):
            self.action("configure", sid, "--review-required")
        self.action("claim", worker, actor="Ada")
        self.action("claim", own)
        self.action("claim", unreviewed, actor="Bert")
        finding = self.json(["note", self.ref, "--subtask", worker, "--summary", "Producer shape verified",
                             "--body", "Use two fields"], "Ada")["item"]
        own_note = self.json(["note", self.ref, "--subtask", own, "--summary", "My own integration note"])["item"]
        unscoped = self.json(["note", self.ref, "--summary", "Unscoped note"], "Grace")["item"]
        self.action("done", worker, "--result", "Worker result verified", actor="Ada")
        self.action("done", own, "--result", "Main's own part verified")
        self.action("done", unreviewed, "--result", "Unreviewed part verified", actor="Bert")
        # Reviews list only opted-in work completed by someone other than the parent owner.
        self.assertEqual([(row["kind"], row["subtaskId"]) for row in self.inbox()],
                         [("review", worker), ("finding", worker), ("finding", None)])
        self.reject(["ack", self.ref, finding["id"]], "owned", "Ada")
        self.reject(["ack", self.ref, finding["id"], own_note["id"]], "not_found")
        history = len(core.resolve_ref(core.load(self.path), self.ref)["history"])
        rev = json.loads(self.path.read_bytes())["rev"]
        acknowledged = self.json(["ack", self.ref, finding["id"], unscoped["id"]])
        self.assertEqual(acknowledged["rev"], rev + 1)
        self.assertEqual([item["id"] for item in acknowledged["items"]], [finding["id"], unscoped["id"]])
        self.assertEqual(len(core.resolve_ref(core.load(self.path), self.ref)["history"]), history)
        self.assertEqual(self.json(["ack", self.ref, unscoped["id"], finding["id"]])["rev"], acknowledged["rev"])
        self.assertEqual([row["kind"] for row in self.inbox()], ["review"])
        self.json(["takeover", self.ref, "--reason", "New integration owner"], "Other Main")
        transferred = self.inbox("Other Main")
        self.assertIn(finding["id"], [row["note"]["id"] for row in transferred if row["kind"] == "finding"])
        self.assertEqual(self.inbox(), [])
        self.json(["ack", self.ref, finding["id"]], "Other Main")
        self.json(["takeover", self.ref, "--reason", "Main resumes integration"])
        self.assertEqual([row["kind"] for row in self.inbox()], ["review"])
        self.json(["takeover", self.ref, "--reason", "Other Main resumes integration"], "Other Main")
        self.assertNotIn(finding["id"], [row["note"]["id"] for row in self.inbox("Other Main")
                                        if row["kind"] == "finding"])

    def test_lifecycle_round_trip_keeps_extension_fields(self):
        sid, = self.add("Extension-aware contribution")
        self.action("configure", sid, "--review-required", "--scope", "src/extension.py")
        with core.board_transaction(self.path) as doc:
            card = core.resolve_ref(doc, self.ref)
            item = card["subtasks"][0]
            doc["vendorBoard"] = {"preserve": True}
            card["vendorCard"] = ["keep"]
            item["vendorSubtask"] = {"kind": "custom"}
            item["delegation"]["vendorAssignment"] = 17
            item["delegation"]["review"]["vendorReview"] = "stable"
            core.save(self.path, doc, by="Main")
        self.action("claim", sid, actor="Ada")
        self.action("block", sid, "--reason", "Waiting", "--until", "Fixture ready", actor="Ada")
        self.action("resume", sid, actor="Ada")
        self.action("done", sid, "--result", "Verified extensions", actor="Ada")
        self.action("accept", sid)
        self.assertEqual(json.loads(self.path.read_bytes())["schemaVersion"], 4)
        doc = core.load(self.path)
        card = core.resolve_ref(doc, self.ref)
        item = card["subtasks"][0]
        self.assertEqual(doc["vendorBoard"], {"preserve": True})
        self.assertEqual(card["vendorCard"], ["keep"])
        self.assertEqual(item["vendorSubtask"], {"kind": "custom"})
        self.assertEqual(item["delegation"]["vendorAssignment"], 17)
        self.assertEqual(item["delegation"]["review"]["vendorReview"], "stable")
