#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""WorkBoard: the concise agent/human interface for the project kanban.

Every mutation prints one line and supports --json for a machine postcondition.
Run from anywhere inside the project (walks up to board/board.json), or pass
--board <project-root-or-board.json>. `workboard open` shows the board in the
browser; ordinary board commands never start a server or open a browser.
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import sys
import time
from pathlib import Path

from . import __version__, core as wb

wb.configure_utf8()


def _emit(args, card: dict, doc: dict, action: str, extra: str = "",
          *, item=None, items=None) -> None:
    rev = doc["rev"]
    n_cards = len(doc["cards"])
    if getattr(args, "json", False):
        result = {"ok": True, "action": action, "num": card["num"],
                  "id": card["id"], "column": card["column"],
                  "rev": rev, "actor": wb.actor(),
                  "activeOwner": card.get("activeOwner"), "outcome": card.get("outcome"),
                  "dependsOn": card.get("dependsOn") or []}
        if item is not None:
            result["item"] = item
        if items is not None:
            result["items"] = items
        print(json.dumps(result, ensure_ascii=False))
        return
    tail = f" (rev {rev} · {n_cards} cards)"
    if extra:
        tail = f" (rev {rev}{extra})"
    print(f"{wb.fmt_ref(card)} {card['title'][:60]} {action}{tail}")


def _resolve(doc, ref) -> dict:
    try:
        return wb.resolve_ref(doc, ref)
    except wb.RefError as e:
        raise wb.WorkflowError(str(e), 404) from e

def _column(doc, ref) -> dict:
    try:
        return wb.ensure_column(doc, ref)
    except wb.RefError as e:
        raise wb.WorkflowError(str(e), 422) from e



def _read_stdin() -> str:
    text = sys.stdin.buffer.read().decode("utf-8-sig").replace("\r\n", "\n").strip()
    if not text:
        raise wb.WorkflowError("stdin must contain nonempty text")
    return text


# ===== verbs =====

def cmd_add(args):
    p = wb.find_board(args.board)
    origin = _read_stdin() if args.origin_stdin else (args.origin or "")
    with wb.board_transaction(p) as doc:
        col = args.column or "task"
        target = _column(doc, col)
        col = target["id"]
        if col not in ("backlog", "task"):
            raise wb.WorkflowError("new cards must enter Backlog or Task; use lifecycle commands for later stages",
                                   code="state")
        title = args.title
        card = wb.normalize_card({
            "num": wb.new_num(doc),
            "id": wb.unique_id(doc, wb.slugify(title)),
            "code": "",
            "title": title,
            "column": col,
            "priority": args.priority,
            "tags": list(args.tag or []),
            "origin": origin,
            "notes": "",
            "log": [],
            "writeup": "",
            "subtasks": [],
            "links": [],
            "history": [],
            "cycles": [],
            "createdAt": wb.now_iso(),
            "updatedAt": wb.now_iso(),
            "doneAt": None,
            "reopenReason": None,
        })
        wb.hist(card, "created", by=wb.actor(), note=origin or None)
        doc["cards"].append(card)
        if args.on:
            ids = [_resolve(doc, ref)["id"] for ref in args.on]
            wb.workflow_action(doc, card, "dependencies", {"ids": ids}, wb.actor())
        wb.save(p, doc)
    _emit(args, card, doc, f"added → {col}")


def _fly(doc, card, col, note=None):
    return wb.workflow_action(doc, card, "move", {"to": col, "note": note}, wb.actor())


def _run_workflow(args, action, details):
    p = wb.find_board(args.board)
    by = wb.actor()
    with wb.board_transaction(p, args.expected_rev, args.ref) as doc:
        card = _resolve(doc, args.ref)
        notes = card.get("notes")
        already_owned = card["column"] == "inprogress" and card.get("activeOwner") == by
        wb.workflow_action(doc, card, action, details, by)
        if not ((action == "workpad" and notes == card.get("notes"))
                or (action == "start" and already_owned)):
            wb.save(p, doc, by=by)
    return card, doc


def cmd_start(args):
    card, doc = _run_workflow(args, "start", {})
    _emit(args, card, doc, "→ inprogress")


def cmd_done(args):
    if not args.writeup and not args.writeup_stdin:
        raise wb.WorkflowError('done requires --writeup "..." (or --writeup-stdin)', code="state")
    writeup = _read_stdin() if args.writeup_stdin else args.writeup
    card, doc = _run_workflow(args, "complete", {"writeup": writeup})
    _emit(args, card, doc, "→ done", extra=f" · writeup {len(writeup)} ch")


def cmd_fly(args):
    p = wb.find_board(args.board)
    with wb.board_transaction(p, args.expected_rev, args.ref) as doc:
        card = _resolve(doc, args.ref)
        if wb.slugify(args.column, 24) == "done":
            raise wb.WorkflowError(f"use done {args.ref} --writeup to complete work", code="state")
        frm = card["column"]
        _fly(doc, card, args.column, note=args.note)
        wb.save(p, doc)
    _emit(args, card, doc, f"{frm} → {card['column']}")


def cmd_block(args):
    reason = _read_stdin() if args.reason_stdin else args.reason
    card, doc = _run_workflow(args, "block", {"reason": reason, "until": args.until})
    _emit(args, card, doc, "→ blocked")


def cmd_resume(args):
    card, doc = _run_workflow(args, "resume", {"note": args.note, "to": args.to})
    _emit(args, card, doc, f"resumed → {card['column']}")


def cmd_update(args):
    p = wb.find_board(args.board)
    notes = _read_stdin() if args.notes_stdin else args.notes
    with wb.board_transaction(p, args.expected_rev, args.ref) as doc:
        card = _resolve(doc, args.ref)
        changed = []
        if args.title:
            card["title"] = args.title
            changed.append("title")
        if args.priority:
            card["priority"] = args.priority
            changed.append("priority")
        if args.add_tag:
            for t in args.add_tag:
                if t not in card["tags"]:
                    card["tags"].append(t)
            changed.append("+tag")
        if args.rm_tag:
            card["tags"] = [t for t in card["tags"] if t not in args.rm_tag]
            changed.append("-tag")
        if notes is not None:
            card["notes"] = notes
            changed.append("notes")
        if changed:
            wb.hist(card, "updated", by=wb.actor(), note=",".join(changed))
            wb.touch(card)
            wb.save(p, doc)
    if args.json:
        _emit(args, card, doc, f"updated: {', '.join(changed) or 'nothing'}")
    else:
        print(f"{wb.fmt_ref(card)} updated: {', '.join(changed) or 'nothing'}")


def cmd_note(args):
    body = _read_stdin() if args.stdin else args.body
    card, doc = _run_workflow(args, "note", {"summary": args.summary, "body": body})
    entry = card["log"][-1]
    action = f"note added: {entry['summary'][:60]}"
    if args.json:
        _emit(args, card, doc, action, item=entry)
    else:
        print(f"{wb.fmt_ref(card)} {card['title'][:60]} {action}")


def cmd_subtask(args):
    p = wb.find_board(args.board)
    by = wb.actor()
    with wb.board_transaction(p, args.expected_rev, args.ref) as doc:
        card = _resolve(doc, args.ref)
        existing = {st["id"]: st for st, _ in wb.iter_subtasks(card["subtasks"])}
        items, changed = [], []
        if args.op == "add":
            parent = existing.get(args.parent) if args.parent else None
            if args.parent and parent is None:
                raise wb.WorkflowError(f"no subtask {args.parent}", 404)
            destination = parent["children"] if parent is not None else card["subtasks"]
            number = len(existing) + 1
            for text in args.what:
                text = wb._required_text(text, "subtask text")
                while f"s-{number}" in existing:
                    number += 1
                st = {"id": f"s-{number}", "text": text, "done": False,
                      "createdAt": wb.now_iso(), "doneAt": None, "by": by,
                      "children": [], "collapsed": False}
                number += 1
                destination.append(st)
                items.append(st)
            changed = items
        else:
            for reference in dict.fromkeys(args.what):
                if reference not in existing:
                    raise wb.WorkflowError(f"no subtask {reference} on {wb.fmt_ref(card)}", 404)
                items.append(existing[reference])
            if args.op == "rm":
                remove_ids = {st["id"] for st in items}

                def remove(subtasks):
                    subtasks[:] = [st for st in subtasks if st["id"] not in remove_ids]
                    for st in subtasks:
                        remove(st["children"])

                remove(card["subtasks"])
                changed = items
            else:
                done = args.op == "done"
                for st in items:
                    if st["done"] == done:
                        continue
                    st["done"] = done
                    st["doneAt"] = wb.now_iso() if done else None
                    if done:
                        st["doneBy"] = by
                    else:
                        st.pop("doneBy", None)
                    changed.append(st)
        if changed:
            wb.hist(card, f"subtask-{args.op}", by=by, note=", ".join(st["id"] for st in changed))
            wb.touch(card)
            wb.save(p, doc, by=by)
        subtasks = [st for st, _ in wb.iter_subtasks(card["subtasks"])]
        count = sum(st["done"] for st in subtasks)
    ids = ", ".join(st["id"] for st in items)
    verb = {"add": "added", "done": "done", "undone": "reopened", "rm": "removed"}[args.op]
    action = f"subtask [{ids}] {verb} · {count}/{len(subtasks)} done"
    _emit(args, card, doc, action, items=items, item=items[0] if len(items) == 1 else None)


def cmd_bug(args):
    card, doc = _run_workflow(args, "bug", {"reason": args.reason})
    _emit(args, card, doc, f"→ inprogress [{card['subtasks'][-1]['id']}]", item=card["subtasks"][-1])


def cmd_improve(args):
    card, doc = _run_workflow(args, "improve", {"text": args.text})
    _emit(args, card, doc, f"→ inprogress [{card['subtasks'][-1]['id']}]", item=card["subtasks"][-1])


def cmd_reopen(args):
    mode = args.as_ or "task"
    card, doc = _run_workflow(args, "reopen", {"reason": args.reason, "as": mode})
    _emit(args, card, doc, f"reopened → task ({mode})")


def cmd_workpad(args):
    card, doc = _run_workflow(args, "workpad", {})
    _emit(args, card, doc, "workpad ready in notes")


def cmd_reasoned_action(args):
    card, doc = _run_workflow(args, args.cmd, {"reason": args.reason})
    _emit(args, card, doc, f"{args.cmd} → {card['column']}")


def cmd_depends(args):
    p = wb.find_board(args.board)
    with wb.board_transaction(p, args.expected_rev, args.ref) as doc:
        card = _resolve(doc, args.ref)
        ids = list(card.get("dependsOn") or [])
        if args.clear:
            ids = []
        for ref in args.on or []:
            ids.append(_resolve(doc, ref)["id"])
        for ref in args.remove or []:
            # A deleted predecessor is deliberately unresolved, but its exact ID can be removed.
            dependency_id = ref if ref in ids else _resolve(doc, ref)["id"]
            ids = [i for i in ids if i != dependency_id]
        wb.workflow_action(doc, card, "dependencies", {"ids": ids}, wb.actor())
        wb.save(p, doc)
    _emit(args, card, doc, f"dependencies updated ({len(card['dependsOn'])})")


def cmd_comment(args):
    p = wb.find_board(args.board)
    operation = {"type": args.op}
    if args.op != "add":
        operation["id"] = args.what
    if args.op != "delete":
        operation["text"] = _read_stdin() if args.stdin else (args.what if args.op == "add" else args.text)
    with wb.board_transaction(p, args.expected_rev, args.ref) as doc:
        card = _resolve(doc, args.ref)
        removed = next((item for item in card["comments"] if item.get("id") == args.what), None)
        comment = wb.comment_action(card, operation, wb.actor())
        wb.save(p, doc)
    item = comment if comment is not None else removed
    _emit(args, card, doc, f"comment {args.op} [{item['id']}]", item=item)


def cmd_context(args):
    print(json.dumps(wb.card_context(wb.find_board(args.board), args.ref, full=args.full),
                     ensure_ascii=False, indent=None if args.json else 2))


def cmd_attachment(args):
    p = wb.find_board(args.board)
    if args.op == "get":
        result = wb.attachment_export(p, args.ref, args.id, args.out)
    elif args.op == "list":
        context = wb.card_context(p, args.ref)
        card = context["card"]
        result = {"ok": True, "board": str(p), "rev": context["rev"],
                  "cardId": card["id"], "num": card["num"], "attachments": card["attachments"]}
    else:
        if args.op == "add":
            wb.require_write_scope(p)
            with open(args.file, "rb") as stream:
                data = stream.read(wb.MAX_ATTACHMENT_BYTES + 1)
            doc, card, metadata = wb.attachment_add(
                p, args.ref, args.name or Path(args.file).name, data, wb.actor(),
                mime=args.mime, expected_rev=args.expected_rev)
        else:
            doc, card, metadata = wb.attachment_detach(
                p, args.ref, args.id, wb.actor(), expected_rev=args.expected_rev)
        _emit(args, card, doc, f"attachment {args.op} [{metadata['id']}]", item=metadata)
        return
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.op == "list":
        for item in result["attachments"]:
            print(f"{item['id']} {item['name']} ({item['size']} bytes, {item['mime']})")
        if not result["attachments"]:
            print("(no attachments)")
    elif args.op == "get":
        print(f"#{result['num']} attachment exported → {result['out']} (SHA256 {result['sha256']})")


def cmd_next(args):
    if args.limit < 0:
        raise wb.WorkflowError("--limit must be nonnegative")
    doc = wb.load(wb.find_board(args.board))
    cards = wb.ready_cards(doc)[:args.limit]
    if args.json:
        fields = ("num", "id", "title", "priority", "tags", "createdAt", "dependsOn")
        print(json.dumps({"ok": True, "rev": doc["rev"],
                          "cards": [{key: card[key] for key in fields} for card in cards]},
                         ensure_ascii=False))
    elif not cards:
        print("(no ready cards)")
    else:
        for card in cards:
            print(f"{wb.fmt_ref(card)} {card['title'][:72]} [{card.get('priority') or 'unset'}]")


def _age(iso):
    try:
        secs = time.time() - calendar.timegm(
            time.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"))
    except (ValueError, TypeError):
        return "?"
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _age_days(iso) -> int:
    try:
        secs = time.time() - calendar.timegm(
            time.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"))
        return max(0, int(secs // 86400))
    except (ValueError, TypeError):
        return -1


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} ch, --full)"


def cmd_show(args):
    p = wb.find_board(args.board)
    doc = wb.load(p)
    card = _resolve(doc, args.ref)
    out = dict(card)
    if not args.full:
        out["notes"] = _clip(out.get("notes", ""), 300)
        out["writeup"] = _clip(out.get("writeup", ""), 400)
        out["log"] = [{**entry, "body": _clip(entry["body"], 300)} for entry in out["log"][-5:]]
        out["history"] = out["history"][-10:]
    print(json.dumps({"ok": True, "rev": doc["rev"], "card": out} if args.json else out,
                     indent=None if args.json else 2, ensure_ascii=False))


def _match(card, args) -> bool:
    if args.column and card["column"] != args.column.lstrip("#"):
        return False
    if args.tag and args.tag not in card["tags"]:
        return False
    if args.priority and card["priority"] != args.priority:
        return False
    return True


def cmd_list(args):
    p = wb.find_board(args.board)
    doc = wb.load(p)
    cards = [c for c in doc["cards"] if _match(c, args)]
    order = {c["id"]: i for i, c in enumerate(doc["columns"])}
    cards.sort(key=lambda c: (order.get(c["column"], 99), c["num"]))
    if args.json:
        print(json.dumps({"ok": True, "board": str(p), "rev": doc["rev"], "cards": cards}, ensure_ascii=False))
        return
    if not cards:
        print("(no matching cards)")
        return
    for c in cards:
        pr = {"critical": "‼ ", "low": "▽ "}.get(c["priority"] or "", "")
        tags = " ".join(f"[{t}]" for t in c["tags"][:3])
        subs = [s for s, _ in wb.iter_subtasks(c["subtasks"])]
        prog = ""
        if subs:
            d = sum(1 for s in subs if s["done"])
            prog = f" ☑{d}/{len(subs)}"
        age = _age(c["updatedAt"])
        print(f"{wb.fmt_ref(c):>6}  {c['column']:<12} {pr}{c['title'][:52]}"
              f"{prog} {tags} ({age})")


def cmd_query(args):
    p = wb.find_board(args.board)
    doc = wb.load(p)
    nums = {card["id"]: card["num"] for card in doc["cards"]}
    fields_map = {
        "num": lambda c: c["num"], "id": lambda c: c["id"],
        "title": lambda c: c["title"], "column": lambda c: c["column"],
        "priority": lambda c: c["priority"], "tags": lambda c: c["tags"],
        "outcome": lambda c: c["outcome"], "owner": lambda c: c["activeOwner"],
        "deps": lambda c: [nums.get(dep) for dep in c["dependsOn"]],
        "changedRev": lambda c: c["changedRev"],
        "createdAt": lambda c: c["createdAt"], "updatedAt": lambda c: c["updatedAt"],
        "doneAt": lambda c: c["doneAt"], "origin": lambda c: c["origin"],
    }
    names = [field.strip() for field in (args.fields or "num,title,column").split(",")]
    unknown = [name for name in names if name not in fields_map]
    if unknown:
        raise wb.WorkflowError(f"unknown fields: {', '.join(unknown)}; valid fields: {', '.join(fields_map)}")
    owner = wb.actor() if args.mine else args.owner
    cards = [c for c in doc["cards"] if _match(c, args)
             and (owner is None or _holder(c) == owner)]
    if args.since_days:
        cards = [c for c in cards if _age_days(c["updatedAt"]) <= args.since_days]
    cards.sort(key=lambda c: c["num"])
    if args.limit:
        cards = cards[-args.limit:]
    getters = [fields_map[name] for name in names]
    out = [{name: get(c) for name, get in zip(names, getters)} for c in cards]
    print(json.dumps({"ok": True, "rev": doc["rev"], "cards": out}, ensure_ascii=False))


def _safe_updated(c):
    try:
        secs = time.time() - calendar.timegm(
            time.strptime(c["updatedAt"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"))
        return secs
    except (ValueError, TypeError):
        return 0


def cmd_search(args):
    p = wb.find_board(args.board)
    doc = wb.load(p)
    terms = [t.lower() for t in args.terms]

    def hay(c):
        blobs = [c["title"], c["origin"], c["notes"], c["writeup"], c["id"]]
        blobs += c["tags"]
        blobs += [s["text"] for s, _ in wb.iter_subtasks(c["subtasks"])]
        blobs += [str(comment.get("text") or "") for comment in c["comments"]]
        blobs += [text for entry in c["log"] for text in (entry["summary"], entry["body"])]
        return " ".join(blobs).lower()

    hits = [c for c in doc["cards"] if all(t in hay(c) for t in terms)]
    if args.json:
        print(json.dumps({"ok": True, "board": str(p), "rev": doc["rev"], "cards": hits}, ensure_ascii=False))
        return
    for c in hits[:20]:
        print(f"{wb.fmt_ref(c):>6}  {c['column']:<12} {c['title'][:56]}")
    if not hits:
        print("(no matches)")


def _stage_attention(card: dict) -> str | None:
    return wb.stage_attention(card)


def _holder(card: dict) -> str | None:
    """Active owner, or for Blocked work (blocking releases ownership) the actor who blocked it."""
    if card.get("activeOwner"):
        return card["activeOwner"]
    if card["column"] == "blocked":
        blocked = [entry for entry in card.get("history") or [] if entry.get("ev") == "blocked"]
        return blocked[-1].get("by") if blocked else None
    return None


def cmd_digest(args):
    p = wb.find_board(args.board)
    doc = wb.load(p)
    by = wb.actor()
    ready = wb.ready_cards(doc)
    counts = {}
    for c in doc["cards"]:
        counts[c["column"]] = counts.get(c["column"], 0) + 1
    if args.json:
        print(json.dumps({
            "ok": True, "board": str(p), "name": doc["name"], "schemaVersion": doc["schemaVersion"],
            "rev": doc["rev"], "savedAt": doc["savedAt"], "stats": wb.board_stats(doc),
            "columns": [{**column, "count": counts.get(column["id"], 0)} for column in doc["columns"]],
            "attention": [{"id": card["id"], "num": card["num"], "title": card["title"],
                           "owner": card.get("activeOwner"), "attention": attention} for card in doc["cards"]
                          if (attention := _stage_attention(card))],
            "ready": [card["num"] for card in ready[:5]],
            "sweepCandidates": [card["id"] for card in wb._sweep_candidates(doc, 14)],
        }, ensure_ascii=False))
        return
    col_line = " · ".join(f"{c['name']}: {counts.get(c['id'], 0)}"
                          for c in doc["columns"] if counts.get(c["id"]))
    print(f"WorkBoard: {doc['name']} — rev {doc['rev']} · {len(doc['cards'])} cards"
          + (f" · saved {doc['savedAt']}" if doc.get("savedAt") else ""))
    mine = [card for card in doc["cards"] if _holder(card) == by]
    if mine:
        print(f"  MINE @{by}:")
        for card in mine:
            print(f"    {wb.fmt_ref(card)} {card['column']} {card['title'][:60]}")
    if col_line:
        print(f"  {col_line}")
    ip = [c for c in doc["cards"] if c["column"] == "inprogress"]
    for c in ip:
        subs = [s for s, _ in wb.iter_subtasks(c["subtasks"])]
        prog = f" ☑{sum(1 for s in subs if s['done'])}/{len(subs)}" if subs else ""
        attention = _stage_attention(c)
        marker = f" ⚠{attention}" if attention else ""
        owner = f" @{c['activeOwner']}" if c.get("activeOwner") else ""
        print(f"  IN PROGRESS: {wb.fmt_ref(c)}{owner} {c['title'][:60]}{prog}"
              f" ({_age(c['updatedAt'])}){marker}")
    done_cols = {c["id"] for c in doc["columns"] if c["kind"] == "done"} | {"done"}
    shipped = sorted((c for c in doc["cards"] if c["column"] in done_cols and c["doneAt"]
                      and c.get("outcome") != "canceled"),
                     key=lambda c: c["doneAt"], reverse=True)[:3]
    for c in shipped:
        print(f"  SHIPPED: {wb.fmt_ref(c)} {c['title'][:58]} ({_age(c['doneAt'])} ago)")
    blocked = [c for c in doc["cards"] if c["column"] == "blocked"]
    for c in blocked:
        reason = (c.get("blockedReason") or "reason missing").strip()
        until = (c.get("unblockWhen") or "condition missing").strip()
        owner = f" @{_holder(c)}" if _holder(c) else ""
        print(f"  BLOCKED: {wb.fmt_ref(c)}{owner} {c['title'][:48]} — {reason}; until {until}")
    canceled = [c for c in doc["cards"] if c.get("outcome") == "canceled"]
    for c in canceled[-3:]:
        print(f"  CANCELED: {wb.fmt_ref(c)} {c['title'][:48]} — {c.get('cancelReason') or ''}")
    refs = " — " + " ".join(wb.fmt_ref(card) for card in ready[:5]) if ready else ""
    print(f"  READY: {len(ready)}{refs} · REWORK: "
          f"{sum(bool(c.get('reworkReason')) for c in doc['cards'] if c['column'] != 'done')}")
    if not ip and not shipped:
        print("  (quiet board)")
    pending = wb._sweep_candidates(doc, 14)
    if pending:
        print(f"  sweep: {len(pending)} done cards older than 14d (operator task)")


def cmd_init(args):
    if args.board:
        raise wb.WorkflowError("init uses --dir, never --board")
    if wb.os.environ.get("WORKBOARD_SCOPE_ROOT") and not args.dir:
        raise wb.WorkflowError("scoped init requires an explicit --dir under WORKBOARD_SCOPE_ROOT")
    root = wb.require_write_scope(args.dir or Path.cwd())
    bdir = root / "board"
    p = wb.require_write_scope(bdir / "board.json")
    wb.require_write_scope(wb.registry_path())
    wb.registry_load()
    if p.exists():
        raise wb.WorkflowError(f"board already exists at {p}", 409)
    doc = {"name": args.name or root.name, "rev": 0,
           "nextNum": 1, "columns": [dict(c) for c in wb.DEFAULT_COLUMNS],
           "cards": []}
    bdir.mkdir(parents=True, exist_ok=True)
    wb.registry_path().parent.mkdir(parents=True, exist_ok=True)
    with wb.board_lock(wb.registry_path()):
        p = wb.canonical_registered_board(p)
        with wb.board_lock(p):
            p = wb.canonical_registered_board(p)
            if p.exists():
                raise wb.WorkflowError(f"board already exists at {p}", 409)
            wb.save(p, doc)
            wb.register_board(doc["name"], p)
    if args.json:
        print(json.dumps({"ok": True, "board": str(p), "rev": doc["rev"],
                          "actor": wb.actor(), "name": doc["name"]}))
    else:
        print(f"board created: {p} — view it with: workboard open")


def cmd_serve(args):
    from . import server
    server.serve(args)


def cmd_open(args):
    from . import server
    server.open_board(args)


def cmd_recover(args):
    p = wb.find_board(args.board)
    if args.apply:
        wb.require_write_scope(p)
    snaps = wb.list_backups(p)
    if not snaps:
        if args.apply:
            with wb.board_transaction(p, args.expected_rev):
                raise wb.WorkflowError("no recovery backups are available", 404)
        if args.json:
            print(json.dumps({"ok": True, "board": str(p), "backups": []}))
        else:
            print("(no backups)")
        return
    if args.apply:
        pick = None
        if args.rev:
            pick = next((path for rev, path in snaps if rev == args.rev), None)
            if pick is None:
                raise wb.WorkflowError(f"no backup for rev {args.rev}", 404)
        else:
            pick = snaps[0][1]
        raw = json.loads(wb.read_text_shared(pick))
        with wb.board_transaction(p, args.expected_rev) as current:
            restored = wb.normalize_doc(raw)
            restored["rev"] = current["rev"]
            restored["nextNum"] = max(restored["nextNum"], current["nextNum"])
            wb.save(p, restored)
        if args.json:
            print(json.dumps({"ok": True, "board": str(p), "rev": restored["rev"],
                              "actor": wb.actor(), "restoredFrom": str(pick)}))
        else:
            print(f"restored {pick.name} → {p}")
        return
    if args.json:
        print(json.dumps({"ok": True, "board": str(p),
                          "backups": [{"rev": rev, "path": str(path)} for rev, path in snaps]}))
        return
    print("available backups (newest first):")
    for rev, path in snaps[:10]:
        print(f"  rev {rev:<6} {path.name}")


def cmd_sweep(args):
    p = wb.find_board(args.board)
    moving = wb.sweep(p, days=args.days, apply=args.apply, expected_rev=args.expected_rev)
    if args.json:
        print(json.dumps({"ok": True, "board": str(p), "rev": wb.load(p)["rev"],
                          "actor": wb.actor(), "applied": args.apply, "cards": moving}, ensure_ascii=False))
        return
    if not moving:
        print(f"(nothing done-and-older-than-{args.days}d to archive)")
        return
    if args.apply:
        print(f"archived {len(moving)} cards → board/{wb.ARCHIVE_DIR}/ "
              f"(#{', #'.join(str(c['num']) for c in moving[:8])}"
              + (" …" if len(moving) > 8 else "") + ")")
    else:
        print(f"{len(moving)} cards would be archived — re-run with --apply")


def cmd_wip(args):
    p = wb.find_board(args.board)
    value = str(args.limit).strip().lower()
    if value in ("off", "none", "0"):
        limit = 0
    else:
        try:
            limit = int(value)
        except ValueError:
            raise wb.WorkflowError("wip limit must be off or an integer 1..20")
        if limit < 1 or limit > 20:
            raise wb.WorkflowError("wip limit must be off or an integer 1..20")
    with wb.board_transaction(p, args.expected_rev) as doc:
        column = _column(doc, "inprogress")
        if limit:
            column["wipLimit"] = limit
        else:
            column.pop("wipLimit", None)
        wb.save(p, doc)
    payload = {"ok": True, "rev": doc["rev"], "column": "inprogress",
               "actor": wb.actor(), "wipLimit": limit or None}
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"In Progress WIP limit → {limit if limit else 'off'} (rev {doc['rev']})")


def cmd_columns_core(args):
    p = wb.find_board(args.board)
    transaction = (wb.board_transaction(p, args.expected_rev) if args.apply
                   else contextlib.nullcontext(wb.load(p)))
    with transaction as doc:
        report = wb.consolidate_to_core_columns(doc)
        purged = report.pop("purgedCards")
        archive = None
        if args.apply:
            archive = wb.archive_removed_cards(
                p, purged, "five-column-consolidation"
            )
            wb.save(p, doc)
    payload = {
        "ok": True,
        "applied": bool(args.apply),
        "board": str(p),
        "rev": doc["rev"],
        "actor": wb.actor(),
        "columns": [column["id"] for column in doc["columns"]],
        "removedColumns": report["removedColumns"],
        "moved": report["moved"],
        "purged": len(purged),
        "archive": str(archive) if archive else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    mode = "consolidated" if args.apply else "would consolidate"
    print(
        f"{mode} → {', '.join(payload['columns'])} · "
        f"{sum(payload['moved'].values())} moved · {payload['purged']} purged"
        + (f" · archive {payload['archive']}" if payload["archive"] else "")
    )


def cmd_boards(args):
    from urllib.parse import quote
    port = int(wb.os.environ.get("WORKBOARD_PORT") or 7891)
    boards = wb.registry_load().get("boards", {})
    rows = [(name, path, f"http://127.0.0.1:{port}/b/{quote(name, safe='')}/")
            for name, path in sorted(boards.items())]
    if args.json:
        print(json.dumps({"ok": True, "boards": [
            {"name": name, "board": path, "url": url, "exists": Path(path).is_file()}
            for name, path, url in rows]}, ensure_ascii=False))
        return
    if not rows:
        print("(no registered boards — create one with: workboard init <name>)")
        return
    for name, path, url in rows:
        exists = "✓" if Path(path).exists() else "✗ missing"
        print(f"  {name:<20} {url}  {path} {exists}")


def cmd_which(args):
    p = wb.find_board(args.board)
    doc = wb.load(p)
    if args.json:
        print(json.dumps({"ok": True, "board": str(p), "name": doc["name"],
                          "schemaVersion": doc["schemaVersion"], "rev": doc["rev"],
                          "cards": len(doc["cards"])}, ensure_ascii=False))
        return
    print(f"{p} — '{doc['name']}' rev {doc['rev']} · {len(doc['cards'])} cards")


# ===== parser =====



def _global_arguments(parser, *, root=False):
    parser.add_argument("--board",
                        default=None if root else argparse.SUPPRESS,
                        help="project root or board.json path")
    parser.add_argument("--json", action="store_true",
                        default=False if root else argparse.SUPPRESS,
                        help="machine-readable result")
    parser.add_argument("--actor", default=None if root else argparse.SUPPRESS,
                        help="actor label overriding WORKBOARD_ACTOR")


def _revision(value):
    try:
        revision = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("revision must be a nonnegative integer")
    if revision < 0:
        raise argparse.ArgumentTypeError("revision must be a nonnegative integer")
    return revision


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="workboard", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"workboard {__version__}")
    _global_arguments(ap, root=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, **kw):
        sp = sub.add_parser(name, help=help, allow_abbrev=False)
        _global_arguments(sp)
        if name in {"start", "done", "fly", "block", "resume", "update", "note",
                    "subtask", "wip", "bug", "improve", "reopen", "workpad", "takeover",
                    "cancel", "rework", "depends", "comment", "recover", "columns-core", "sweep"}:
            sp.add_argument("--expected-rev", type=_revision,
                            help="reviewed revision: card-scoped for card verbs; exact board revision for maintenance")
        sp.set_defaults(fn=fn)
        return sp

    p = add("add", cmd_add, "create a card")
    p.add_argument("--title", required=True)
    p.add_argument("--column", default="task")
    p.add_argument("--priority", choices=["critical", "mid", "low"])
    p.add_argument("--tag", action="append")
    origin = p.add_mutually_exclusive_group()
    origin.add_argument("--origin")
    origin.add_argument("--origin-stdin", action="store_true")
    p.add_argument("--on", action="append", help="prerequisite card reference (repeatable)")

    p = add("start", cmd_start, "fly a card to In Progress")
    p.add_argument("ref")

    p = add("done", cmd_done, "ship a card to Done (writeup required)")
    p.add_argument("ref")
    writeup = p.add_mutually_exclusive_group()
    writeup.add_argument("--writeup")
    writeup.add_argument("--writeup-stdin", action="store_true")

    p = add("fly", cmd_fly, "move between backlog/task/inprogress (Blocked via block/resume, Done via done)")
    p.add_argument("ref")
    p.add_argument("column")
    p.add_argument("--note")

    p = add("block", cmd_block, "move committed work to Blocked with an actionable reason")
    p.add_argument("ref")
    reason = p.add_mutually_exclusive_group(required=True)
    reason.add_argument("--reason")
    reason.add_argument("--reason-stdin", action="store_true")
    p.add_argument("--until", required=True)

    p = add("resume", cmd_resume, "leave Blocked with a resolution note")
    p.add_argument("ref")
    p.add_argument("--note", required=True)
    p.add_argument("--to", choices=["task", "inprogress"], default="inprogress")

    p = add("update", cmd_update, "edit card fields")
    p.add_argument("ref")
    p.add_argument("--title")
    p.add_argument("--priority", choices=["critical", "mid", "low"])
    p.add_argument("--add-tag", action="append")
    p.add_argument("--rm-tag", action="append")
    notes = p.add_mutually_exclusive_group()
    notes.add_argument("--notes")
    notes.add_argument("--notes-stdin", action="store_true")

    p = add("note", cmd_note, "append a timeline note: one-line summary, optional markdown body")
    p.add_argument("ref")
    p.add_argument("--summary", required=True, help="one line, at most 160 characters")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body", help="markdown body")
    body.add_argument("--stdin", action="store_true", help="read the markdown body from stdin")

    p = add("subtask", cmd_subtask, "manage subtasks: add/done/undone/rm")
    p.add_argument("ref")
    p.add_argument("op", choices=["add", "done", "undone", "rm"])
    p.add_argument("what", nargs="+", help="one or more texts (add) or subtask IDs")
    p.add_argument("--parent", help="parent subtask id for nesting")
    p = add("wip", cmd_wip, "set the optional In Progress WIP limit")
    p.add_argument("limit", help="off or integer 1..20")


    p = add("bug", cmd_bug, "reopen Done→In Progress with a bug tag + fix subtask")
    p.add_argument("ref")
    p.add_argument("--reason", required=True)

    p = add("improve", cmd_improve, "reopen Done→In Progress for an enhancement")
    p.add_argument("ref")
    p.add_argument("text")

    p = add("reopen", cmd_reopen, "reopen a Done card back to Task with a reason")
    p.add_argument("ref")
    p.add_argument("--reason", required=True)
    p.add_argument("--as", dest="as_", choices=["task", "bug", "improve"])

    p = add("next", cmd_next, "read-only priority/age-ranked ready work")
    p.add_argument("--limit", type=int, default=5)

    p = add("workpad", cmd_workpad, "seed missing acceptance and verification notes sections")
    p.add_argument("ref")

    for verb in ("takeover", "cancel", "rework"):
        p = add(verb, cmd_reasoned_action, f"{verb} a card with an explicit reason")
        p.add_argument("ref")
        p.add_argument("--reason", required=True)

    p = add("depends", cmd_depends, "add, remove, or clear prerequisite references")
    p.add_argument("ref")
    p.add_argument("--on", action="append")
    p.add_argument("--remove", action="append")
    p.add_argument("--clear", action="store_true")

    p = add("comment", cmd_comment, "add text, edit ID text, or delete ID")
    p.add_argument("ref")
    p.add_argument("op", choices=["add", "edit", "delete"])
    p.add_argument("what", nargs="?", help="comment text (add) or immutable comment ID")
    p.add_argument("text", nargs="?", help="replacement text (edit)")
    p.add_argument("--stdin", action="store_true", help="read add/edit text from stdin")

    p = add("context", cmd_context, "card, discussion, files, readiness, and bounded completed work/history")
    p.add_argument("ref")
    p.add_argument("--full", action="store_true", help="include all done subtasks, history, and notes")

    p = add("attachment", cmd_attachment, "list, add, export, or detach a card attachment")
    p.add_argument("ref")
    operations = p.add_subparsers(dest="op", required=True)
    for operation in ("list", "add", "get", "remove"):
        operation_parser = operations.add_parser(operation, allow_abbrev=False)
        _global_arguments(operation_parser)
        if operation in ("get", "remove"):
            operation_parser.add_argument("id")
        if operation in ("add", "remove"):
            operation_parser.add_argument("--expected-rev", type=_revision)
        if operation == "add":
            operation_parser.add_argument("--file", required=True)
            operation_parser.add_argument("--name")
            operation_parser.add_argument("--mime")
        if operation == "get":
            operation_parser.add_argument("--out", required=True)

    p = add("show", cmd_show, "print one card as JSON")
    p.add_argument("ref")
    p.add_argument("--full", action="store_true")

    p = add("list", cmd_list, "human listing of cards")
    p.add_argument("--column")
    p.add_argument("--tag")
    p.add_argument("--priority", choices=["critical", "mid", "low"])

    p = add("query", cmd_query, "JSON projection of filtered cards")
    p.add_argument("--column")
    p.add_argument("--tag")
    p.add_argument("--priority", choices=["critical", "mid", "low"])
    owner = p.add_mutually_exclusive_group()
    owner.add_argument("--owner", help="filter by exact owner label")
    owner.add_argument("--mine", action="store_true", help="filter by effective actor")
    p.add_argument("--since-days", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--fields", help="comma list: num,id,title,column,priority,tags,outcome,owner,deps,changedRev,createdAt,updatedAt,doneAt,origin")

    p = add("search", cmd_search, "ANDed substring search across all text")
    p.add_argument("terms", nargs="+")

    add("digest", cmd_digest, "~15-line board pulse; read this first")
    add("boards", cmd_boards, "list registered boards")
    add("which", cmd_which, "print resolved board path + counts")

    p = add("init", cmd_init, "create and register a board in cwd (or --dir)")
    p.add_argument("name", nargs="?")
    p.add_argument("--dir")

    p = add("serve", cmd_serve, "run the local board server for every registered board (foreground)")
    p.add_argument("--port", type=int, help="TCP port on 127.0.0.1 (default: $WORKBOARD_PORT or 7891)")
    p.add_argument("--open", action="store_true", help="open this project's board in the browser")
    p.add_argument("--service", action="store_true", help="background-service mode: log to file, never open a browser")

    add("open", cmd_open, "open this project's board in the browser, starting the server if needed")

    from . import doctor, install, update
    for module in (install, update, doctor):
        module.register(add)

    p = add("recover", cmd_recover, "list/restore .backups snapshots")
    p.add_argument("rev", nargs="?", type=int)
    p.add_argument("--apply", action="store_true")

    p = add("columns-core", cmd_columns_core,
            "consolidate legacy columns into the five core workflow columns")
    p.add_argument("--apply", action="store_true")

    p = add("sweep", cmd_sweep, "archive Done cards older than N days")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--apply", action="store_true")

    return ap


def main(argv=None):
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    options = argv[:argv.index("--")] if "--" in argv else argv
    if sum(token == "--board" or token.startswith("--board=") for token in options) > 1:
        ap.error("--board may be specified only once")
    args = ap.parse_args(argv)
    if args.cmd == "init" and args.board is not None:
        ap.error("init uses --dir as its destination; --board is not accepted")
    if args.cmd in ("recover", "columns-core", "sweep") and args.expected_rev is not None and not args.apply:
        ap.error("--expected-rev on maintenance requires --apply")
    if args.cmd == "comment":
        if args.op == "add" and (args.text is not None or (args.what is not None) == args.stdin):
            ap.error("comment add requires text or --stdin, not both")
        if args.op == "edit" and (args.what is None or (args.text is not None) == args.stdin):
            ap.error("comment edit requires an ID and replacement text or --stdin")
        if args.op == "delete" and (args.what is None or args.text is not None or args.stdin):
            ap.error("comment delete requires only an ID")
    if args.cmd == "subtask" and args.parent and args.op != "add":
        ap.error("--parent applies only to subtask add")
    previous_actor = wb.os.environ.get("WORKBOARD_ACTOR")
    try:
        if args.actor is not None:
            wb.os.environ["WORKBOARD_ACTOR"] = wb.validate_actor(args.actor)
        wb.actor()
        args.fn(args)
    except (wb.WorkflowError, wb.RefError, wb.LockTimeout, wb.UnsafeBoardPath,
            wb.RegistryConflict, OSError, ValueError, KeyError, TypeError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and isinstance(exc.code, int):
            raise
        status = getattr(exc, "status", 404 if isinstance(exc, (FileNotFoundError, wb.RefError))
                         else 409 if isinstance(exc, (FileExistsError, wb.RegistryConflict))
                         else 500 if isinstance(exc, (OSError, wb.LockTimeout)) else 422)
        code = (exc.code if isinstance(exc, wb.WorkflowError) else
                "lock" if isinstance(exc, wb.LockTimeout) else
                "scope" if isinstance(exc, wb.UnsafeBoardPath) else
                "not_found" if isinstance(exc, wb.RefError) else
                "io" if isinstance(exc, OSError) else "invalid")
        message = str(exc).removeprefix("error: ")
        if args.json:
            rev = getattr(exc, "rev", None)
            if rev is None:
                try:
                    rev = wb.load(wb.find_board(args.board))["rev"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            error = {"ok": False, "status": status, "code": code, "error": message, "rev": rev}
            if isinstance(exc, wb.RevisionConflict) and exc.card is not None:
                error["card"] = exc.card
            print(json.dumps(error, ensure_ascii=False))
            raise SystemExit(1)
        print(f"error [{code}]: {message}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if args.actor is not None:
            if previous_actor is None:
                wb.os.environ.pop("WORKBOARD_ACTOR", None)
            else:
                wb.os.environ["WORKBOARD_ACTOR"] = previous_actor


if __name__ == "__main__":
    main()
