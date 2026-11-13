# HTTP API

The per-user server (`workboard serve`, or the background service) speaks HTTP/1.1 on `http://127.0.0.1:<port>/`. The port is `--port`, else `WORKBOARD_PORT`, else `port` in `~/.workboard/config.json`, else `7891`. The board UI uses exactly this API. Agents normally use the [CLI](cli.md) instead, which works without the server.

- Server routes live at the root.
- Board routes live under `/b/<enc>/`, where `<enc>` is the registered board name percent-encoded with `urllib.parse.quote(name, safe="")` (JavaScript: `encodeURIComponent`).

## Rules for every request

| Rule | Detail |
|---|---|
| Host | `Host` must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`; otherwise 403. This blocks DNS-rebinding pages. |
| Origin | If a request sends `Origin`, it must be `http://127.0.0.1:<port>` or `http://localhost:<port>`; otherwise 403. |
| Content type (JSON writes) | `application/json` (parameters ignored), else 415. |
| Body framing | Exactly one numeric `Content-Length` and no `Transfer-Encoding`, else 400. |
| Body size | JSON bodies up to 32 MiB and attachments up to 10 MiB, else 413. An empty JSON body is 400. A body that takes more than 15 s to read is 408. |
| JSON body | Must parse as a JSON object, else 400. |
| Base revision | Board writes need the revision the client last saw: the `X-Board-Base-Rev` header, else body `baseRev`. A missing, boolean or negative value is 400. |
| Revision check | Browser writes are board-scoped: the board `rev` must equal the base revision exactly, otherwise 409. Nothing is retried or merged. |
| Rev bump | Every successful write increments `rev` (even a no-op such as starting a card you already own) and sets `savedAt`/`savedBy`. Use the `rev` in the response as the next base revision. |
| Actor | JSON writes take body `actor` (default `"user"`). Attachment uploads take the URL-encoded `X-WorkBoard-Actor` header. The label must be nonblank, at most 80 characters and free of control characters, else 422. |
| Card `{ref}` | URL-decoded, then matched by number (a leading `#` is stripped; encode it as `%23`), exact ID, unique case-insensitive code, or unique ID prefix. |
| Caching | Every response sends `Cache-Control: no-store`. Responses with status 400 or higher also send `Connection: close`. |
| Store busy | Writes wait while other writers hold the board's store (see [Writes](architecture.md#writes)). A timeout is 500 `board unavailable: …`. |

### Error bodies

| Cause | Status | Body |
|---|---|---|
| Unknown board name | 404 | `{"error": "unknown_board", "name": "<name>"}` |
| Registered board is missing | 410 | `{"error": "board_missing", "board": "<abs path>"}` |
| Revision conflict | 409 | `{"ok": false, "status": 409, "conflict": true, "error": "board changed; current revision is N; refresh context before retrying", "rev": N}` |
| Workflow or validation error | its status (400, 404, 409, 413, 415, 422, …) | `{"ok": false, "status": S, "error": "…", "conflict": S == 409, "code"?: "owned"}`. `code` is the [CLI error code](cli.md#output) (`owned`, `state`, `deps`, `wip`, `invalid`, …) whenever a workflow rule refused the request. |
| Unknown card | 404 | `{"ok": false, "status": 404, "error": "…"}` |
| Anything else | 500 | `{"error": "board unavailable: …"}` |
| Unknown route | 404 | `{"error": "not found"}` |

A 409 is not always a revision conflict: ownership, dependency, WIP and state violations also use 409. Branch on `conflict` together with `rev`, not on the status alone; a refusal by a workflow rule carries `code` instead of `rev`.

## Server routes

### `GET /health`

```json
{"ok": true, "app": "workboard", "version": "0.4.0", "apiVersion": 2, "schemaVersion": 4,
 "supportedSchemaVersions": [3, 4],
 "capabilities": ["context", "attachment-cli", "shared-attachments", "expected-rev", "schema-guard",
                  "delegated-subtasks", "focused-context", "handoff", "write-scopes", "review-inbox", "subtask-notes"],
 "pid": 1234, "port": 7891, "startedAt": "2026-09-24T08:00:00Z", "boards": 3, "registryError": null, "sseClients": 1}
```

`boards` is the number of registered boards (`null`, with the reason in `registryError`, when the registry can't be read). `schemaVersion` is the board schema the server reads and writes (see [Schema](#schema)). Use this route for readiness checks. SSE streams stay open, so network-idle heuristics never settle. `workboard service status`, `service restart` and `doctor` compare `version` with the CLI's.

### `GET /`

The board UI with no board selected. It lists the registered boards.

### `GET /api/boards`

```json
{"boards": [{"name": "my-project", "board": "/home/me/.workboard/boards/my-project/board.json",
             "project": "/home/me/src/my-project", "url": "/b/my-project/", "exists": true,
             "rev": 42, "cards": 17, "error": null}]}
```

Sorted case-insensitively by name. `board` is the board's `board.json` in the WorkBoard home and `project` the linked project folder. For a missing or unreadable board, `rev` and `cards` are `null` and `error` explains why.

### `POST /api/boards/delete`

Body: `{"name", "board", "baseRev"}`. `board` must be the exact registered board file, as listed by `/api/boards`. `baseRev` is required: the board revision you reviewed, or `null` if the board is expected to be gone already. The board's whole folder, with its store and attachments, moves to `~/.workboard/deleted/<dir>-<UTC timestamp>/`, and the name is unregistered. The project folder is never touched. Response: `{"ok": true, "recoveryPath": "<moved folder, or null if the board folder was already gone>", "board": "<path>"}`. Errors: 400 for invalid fields, 404 for an unregistered name, 409 `{"ok": false, "conflict": true, "error"}` for a stale revision or a changed registration, and 500 for a lock timeout or I/O failure.

### `POST /api/shutdown`

Requires the header `X-WorkBoard-Token: <token>`, with the token read from `~/.workboard/server.json`. Responds `{"ok": true}`, then stops gracefully. A wrong or missing token is 403. `workboard service remove|restart` and `workboard upgrade` use this route.

### `GET /b/<enc>` and `GET /b/<enc>/`

`/b/<enc>` redirects (301) to `/b/<enc>/`, which serves the board UI for that board.

## Board routes

All paths below are relative to `/b/<enc>/`.

| Method | Path | Purpose |
|---|---|---|
| GET | `board.json[?since=REV]` | The whole board, or what changed after `REV` |
| GET | `rev` | The revision as `text/plain` |
| GET | `events` | Server-Sent Events |
| GET | `api/ready` | Ready cards |
| GET | `api/stats` | Board statistics |
| GET | `api/git` | Local read-only git status of the project |
| GET | `api/card/{ref}/scopes?subtask=ID` | Claimed work elsewhere whose saved write scope overlaps this subtask's |
| GET | `api/inbox?actor=ACTOR` | Delegated work that needs the actor's attention |
| PATCH | `api/card/{ref}` | Edit fields and position |
| PATCH | `api/card/{ref}/lifecycle` | Lifecycle action |
| PATCH | `api/structure` | Create, delete or sort cards, edit columns, update board fields |
| POST | `api/card/{ref}/attachments?name=` | Upload an attachment (raw bytes) |
| GET | `api/card/{ref}/attachments/{attId}` | Download an attachment |
| DELETE | `api/card/{ref}/attachments/{attId}` | Detach an attachment |

Every JSON write response includes `ok`, `rev`, `savedAt`, `savedBy` and the changed `card` (`null` after `delete-card`, and for structure operations that change no card), never the whole board. Other tabs fetch the rest with `GET board.json?since=REV`.

### Reads

| Route | Response |
|---|---|
| `GET board.json` | The whole board (see [Schema](#schema)): its fields and every card in display order. A board from before 0.4 is migrated to its store first. |
| `GET board.json?since=REV` | The board's fields with the current `rev`, `cards` holding only the cards whose `changedRev` is greater than `REV` (in display order), and `order`: every card ID in display order. A card missing from `order` was deleted; reorder the cards you hold by `order`. `REV` is a nonnegative integer of at most 18 digits, else 400. |
| `GET api/ready` | `{"rev", "cards"}`: unowned Task cards whose dependencies are completed, sorted by priority, then `createdAt`, `num` and `id`. |
| `GET api/stats` | `{"rev", "stats": {"total", "open", "ready", "blocked", "inprogress", "completed", "canceled", "rework", "completedLast7Days", "byColumn", "byPriority", "byOwner"}}` |
| `GET api/git` | `{"state": "clean"\|"dirty"\|"not_repo"\|"error"\|"unavailable", "root", "branch", "head", "subject", "ahead", "behind", "staged", "unstaged", "untracked", "conflicted", "files": [{"path", "index", "worktree"}] (at most 200), "truncated", "error", "warning"}`. Never fetches or writes. Git runs with an 8 s budget, so call it on demand rather than on a timer. |
| `GET api/card/{ref}/scopes?subtask=ID` | `{"rev", "conflicts": [{"cardId", "num", "subtaskId", "owner", "state", "claimedAt", "lastActivityAt", "overlaps": [{"scope", "otherScope"}]}]}`: claimed delegated subtasks on any card that isn't Done whose saved scopes overlap this subtask's. `lastActivityAt` is the latest of the claim and that subtask's notes; an old one suggests a worker that is gone. Advisory only. A plain checklist subtask is 409 `state`. |
| `GET api/inbox?actor=ACTOR` | `{"rev", "items"}` for `actor` (default `user`), the same rows as [`workboard inbox`](cli.md#inbox): `review`, `claim` and `changes_requested`. An unknown or repeated query parameter is 400. |

### `PATCH api/card/{ref}`: edit a card

```jsonc
{ "baseRev": 12, "actor": "user",
  "card": { /* code, title, column, priority, tags, origin, notes, writeup, subtasks, lastTouchedSubtask, meta, agentRuns */ },
  "position": {"before": "<card id>|null", "after": "<card id>|null"},
  "document": { /* title, name, tagTaxonomy, activeWork, activeWorkId */ } }
```

- `card` is required. Other keys in it are dropped, and server-owned fields must be unchanged. For example, a changed `log` is 422 `'log' is server-owned; use its dedicated action`; add timeline entries with the `note` lifecycle action.
- `subtasks` replaces the whole checklist, a flat list. Every item needs a string `id`, and `done` must be a boolean (422). Items that a client from WorkBoard 0.3.5 or earlier sends with their own subtasks inside are flattened, each just before the ones it held, and their fold state is dropped. An item that omits `delegation` keeps its stored value. Delegated state changes only through the `subtask` lifecycle action: supplying a `delegation` that differs from the stored one, adding one to a new or plain item, or changing `done`/`doneAt` of a delegated item is refused (409 `state`), and so is dropping or retexting another worker's claimed or completed subtask (409 `owned`). The same rules apply to `create-card`.
- Field edits never check who owns the card. Changing `column` runs the lifecycle `move` action, including its owner, WIP, dependency and state checks.
- On a Done card, changing `writeup` to blank or whitespace-only text is 422 `invalid`, as `complete` refuses it.
- `position` reorders cards within the display order: `before` wins over `after`. It never changes the column.
- Response adds `card`, `positioned` and `"event": "card-updated"`.

### `PATCH api/card/{ref}/lifecycle`

Body: `{"baseRev", "actor"?, "action", "details": {}}`. If the card has another owner, every action except `takeover`, `submit`, `complete`, `cancel`, `note` and `subtask` is 409 `owned`; `submit`, `complete` and `cancel` close that holder's card and name them as `previousOwner` in the history entry. The `subtask` action has its own rules (below). Required text is trimmed and must not be blank.

| `action` | `details` | Allowed from | Effect |
|---|---|---|---|
| `start` | `note`? | not Blocked (use `resume`), not Done | In Progress, claimed by the actor. Dependencies and the WIP limit are enforced. A no-op if you already own it. |
| `submit` | `summary`, `verification` | Backlog, Task or In Progress | Completes with write-up `summary` + `Verification: verification`, as `complete` does. |
| `complete` | `writeup` | Backlog, Task or In Progress | Done, outcome `completed`, owner cleared. Outside In Progress, the card's dependencies must be completed (409 `deps`). Every delegated subtask must be completed (409 `state`); completed results not yet accepted are accepted with it. |
| `block` | `reason`, `until` | not Done | Blocked, owner cleared. |
| `resume` | `note`, `to`? (`task`\|`inprogress`, default `inprogress`) | Blocked | Leaves Blocked. `inprogress` runs the `start` checks. |
| `takeover` | `reason` | In Progress | Owner becomes the actor. |
| `cancel` | `reason` | not Done | Done, outcome `canceled`. |
| `rework` | `reason` | any | Task. Clears the write-up and archives a completed cycle. |
| `reopen` | `reason`, `to`? (`task`\|`inprogress`), `as`? (`task`\|`bug`\|`improve`) | any | Like `rework`, moving to `to` (default `task`). |
| `bug` | `reason` | any | In Progress with the `bug` tag and a `fix bug:` subtask. |
| `improve` | `text` | any | In Progress with an improvement subtask. |
| `move` | `to`, `writeup`?, `note`? | not Blocked, not Done | To Done runs `complete`, to In Progress runs `start`, and moving to Blocked is 409 `state` (use `block`). |
| `dependencies` | `ids` (the full list; `[]` clears) | any | Rejects self-dependencies, unknown ids and cycles. |
| `note` | `summary`, `body`? | any | Appends an entry to the card's `log` (see [Notes timeline](#notes-timeline)). `summary` is trimmed; a summary over 160 characters or with a line break keeps its first line (cut at a word break with `…` if still too long) and moves whole to the top of `body`. `body` is markdown of at most 32,000 characters, counting a moved summary (`null` or omitted means `""`). Otherwise 422. The response adds `item` (the new entry) and `summaryMoved`. |
| `subtask` | `op`, `ids`, and the fields `op` needs | see below | Runs the subtask lifecycle. The response adds `items` (the selected subtasks) and, for one subtask, `item`. |

An unknown action is 422. The response adds `card`, `action` and `"event": "card-updated"`.

For `action: "subtask"`, `details` holds `op`, `ids` and the fields that operation needs. `ids` is a list (a single string is accepted). Each `op` follows the rules and error codes of the [CLI command](cli.md#delegated-subtasks) of the same name:

| `op` | Fields | Effect |
|---|---|---|
| `add` | `texts` (one or more), `delegated`? (boolean) | Add subtasks to the end of the checklist; `delegated: true` makes them available delegated work. |
| `done`, `undone`, `rm` | `ids` | Plain checklist subtasks: any number of ids, any actor; `result` is refused (422). Removing a delegated subtask needs the card's owner (409 `owned`). |
| `delegate` | one id | Make an open checklist subtask available. |
| `configure` | one id, `scope` (paths relative to the project root) | Set the saved write scope; `[]` clears it. On claimed work it keeps the claim but can only grow: a scope that drops a saved path, or `[]`, is 409 `state`. |
| `claim` | one id | Claim available work. |
| `done` | one id, `result` | The worker completes claimed work. The card's owner can also complete available work or work it holds; that result is accepted at once. |
| `undone` | one id | The worker reopens completed work that was not accepted. |
| `takeover` | one id, `reason` | Take claimed work from its worker. |
| `accept` | one or more ids | The card's owner accepts completed results, all or none. |
| `request-changes` | one id, `reason` | The card's owner sends a completed result back to its worker. |

A delegated subtask's `delegation` is:

```json
{"state": "claimed", "owner": "worker-a", "claimedAt": "2026-09-24T08:15:02Z", "result": "",
 "scope": ["src/migration.py"],
 "review": {"state": "pending", "by": null, "at": null, "reason": ""}}
```

`state` is `available`, `claimed` or `completed`, and `review.state` is `pending`, `accepted` or `changes_requested`. Plain checklist subtasks have no `delegation`. A card can't be completed while a delegated subtask is unfinished.

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
  "event": "card-updated", "action": "note",
  "item": { /* the new log entry */ }, "summaryMoved": false }
```

A stale `baseRev` is 409 like any other write.

### `PATCH api/structure`

Body: `{"baseRev", "actor"?, "operation": {"type", …}}`. The response adds `operation`, `card`, `cardCounts` and `totalCards`.

| `type` | Fields | Behavior |
|---|---|---|
| `create-card` | `card` | `card.id` is required: 32 hex characters or a UUID, optionally preceded by a lowercase slug and `-` (at most 120 characters). It must not already be used as a card id or dependency (409). `column` is `backlog`, `task` (default) or `inprogress`, and `title` is required. The server assigns `num` and timestamps and appends the card. Creating in In Progress runs the `start` checks. |
| `delete-card` | `cardId` | A card owned by someone else is 409 (`card is owned by X; take over before deleting`), and so is one with another worker's claimed or completed delegated subtask (`owned`). Dependencies that point to it are left as they are. |
| `sort-cards` | `mode: "created-desc"` | Sorts by column, then newest first. |
| `update-columns` | `columns` | Exactly the five core column ids, in display order. Each entry is `{"id", "name"?, "kind"?, "wipLimit"?, "stackUnder"?}`. `wipLimit`: an integer sets it, `null` removes it, and omitting it keeps it. An omitted `stackUnder` unstacks the column. |
| `update-document` | `changes` | `title`, `name`, `tagTaxonomy`, `activeWork`, `activeWorkId` |

### Attachments

- **Upload:** `POST api/card/{ref}/attachments?name=<encodeURIComponent(name)>`. The body is the raw bytes (up to 10 MiB), and `X-Board-Base-Rev` is required as a header. `Content-Type` must be a bare `type/subtype`; if absent, it is guessed from the name. The response adds `card` and `attachment: {"id", "name", "size", "mime", "sha256", "createdAt", "by"}`.
- **Download:** `GET api/card/{ref}/attachments/{attId}`, where `attId` is 32 hex characters. It is served as `application/octet-stream` with `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`. A size or hash mismatch is 409, missing bytes are 404, and a request marked `Sec-Fetch-Site: cross-site` is 403.
- **Detach:** `DELETE api/card/{ref}/attachments/{attId}` with body `{"baseRev", "actor"?}`. Allowed for the attachment's author, the card's `activeOwner`, or anyone when it has no `by`; otherwise 409 `owned`. Only metadata is removed; the bytes stay on disk.

## Server-Sent Events

`GET /b/<enc>/events` streams `text/event-stream`. The stream opens with the comment `: connected` and sends `: keepalive` after 15 s without events. It works even when the board is missing. The server checks each watched board's revision every 0.5 s, one small read that never loads the board.

| Event | Data | When |
|---|---|---|
| `rev-bumped` | `{"rev": N}` | The board's revision changed |
| `board-missing` | `{"board": "<path>"}` | The board vanished or can't be read |
| `resync-required` | `{}` | Right after either of the above |

A client fetches `board.json` on connect, then `board.json?since=REV` with its own `rev` on every `resync-required`, and merges the changed cards. A `rev-bumped` event with a revision at or below its own is the echo of its own write. Events for one board never reach subscribers of another board. Each client queue holds 256 events; overflow is dropped.

## Schema

`GET board.json` returns `schemaVersion` 4, `name`, `title`?, `rev`, `nextNum`, `savedAt`, `savedBy`, `columns` and `cards` (array order is display order), plus optional `tagTaxonomy`, `activeWork` and `activeWorkId`. Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`. The board's data lives in `board.db`; the `board.json` file on disk is a stub that points at it (see [Architecture](architecture.md#the-store)).

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
| `tags` | String array |
| `subtasks` | A flat list of `{"id", "text", "done", "createdAt", "doneAt", "by"?, "doneBy"?, "delegation"?, "changedRev"?}`. Boards from WorkBoard 0.3.5 or earlier whose subtasks held their own subtasks load as one list, each just before the ones it held. `delegation` is described [above](#patch-apicardreflifecycle). `changedRev`, on delegated subtasks only, is the board revision that last changed the subtask or a note on it; the server sets it on save and ignores a value a client sends |
| `dependsOn` | Card ids, set through the `dependencies` action |
| `activeOwner`, `claimedAt` | Set only while In Progress |
| `outcome` | `completed` or `canceled` in Done, otherwise `null` |
| `blockedReason`, `unblockWhen`, `blockedAt`, `cancelReason`, `reworkReason`, `reopenReason`, `doneAt` | Lifecycle state |
| `attachments` | `[{"id", "name", "size", "mime", "sha256", "createdAt", "by"}]` |
| `history` | Every `{"at", "ev", "from"?, "to"?, "by"?, "note"?}` entry, oldest first |
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
| `subtaskId` | Optional: the subtask a note is about, on a CLI `note --subtask` entry or a generated note |
| `kind` | Optional: `"generated"` for notes the subtask lifecycle writes (results, takeovers, reviews) |
| `editedAt` | Optional: when its author last edited the entry (CLI `note edit`), or when a comment from before 0.4 was last edited |
| `editedBy` | Optional: who last edited a comment from before 0.4 |

Entries are added only through the `note` lifecycle action (or the CLI's `note`) and the subtask lifecycle, and edited or removed only by their author through the CLI's `note edit` and `note rm`. A card PATCH that changes `log` is 422. Comments from boards before 0.4 are timeline entries with their original ID, time and author.
