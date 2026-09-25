# HTTP API

The per-user server (`workboard serve`, or the background service) speaks HTTP/1.1 on `http://127.0.0.1:<port>/`. The port is `--port`, else `WORKBOARD_PORT`, else `port` in `~/.workboard/config.json`, else `7891`. The board UI uses exactly this API. Agents normally use the [CLI](cli.md) instead, which works without the server.

- Server routes live at the root.
- Board routes live under `/b/<enc>/`, where `<enc>` is the registered board name percent-encoded with `urllib.parse.quote(name, safe="")` (JavaScript: `encodeURIComponent`).

## Rules for every request

| Rule | Detail |
|---|---|
| Host | `Host` must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`; otherwise 403. This blocks DNS-rebinding pages. |
| Origin | If a request sends `Origin`, it must be `http://127.0.0.1:<port>` or `http://localhost:<port>`; otherwise 403. |
| Content type (JSON writes) | `application/json` (parameters ignored), else 415. `POST board.json` also accepts `text/plain` for `navigator.sendBeacon`. |
| Body framing | Exactly one numeric `Content-Length` and no `Transfer-Encoding`, else 400. |
| Body size | JSON bodies up to 32 MiB and attachments up to 10 MiB, else 413. An empty JSON body is 400. A body that takes more than 15 s to read is 408. |
| JSON body | Must parse as a JSON object, else 400. |
| Base revision | Board writes need the revision the client last saw: the `X-Board-Base-Rev` header, else body `baseRev`, else legacy body `rev` (treated as `rev - 1`). A missing, boolean or negative value is 400. |
| Revision check | Browser writes are board-scoped: the board `rev` must equal the base revision exactly, otherwise 409. Nothing is retried or merged. |
| Rev bump | Every successful write increments `rev` (even a no-op such as starting a card you already own) and sets `savedAt`/`savedBy`. Use the `rev` in the response as the next base revision. |
| Actor | JSON writes take body `actor` (default `"user"`). Attachment uploads take the URL-encoded `X-WorkBoard-Actor` header. The label must be nonblank, at most 80 characters and free of control characters, else 422. |
| Card `{ref}` | URL-decoded, then matched by number (a leading `#` is stripped; encode it as `%23`), exact ID, unique case-insensitive code, or unique ID prefix. |
| Caching | Every response sends `Cache-Control: no-store`. Responses with status 400 or higher also send `Connection: close`. |
| Lock | Writes wait up to 5 s for the board lock. A timeout is 500 `board unavailable: …`. |

### Error bodies

| Cause | Status | Body |
|---|---|---|
| Unknown board name | 404 | `{"error": "unknown_board", "name": "<name>"}` |
| Registered board file is missing | 410 | `{"error": "board_missing", "board": "<abs path>"}` |
| Revision conflict | 409 | `{"ok": false, "status": 409, "conflict": true, "error": "board changed; current revision is N; refresh context before retrying", "rev": N}` |
| Workflow or validation error | its status (400, 404, 409, 413, 415, 422, …) | `{"ok": false, "status": S, "error": "…", "conflict": S == 409}` |
| Unknown card | 404 | `{"ok": false, "status": 404, "error": "…"}` |
| Anything else | 500 | `{"error": "board unavailable: …"}` |
| Unknown route | 404 | `{"error": "not found"}` |

A 409 is not always a revision conflict: ownership, dependency, WIP and state violations also use 409. Branch on `conflict` together with `rev`, not on the status alone.

## Server routes

### `GET /health`

```json
{"ok": true, "app": "workboard", "version": "0.1.0", "apiVersion": 2, "schemaVersion": 3,
 "supportedSchemaVersions": [1, 2, 3], "capabilities": ["context", "..."],
 "pid": 1234, "port": 7891, "startedAt": "2026-09-24T08:00:00Z", "boards": 3, "sseClients": 1}
```

`boards` is the number of registered boards. Use this route for readiness checks. SSE streams stay open, so network-idle heuristics never settle.

### `GET /`

The board UI with no board selected. It lists the registered boards.

### `GET /api/boards`

```json
{"boards": [{"name": "my-project", "board": "/home/me/.workboard/boards/my-project/board.json",
             "project": "/home/me/src/my-project", "url": "/b/my-project/", "exists": true,
             "rev": 42, "cards": 17, "error": null}]}
```

Sorted case-insensitively by name. `board` is the board file in the WorkBoard home and `project` the linked project folder. For a missing or unreadable board, `rev` and `cards` are `null` and `error` explains why.

### `POST /api/boards/delete`

Body: `{"name", "board", "baseRev"}`. `board` must be the exact registered board file, as listed by `/api/boards`. `baseRev` is required: the board revision you reviewed, or `null` if the board file is expected to be gone already. The board's whole folder, with its backups, archives and attachments, moves to `~/.workboard/deleted/<dir>-<UTC timestamp>/`, and the name is unregistered. The project folder is never touched. Response: `{"ok": true, "recoveryPath": "<moved folder, or null if the board folder was already gone>", "board": "<path>"}`. Errors: 400 for invalid fields, 404 for an unregistered name, 409 `{"ok": false, "conflict": true, "error"}` for a stale revision or a changed registration, and 500 for a lock timeout or I/O failure.

### `POST /api/shutdown`

Requires the header `X-WorkBoard-Token: <token>`, with the token read from `~/.workboard/server.json`. Responds `{"ok": true}`, then stops gracefully. A wrong or missing token is 403. `workboard service remove|restart` and `workboard upgrade` use this route.

### `GET /b/<enc>` and `GET /b/<enc>/`

`/b/<enc>` redirects (301) to `/b/<enc>/`, which serves the board UI for that board.

## Board routes

All paths below are relative to `/b/<enc>/`.

| Method | Path | Purpose |
|---|---|---|
| GET | `board.json` | The full document |
| POST | `board.json` | Save a full snapshot |
| GET | `api/bootstrap` | `{"state": document}` |
| GET | `rev` | The revision as `text/plain` |
| GET | `events` | Server-Sent Events |
| GET | `api/ready` | Ready cards |
| GET | `api/stats` | Board statistics |
| GET | `api/git` | Local read-only git status of the project |
| GET | `api/cards?column=&offset=&limit=` | One page of a column |
| GET | `api/card/{ref}` | One card |
| GET | `api/card/{ref}/context` | A card with its dependencies and dependents |
| PATCH | `api/card/{ref}` | Edit fields and position |
| PATCH | `api/card/{ref}/lifecycle` | Lifecycle action |
| PATCH | `api/card/{ref}/comments` | Add, edit or delete a comment |
| PATCH | `api/structure` | Create, delete or sort cards, edit columns, update the document |
| POST | `api/card/{ref}/attachments?name=` | Upload an attachment (raw bytes) |
| GET | `api/card/{ref}/attachments/{attId}` | Download an attachment |
| DELETE | `api/card/{ref}/attachments/{attId}` | Detach an attachment |

Every JSON write response includes `ok`, `rev`, `savedAt`, `savedBy` and the saved `document`.

### Reads

| Route | Response |
|---|---|
| `GET board.json` | The document (see [Schema](#schema)). An unsupported `schemaVersion` is 409 with `conflict: true` and no `rev`. |
| `GET api/ready` | `{"rev", "cards"}`: unowned Task cards whose dependencies are completed, sorted by priority, then `createdAt`, `num` and `id`. |
| `GET api/stats` | `{"rev", "stats": {"total", "open", "ready", "blocked", "inprogress", "completed", "canceled", "rework", "completedLast7Days", "byColumn", "byPriority", "byOwner"}}` |
| `GET api/git` | `{"state": "clean"\|"dirty"\|"not_repo"\|"error"\|"unavailable", "root", "branch", "head", "subject", "ahead", "behind", "staged", "unstaged", "untracked", "conflicted", "files": [{"path", "index", "worktree"}] (at most 200), "truncated", "error", "warning"}`. Never fetches or writes. Git runs with an 8 s budget, so call it on demand rather than on a timer. |
| `GET api/cards` | `{"column", "cards", "total", "rev"}`. `limit` is clamped to 1–250 (default 50) and `offset` must be 0 or more. Invalid values are 400. |
| `GET api/card/{ref}` | `{"card", "rev"}` |
| `GET api/card/{ref}/context` | `{"ok", "board", "schemaVersion", "rev", "card", "dependencies": [{"id", "num", "title", "column", "outcome", "satisfied"}], "missingDependencies", "dependents": [{"num", "id", "title", "column", "outcome"}], "ready", "omitted"?}`. The card is trimmed to the 10 most recent done subtasks, the last 25 history entries and the newest 10 `log` entries; `omitted` reports what was trimmed (`doneSubtasks`, `history`, plus `log` when `log` entries were dropped). |

### `POST board.json`: snapshot save

```jsonc
{ "baseRev": 12, "actor": "user", "schemaVersion": 3,
  "columns": [/* the five core columns, in display order */],
  "cards": [/* every card, in display order */],
  "title": "…", "name": "…", "tagTaxonomy": {}, "activeWork": null, "activeWorkId": null }
```

- `columns` and `cards` are required. Each card needs a string `id`, and ids must be unique (422).
- Server-owned card fields may be sent only unchanged (422 otherwise): `id`, `num`, `createdAt`, `updatedAt`, `history`, `changedRev`, `activeOwner`, `claimedAt`, `dependsOn`, `outcome`, `cancelReason`, `reworkReason`, `reopenReason`, `blockedReason`, `unblockWhen`, `blockedAt`, `doneAt`, `comments`, `attachments`, `cycles`, `verification`, `reviews` and `log`.
- A snapshot cannot delete cards (422); use `delete-card`. Existing cards are updated as with `PATCH api/card/{ref}`, and a column change runs the lifecycle `move` checks. New cards are created as with `create-card`.
- Every card is touched, so every card's `updatedAt` and `changedRev` change. Prefer the targeted routes.
- Response: `{"ok", "rev", "savedAt", "savedBy", "document"}`.

### `PATCH api/card/{ref}`: edit a card

```jsonc
{ "baseRev": 12, "actor": "user",
  "card": { /* code, title, column, priority, tags, origin, notes, writeup, subtasks, links, lastTouchedSubtask, meta, agentRuns */ },
  "position": {"before": "<card id>|null", "after": "<card id>|null"},
  "document": { /* title, name, tagTaxonomy, activeWork, activeWorkId */ } }
```

- `card` is required. Other keys in it are dropped, and server-owned fields must be unchanged. For example, a changed `log` is 422 `'log' is server-owned; use its dedicated action`; add timeline entries with the `note` lifecycle action.
- `subtasks` replaces the whole tree. Every item needs a string `id`. If an item omits `children`, its existing children are kept.
- Changing `column` runs the lifecycle `move` action, including owner, WIP, dependency and state checks. Field edits alone don't check ownership.
- `position` reorders cards within the display order: `before` wins over `after`. It never changes the column.
- Response adds `card`, `positioned` and `"event": "card-updated"`.

### `PATCH api/card/{ref}/lifecycle`

Body: `{"baseRev", "actor"?, "action", "details": {}}`. If the card has another owner, every action except `takeover`, `workpad` and `note` is 409. Required text is trimmed and must not be blank.

| `action` | `details` | Allowed from | Effect |
|---|---|---|---|
| `start` | `note`? | not Blocked (use `resume`), not Done | In Progress, claimed by the actor. Dependencies and the WIP limit are enforced. A no-op if you already own it. |
| `submit` | `summary`, `verification` | In Progress | Completes with write-up `summary` + `Verification: verification`. |
| `complete` | `writeup` | In Progress | Done, outcome `completed`, owner cleared. |
| `block` | `reason`, `until` | not Done | Blocked, owner cleared. |
| `resume` | `note`, `to`? (`task`\|`inprogress`, default `inprogress`) | Blocked | Leaves Blocked. `inprogress` runs the `start` checks. |
| `takeover` | `reason` | In Progress | Owner becomes the actor. |
| `cancel` | `reason` | not Done | Done, outcome `canceled`. |
| `rework` | `reason` | any | Task. Clears the write-up and archives a completed cycle. |
| `reopen` | `reason`, `to`? (`task`\|`inprogress`), `as`? (`task`\|`bug`\|`improve`) | any | Like `rework`, moving to `to` (default `task`). |
| `bug` | `reason` | any | In Progress with the `bug` tag and a `fix bug:` subtask. |
| `improve` | `text` | any | In Progress with an improvement subtask. |
| `move` | `to`, `writeup`?, `note`? | not Blocked, not Done | To Done runs `complete`, to In Progress runs `start`, and moving to Blocked is 422 (use `block`). |
| `dependencies` | `ids` (the full list; `[]` clears) | any | Rejects self-dependencies, unknown ids and cycles. |
| `workpad` | | any | Adds missing acceptance and verification sections to the pinned notes. |
| `note` | `summary`, `body`? | any | Appends an entry to the card's `log` (see [Notes timeline](#notes-timeline)). `summary` is trimmed and must be one line of 1–160 characters; `body` is markdown of at most 32,000 characters (`null` or omitted means `""`). Otherwise 422. |

An unknown action is 422. The response adds `card`, `action` and `"event": "card-updated"`.

Adding a note:

```jsonc
// PATCH /b/my-project/api/card/12/lifecycle
{ "baseRev": 42, "actor": "user", "action": "note",
  "details": {"summary": "Chose SQLite over JSON files",
              "body": "- Concurrent writers need row locks\n- Commit `abc1234`"} }

// 200
{ "ok": true, "rev": 43, "savedAt": "2026-09-24T08:15:02Z", "savedBy": "user",
  "card": { /* other card fields; "log" ends with the new entry */
            "log": [{"id": "9b1c2e7f40a84d6c8e3f5a2b1d0c9e7f", "at": "2026-09-24T08:15:02Z", "by": "user",
                     "summary": "Chose SQLite over JSON files",
                     "body": "- Concurrent writers need row locks\n- Commit `abc1234`"}] },
  "document": { /* the saved document */ }, "event": "card-updated", "action": "note" }
```

A stale `baseRev` is 409 like any other write.

### `PATCH api/card/{ref}/comments`

Body: `{"baseRev", "actor"?, "operation"}`, where the operation is one of `{"type": "add", "text"}`, `{"type": "edit", "id", "text"}` or `{"type": "delete", "id"}`. Text is trimmed, nonblank and at most 16,000 characters. Extra keys are 422 and an unknown id is 404. The response adds `card` and `comment` (`null` after a delete). The server doesn't check authorship; clients must not edit or delete other people's comments.

### `PATCH api/structure`

Body: `{"baseRev", "actor"?, "operation": {"type", …}}`. The response adds `operation`, `card`, `sourceCard`, `cardCounts` and `totalCards`.

| `type` | Fields | Behavior |
|---|---|---|
| `create-card` | `card` | `card.id` is required: 32 hex characters or a UUID, optionally preceded by a lowercase slug and `-` (at most 120 characters). It must not already be used as a card id, dependency or link (409). `column` is `backlog`, `task` (default) or `inprogress`, and `title` is required. The server assigns `num` and timestamps and appends the card. Creating in In Progress runs the `start` checks. |
| `create-follow-up` | `sourceCardId`, `card` | The source card must be Done. The new card and the source link to each other. |
| `delete-card` | `cardId` | A card owned by someone else is 409. Links and dependencies that point to it are left as they are. |
| `sort-cards` | `mode: "created-desc"` | Sorts by column, then newest first. |
| `update-columns` | `columns` | Exactly the five core column ids, in display order. Each entry is `{"id", "name"?, "kind"?, "wipLimit"?, "stackUnder"?}`. `wipLimit`: an integer sets it, `null` removes it, and omitting it keeps it. An omitted `stackUnder` unstacks the column. |
| `update-document` | `changes` | `title`, `name`, `tagTaxonomy`, `activeWork`, `activeWorkId` |

### Attachments

- **Upload:** `POST api/card/{ref}/attachments?name=<encodeURIComponent(name)>`. The body is the raw bytes (up to 10 MiB), and `X-Board-Base-Rev` is required as a header. `Content-Type` must be a bare `type/subtype`; if absent, it is guessed from the name. The response adds `card` and `attachment: {"id", "name", "size", "mime", "sha256", "createdAt", "by"}`.
- **Download:** `GET api/card/{ref}/attachments/{attId}`, where `attId` is 32 hex characters. It is served as `application/octet-stream` with `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`. A size or hash mismatch is 409 and missing bytes are 404.
- **Detach:** `DELETE api/card/{ref}/attachments/{attId}` with body `{"baseRev", "actor"?}`. Only the metadata is removed; the bytes stay on disk.

## Server-Sent Events

`GET /b/<enc>/events` streams `text/event-stream`. The stream opens with the comment `: connected` and sends `: keepalive` after 15 s without events. It works even when the board file is missing.

| Event | Data | When |
|---|---|---|
| `rev-bumped` | `{"rev": N}` | `board.json` changed and loads |
| `board-missing` | `{"board": "<path>"}` | `board.json` vanished or doesn't load |
| `resync-required` | `{}` | Right after either of the above |

A client should fetch `board.json` on connect and on every `resync-required`, and replace its state if `rev` differs. A `rev-bumped` event with a revision at or below its own is the echo of its own write. Events for one board never reach subscribers of another board. Each client queue holds 256 events; overflow is dropped.

## Schema

The document (`schemaVersion` 3) contains `name`, `title`?, `rev`, `nextNum`, `savedAt`, `savedBy`, `columns` and `cards` (array order is display order), plus optional `tagTaxonomy`, `activeWork` and `activeWorkId`. Unknown top-level keys on disk are preserved. Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`.

Every read returns the normalized version 3 document. A version 1 or 2 file is upgraded in memory, with legacy card notes split into the timeline (see [Architecture](architecture.md#schema-3-and-the-notes-timeline)), and is written as version 3 on the next save.

A column is `{"id", "name", "kind", "stackUnder": id|null, "wipLimit"?}`. Only the In Progress `wipLimit` is enforced.

Card fields:

| Field | Notes |
|---|---|
| `id`, `num` | Immutable, server-assigned |
| `code`, `title`, `origin`, `writeup` | Editable strings |
| `notes` | Pinned notes: an editable markdown string for durable context such as acceptance criteria |
| `log` | The notes timeline (see [Notes timeline](#notes-timeline)). Server-owned |
| `column` | `backlog`, `task`, `inprogress`, `done` or `blocked` |
| `priority` | `critical`, `mid`, `low` or `null` |
| `tags`, `links` | String arrays |
| `subtasks` | A tree of `{"id", "text", "done", "createdAt", "doneAt", "collapsed", "children", "by"?, "doneBy"?}` |
| `dependsOn` | Card ids, set through the `dependencies` action |
| `activeOwner`, `claimedAt` | Set only while In Progress |
| `outcome` | `completed` or `canceled` in Done, otherwise `null` |
| `blockedReason`, `unblockWhen`, `blockedAt`, `cancelReason`, `reworkReason`, `reopenReason`, `doneAt` | Lifecycle state |
| `comments` | `[{"id", "at", "by", "text", "updatedAt"?, "updatedBy"?}]` |
| `attachments` | `[{"id", "name", "size", "mime", "sha256", "createdAt", "by"}]` |
| `history` | The last 40 `{"at", "ev", "from"?, "to"?, "by"?, "note"?}` entries |
| `cycles` | Archived completions from rework and reopen |
| `changedRev` | The board revision that last changed this card |
| `createdAt`, `updatedAt` | Server timestamps |
| `lastTouchedSubtask`, `meta`, `agentRuns` | Passed through unchanged |

### Notes timeline

`log` is a list of entries in append order (oldest first):

```json
{"id": "9b1c2e7f40a84d6c8e3f5a2b1d0c9e7f", "at": "2026-09-24T08:15:02Z", "by": "codex-auth",
 "summary": "Fixed flush race; tests pass", "body": "- Commit `abc1234`\n- Tests: 42/42"}
```

| Key | Rule |
|---|---|
| `id` | 32 lowercase hex characters, unique within the card |
| `at` | UTC `YYYY-MM-DDTHH:MM:SSZ`; `YYYY-MM-DD` for entries migrated from legacy notes |
| `by` | The actor label, or `null` for migrated entries whose stamp had none |
| `summary` | One line, 1–160 characters, no line breaks |
| `body` | Markdown, at most 32,000 characters, trailing whitespace stripped; `""` when empty |

Entries are added only through the `note` lifecycle action (or the CLI's `note`). A card PATCH or snapshot that changes `log` is 422.
