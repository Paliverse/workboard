# CLI reference

```text
workboard [--board NAME|DIR] [--json] [--actor NAME] COMMAND [ARGS]
wb ...                     # short alias, same program
workboard --version        # prints: workboard 0.1.0
workboard COMMAND --help
```

## Conventions

### Global options

`--board`, `--json` and `--actor` are accepted before or after the command, including after nested actions such as `workboard skills install --json`.

| Option | Meaning |
|---|---|
| `--board NAME\|DIR` | A registered board name, or a project or worktree folder to find the board from (see [Board resolution](#board-resolution)). A `board.json` path is not accepted. |
| `--json` | Print one machine-readable JSON line instead of human output. |
| `--actor NAME` | Actor label recorded on writes and used by `--mine` and the digest's `MINE` section. Overrides `WORKBOARD_ACTOR` and `actor` in `config.json` (default `agent`; the browser records `user`). Nonblank, at most 80 characters, no control characters. Labels are attribution, not authentication. |

Options must be spelled in full: abbreviations such as `--act` are rejected.

### Board resolution

Boards live in `~/.workboard/boards/`, each linked to one project folder. Board commands take `--board`, else `WORKBOARD_DEFAULT_BOARD`, else the current directory:

- A value that is a registered board name selects that board. Otherwise, if it is an existing directory, the board is found from that directory as below. Anything else fails with `not_found`: `no board named or linked to 'X'`.
- From a directory, WorkBoard checks the directory and its parents, nearest first. The first one that is a board's linked project wins, so subfolders use their project's board and the nearest linked project wins when projects are nested.
- Inside a linked git worktree, the check stops at the worktree's top folder and continues from the same relative path in the repository's main checkout, then the checkout and its parents. Worktrees therefore share their repository's boards, wherever they are.
- Otherwise the command fails with `not_found` and suggests running `workboard init` in the project or passing `--board NAME`.

`which` shows which board a directory resolves to. A project that moved is found again after `workboard link NAME` in its new location.

### Settings

Defaults come from environment variables and the optional `~/.workboard/config.json`, for example `{"port": 7891, "actor": "agent"}`. A flag wins over its environment variable, which wins over `config.json`:

| Setting | Order |
|---|---|
| Actor | `--actor`, `WORKBOARD_ACTOR`, `actor`, then `agent` |
| Port (`serve`, `open`, the URLs from `boards`) | `serve --port`, `WORKBOARD_PORT`, `port`, then `7891` |
| Board | `--board`, `WORKBOARD_DEFAULT_BOARD`, then the current directory |

`WORKBOARD_HOME` moves the whole store (default `~/.workboard`). An unknown key or invalid value in `config.json` fails with `invalid`. See the [README](../README.md#configuration).

### References

`REF` identifies a card: its number (`12` or `#12`), its exact ID, a unique case-insensitive card code, or a unique ID prefix. `ID` identifies a subtask, comment or attachment; take it from `context` or from a mutation's JSON `item`/`items`.

### Output

- Mutations print one concise line. With `--json` they print one line containing `ok`, `action`, `num`, `id`, `column`, `rev` and `actor`, plus `item` for a single created or changed subtask, comment, attachment or note entry, and `items` for subtask operations and `ack`.
- Reads print human summaries. With `--json` they print one object that includes the board `rev`.
- Failures exit with status 1. Human form: `error [code]: message`. JSON form: `{"ok": false, "status": HTTP_STATUS, "code": CODE, "error": MESSAGE, "rev": REV}`, where `rev` is the current board revision or `null` when the board cannot be read. Argument usage errors come from the parser and may be plain text.

| `code` | Meaning |
|---|---|
| `stale` | The reviewed state changed (see [Revision guards](#revision-guards)). |
| `owned` | Another actor owns the card, holds the delegated subtask, or wrote the comment or attachment; take it over explicitly or leave it. |
| `deps` | Dependencies are missing, unfinished or canceled, or a delegated subtask's prerequisites are not completed and accepted. |
| `wip` | The In Progress WIP limit is reached. |
| `state` | The lifecycle transition is not allowed, for example `fly REF done`, or a board name or project is already taken. |
| `invalid` | Invalid input (field names, text, revision or blank stdin), or an invalid `config.json` or `boards.json`. |
| `not_found` | Unknown card, subtask, comment, attachment or board (status 404). |
| `lock` | The board lock could not be acquired in time. |
| `scope` | A write path crosses a symbolic link, junction or other reparse point, or an export targets WorkBoard's own storage. |
| `io` | An operating-system or network failure. |

### Side effects

Board commands never start a server and never open a browser. Only `serve`, `open`, `setup`, `service` and `upgrade` may start the server, and only `open` and `serve --open` open a browser.

### Revision guards

`--expected-rev REV` makes a mutation conditional on the state you reviewed. `REV` is the `rev` from your last `context`/read, or from your own last successful mutation.

- **Card commands** (`start`, `done`, `fly`, `block`, `resume`, `update`, `note`, `workpad`, `subtask`, `depends`, `comment`, `attachment add|remove`, `bug`, `improve`, `reopen`, `takeover`, `cancel`, `rework`): the guard is card-scoped. The command fails with `stale` (409) only when the target card's `changedRev` is greater than `REV`. Writes to other cards don't invalidate it.
- **Board maintenance** (`wip`, `recover --apply`, `sweep --apply`, `columns-core --apply`) and `export`: the guard requires the exact current board revision.
- `REV` must be a nonnegative integer. A value greater than the current board revision fails with `invalid`.
- `add`, reads, `ack`, `handoff`, `import`, `init`, `link`, `serve` and the installation commands reject `--expected-rev`.
- Workers on a delegated subtask don't need the guard: `claim`, `done`, `release`, `block`, `resume` and `note --subtask` check ownership and state under the board lock, while a card-scoped guard fails on every write a sibling worker makes to the same card.

A card-scoped stale error looks like this:

```json
{"ok": false, "status": 409, "code": "stale", "error": "card #12 changed at rev 41 after reviewed rev 37", "rev": 43,
 "card": {"num": 12, "id": "...", "changedRev": 41, "last": {"at": "...", "ev": "comment-add", "by": "alice"}}}
```

Re-read `context`, reconsider, then issue a new command. Never retry blindly with the revision from the error.

### Long text from stdin

`--stdin` and `--*-stdin` read the text from standard input, so long text needs no shell quoting. Empty or whitespace-only input fails with `invalid`.

```sh
git log -1 --format=%B | workboard comment 12 add --stdin --expected-rev 41 --json
```

## Board setup

### `init [NAME] [--dir DIR]`

Create a board with the five columns for the project that contains the current directory, or `DIR`, and link it to that project. The project is the nearest folder at or above it that contains `.git`; inside a linked git worktree it is the repository's main checkout, and without git it is the directory itself. `NAME` defaults to the project folder's name. The board is stored in `~/.workboard/boards/<dir>/`, where `<dir>` comes from the name.

`DIR` must be an existing folder (`not_found` otherwise). An existing board name, or a project that already has a board, fails with `state`; use `link` to move a board to another folder. `--board` is not accepted. Prints `board created: <name> for <project> — view it with: workboard open`. JSON: `{"ok", "name", "board", "project", "rev", "actor"}`.

### `link NAME [--dir DIR]`

Link the board `NAME` to the project that contains the current directory, or `DIR`, found as for `init`. Run it after moving or renaming a project folder. A project has at most one board, so a project already linked to another board fails with `state`. `--board` is not accepted. Prints `linked <name> → <project>`. JSON: `{"ok", "name", "board", "project"}`.

### `boards`

List registered boards, one per line: name, URL, project folder and `✓`, or `✗ board missing` and/or `✗ project missing`. JSON: `{"ok": true, "boards": [{"name", "board", "project", "url", "exists", "projectExists"}]}`.

### `which`

Print the board the current directory (or `--board`) resolves to: `<name> — <board.json> (project <project>) · rev N · M cards`. JSON: `{"ok", "board", "name", "project", "schemaVersion", "rev", "cards"}`.

## Reading

| Command | Output |
|---|---|
| `digest` | A board summary of about 15 lines. It shows `MINE @actor` (cards you own, plus Blocked cards you blocked), `CONTRIBUTIONS @actor` for subtasks you own under another holder's card, column counts, In Progress cards with `@owner`, subtask progress and attention markers, recent shipped, Blocked and canceled cards, `READY: N — #a #b …` (up to five refs), the rework count and the number of old Done cards eligible for `sweep`. JSON adds `stats`, `columns`, `attention` (with `owner`), `contributions`, `ready` (up to five card numbers) and `sweepCandidates`. `query --mine` still means parent-card holder only. |
| `next [--limit N]` | Ready cards (unowned Task cards whose dependencies are all completed), ranked by priority (critical, mid, low, unset), then age. Default limit 5. JSON: `{"ok", "rev", "cards": [{"num", "id", "title", "priority", "tags", "createdAt", "dependsOn"}]}`. Read-only; it does not claim anything. |
| `context REF [--full] [--subtask ID\|--mine\|--assigned-to ACTOR]` | A consistent snapshot of one card for agents: the card with its comments, attachment manifest and notes, its `dependencies` (each with `satisfied`), `missingDependencies`, `dependents` and `ready`. The default keeps 10 done subtasks, 25 history entries, the newest 10 log entries and the newest 10 comments (oldest first, IDs unchanged) and counts the rest in `omitted` (`doneSubtasks`, `history`, plus `log` and `comments` when any were dropped); `--full` keeps everything. `--subtask`, `--mine` and `--assigned-to` focus the subtasks (see [Focused context](#focused-context)). Read-only; the output is JSON even without `--json`. |
| `inbox [REF]` | What needs your attention on delegated work; see [Inbox](#inbox). Read-only. |
| `show REF [--full]` | One card. JSON: `{"ok", "rev", "card"}`. Without `--full`, `notes` longer than 300 characters and `writeup` longer than 400 are cut with a `… (+N ch, --full)` marker, `history` keeps the last 10 entries, `comments` keeps the newest 5, and `log` keeps the newest 5 entries with each body cut to 300 characters with the same marker. `--full` shows everything in full. |
| `list [--column C] [--tag X] [--priority P]` | A human listing of cards. |
| `query [--column C] [--tag X] [--priority P] [--owner NAME \| --mine] [--since-days N] [--limit N] [--fields LIST]` | A JSON projection: `{"ok", "rev", "cards": [...]}`. `--mine` filters by the effective actor. A card's holder is its owner or, for Blocked cards, the actor who blocked it. `--fields` is a comma list of `num,id,title,column,priority,tags,outcome,owner,deps,changedRev,createdAt,updatedAt,doneAt,origin` (default `num,title,column`). Unknown names fail with `invalid`. |
| `search TERMS...` | Cards matching every term (case-insensitive substring) in any text, including comments and every `log` entry's summary and body. |

`P` is `critical`, `mid` or `low`.

## Card lifecycle

The five columns are `backlog`, `task`, `inprogress`, `done` and `blocked`.

| Command | Effect |
|---|---|
| `add --title T [--column backlog\|task] [--priority P] [--tag X]... [--on REF]... [--origin TEXT \| --origin-stdin]` | Create a card, in Task by default. `--on` (repeatable) sets dependencies in the same transaction. |
| `add --from REF [--title T]` | Create a card from another card: its title (unless `--title`), pinned notes, tags (plus any `--tag`), priority (unless `--priority`) and subtask tree. Subtasks get fresh IDs `s-1`…`s-N` in tree order and start open; delegated ones become available again, keeping their scope, required review and prerequisites (renumbered). Origin, comments, notes timeline, attachments, history, owner, outcome and card dependencies are not copied. The other `add` options work as usual. Keep reusable card shapes in Backlog and copy them. A missing source is `not_found`; without `--title` or `--from`, `add` is a usage error. |
| `start REF` | Claim the card and move it to In Progress. Dependencies must be completed and the WIP limit respected. Starting a card you already own does nothing. |
| `done REF (--writeup TEXT \| --writeup-stdin)` | Complete an In Progress card with a write-up of the delivered work and the checks you ran. It fails with `state` while a delegated subtask is unfinished or a required review is not accepted. |
| `fly REF backlog\|task\|inprogress [--note TEXT]` | Move between Backlog, Task and In Progress. Use `done` to reach Done and `block`/`resume` for Blocked. |
| `block REF (--reason TEXT \| --reason-stdin) --until CONDITION` | Move to Blocked with a reason and an observable exit condition. Releases ownership. |
| `resume REF --note TEXT [--to task\|inprogress]` | Leave Blocked with a resolution note (default target `inprogress`, which claims the card). |
| `takeover REF --reason TEXT` | Take ownership of another actor's In Progress card. |
| `cancel REF --reason TEXT` | Close unfinished work in Done with outcome `canceled`. |
| `rework REF --reason TEXT` | Send the card back to Task for rework. Previous completions are kept as cycles. |
| `reopen REF --reason TEXT [--as task\|bug\|improve]` | Reopen a Done card to Task, recording why. |
| `bug REF --reason TEXT` | Reopen into In Progress with the `bug` tag and a fix subtask. |
| `improve REF TEXT` | Reopen into In Progress with an improvement subtask. |

## Card content

| Command | Effect |
|---|---|
| `update REF [--title T] [--priority P] [--add-tag X]... [--rm-tag X]... [--notes TEXT \| --notes-stdin]` | Edit fields. `--notes` replaces the pinned notes. |
| `note REF [--subtask ID] --summary TEXT [--body MARKDOWN \| --stdin]` | Append an entry to the card's notes timeline (`log`). See [Notes timeline](#notes-timeline). With `--subtask`, the entry reports on your delegated subtask (see [Delegated subtasks](#delegated-subtasks)). |
| `workpad REF` | Add missing `## Acceptance criteria` and `## Verification` sections to the pinned notes. |
| `subtask REF add TEXT [TEXT ...] [--parent ID] [--delegated]` | Add one or more subtasks in one revision. `--parent` nests them. `--delegated` makes them claimable (see [Delegated subtasks](#delegated-subtasks)). |
| `subtask REF done\|undone\|rm ID [ID ...]` | Tick, untick or remove checklist subtasks in one revision. If every requested change is already in place, nothing is saved and `rev` stays the same. `rm` also removes nested subtasks. |
| `depends REF [--on REF]... [--remove REF]... [--clear]` | Add, remove or clear dependencies. Self-dependencies, unknown cards and cycles are rejected. |
| `comment REF add (TEXT \| --stdin)` | Add a comment. |
| `comment REF edit ID (TEXT \| --stdin)` | Edit a comment (records `updatedBy`). |
| `comment REF delete ID` | Delete a comment. |
| `attachment REF list` | List the attachment manifest (also included in `context`). |
| `attachment REF add --file SOURCE [--name NAME] [--mime MIME]` | Attach a file of up to 10 MiB. |
| `attachment REF get ID --out DEST` | Export an attachment to a new path. The size and SHA-256 are verified and existing files are never overwritten. JSON includes `out`, `sha256`, `rev`, the metadata and the card's `num`/`id`. |
| `attachment REF remove ID` | Detach an attachment. The bytes are kept on disk for backups and recovery. |

`update`, `workpad`, `note`, `comment add`, `attachment add` and checklist `subtask` operations work on any card, whoever owns it. Lifecycle commands and `depends` need you to own the card, or the card to have no owner. Editing or deleting a comment and detaching an attachment are limited to its author, the card's owner, or anyone when the entry records no author; anyone else gets `owned`.

### Notes timeline

A card has two kinds of notes. The pinned `notes` string holds durable free-form context, such as acceptance criteria; `update --notes` replaces it and `workpad` seeds it. The `log` timeline holds one entry per step, appended with `note` and never edited.

```sh
workboard --actor codex-auth note 12 --summary "Chose SQLite over JSON files" --body "Concurrent writers need row-level locks." --expected-rev 41 --json
git log -1 --format=%B | workboard --actor codex-auth note 12 --summary "Merged the flush fix" --stdin --expected-rev 42 --json
```

- `--summary` is required and must be one line of 1–160 characters after trimming; otherwise the command fails with `invalid`. State what changed or was decided.
- The body is optional markdown: `--body MARKDOWN`, or `--stdin` to read it from standard input (blank input fails with `invalid`). At most 32,000 characters; trailing whitespace is stripped.
- An entry is `{"id", "at", "by", "summary", "body"}`: `id` is 32 lowercase hex characters, `at` is a `YYYY-MM-DDTHH:MM:SSZ` timestamp (`YYYY-MM-DD` for entries migrated from legacy notes), and `by` is the actor (`null` for migrated entries that had none). A worker's `note --subtask` entry adds `subtaskId`; entries the subtask lifecycle writes add `"kind": "generated"` too.
- Human output: `#N <title> note added: <summary>`, with the title and summary cut to 60 characters. With `--json`, `item` is the new entry.
- `note` is a card command, so `--expected-rev` works as for the other card commands. Adding a note does not require owning the card; `note --subtask` requires holding that subtask.

## Delegated subtasks

The owner of an In Progress card (Main) can hand complementary pieces of it to other agents and keep the card. A delegated subtask carries a `delegation` object (see [Architecture](architecture.md#delegated-subtasks)); plain checklist subtasks have none. Every delegated operation takes exactly one subtask ID.

| Command | Who | Effect |
|---|---|---|
| `subtask REF add TEXT... --delegated` | the card's owner, or anyone while it has none | Add available delegated subtasks. |
| `subtask REF delegate ID` | as above | Make one open checklist subtask available. |
| `subtask REF configure ID [--scope PATH]... [--clear-scope] [--on ID]... [--clear-deps] [--review-required\|--no-review-required]` | owner of the In Progress card | Configure available work: its saved write scope (relative paths, `.` for the whole project), its prerequisites among the card's delegated subtasks (no cycles) and whether its result needs review. Options you omit keep their value. Scopes are advisory: `handoff` and focused context warn when one overlaps claimed or blocked work on any card that isn't Done. |
| `subtask REF claim ID` | a worker | Take available work. Its prerequisites, followed through their own prerequisites, must be completed and, where review is required, accepted (`deps`). Claiming your own claim again changes nothing. |
| `note REF --subtask ID --summary TEXT [--body MARKDOWN \| --stdin]` | the worker | Publish a finding on your claimed or blocked subtask. |
| `subtask REF block ID --reason TEXT --until CONDITION` | the worker | Mark claimed work blocked; you keep it. |
| `subtask REF resume ID` | the worker | Continue blocked work. |
| `subtask REF done ID --result TEXT` | the worker | Complete claimed work with its evidence, which is also appended as a timeline note. Repeating it with the same result changes nothing; a different result needs `undone` first. |
| `subtask REF undone ID` | the worker | Reopen completed work, unless Main accepted it. |
| `subtask REF release ID --reason TEXT` | the worker | Give claimed or blocked work back; it becomes available. |
| `subtask REF takeover ID --reason TEXT` | anyone | Take over claimed or blocked work from its worker. |
| `subtask REF accept ID` | owner of the In Progress card | Accept a completed result. |
| `subtask REF request-changes ID --reason TEXT` | owner of the In Progress card | Send a completed result back to its worker as claimed work. |

- `claim`, `done`, `undone`, `block` and `resume` need the card In Progress with an owner (`state` otherwise). Configuring, accepting and requesting changes by anyone but that owner, and `add --delegated` or `delegate` on a card someone else owns, are `owned`.
- A worker command on a subtask someone else holds is `owned`, and so is `release` of available work. The wrong state for your own subtask is `state`.
- `subtask rm` refuses a subtree with another worker's claimed, blocked or completed subtask (`owned`) and a prerequisite that remaining subtasks still list (`state`).
- `done REF` on the card fails with `state` until every delegated subtask is completed and every required review accepted. Nothing expires: blocking, canceling or pausing the card keeps every claim.
- Worker commands don't need `--expected-rev`; see [Revision guards](#revision-guards).

### Focused context

`context REF --subtask ID` keeps only that subtask's subtree and its ancestors; `--mine` keeps the subtasks you hold and `--assigned-to ACTOR` the ones another actor holds. Attachments, notes, dependencies and the newest 10 comments are kept, as in an unfocused read. The response adds `focus` with `subtaskId` or `assignedTo`, `matchedSubtaskIds`, `ancestorIds`, `scopeWarnings` (overlapping saved scopes) and `prerequisites` (each selected subtask's prerequisite subtasks), and `omitted.unrelatedSubtasks`. Focus grants no extra permission.

### Handoff

`handoff REF --subtask ID --worker ACTOR [--write-scope PATH]... [--peer ACTOR=ADDRESS]...` prepares a worker's brief and changes nothing. It never claims the subtask, starts a worker or sends a message.

- Only the owner of the In Progress card may run it (`owned`, or `state` when the card isn't In Progress with an owner). The worker must be another actor.
- The subtask must be delegated and available or already claimed by that worker (`state`; `owned` when another worker holds it), and its prerequisites must be complete (`deps`).
- The write boundary is `--write-scope` (repeatable), else the subtask's saved scope; with neither, it fails with `invalid`. `--peer` names other workers and how to reach them.

It prints a copy-ready prompt. `--json` returns `{"ok", "board", "rev", "cardId", "num", "title", "parentOwner", "worker", "assignment", "scopeWarnings", "ancestorIds", "writeScope", "peers", "origin", "notes", "boardName", "commands", "placeholders", "prompt"}`. `commands` holds four argument arrays:

```json
{"read":     ["workboard", "--board=demo", "--actor=worker-a", "context", "CARD_ID", "--subtask=s-4", "--json"],
 "claim":    ["workboard", "--board=demo", "--actor=worker-a", "subtask", "CARD_ID", "claim", "--json", "--", "s-4"],
 "publish":  ["workboard", "--board=demo", "--actor=worker-a", "note", "CARD_ID", "--subtask=s-4", "--summary={summary}", "--stdin", "--json"],
 "complete": ["workboard", "--board=demo", "--actor=worker-a", "subtask", "CARD_ID", "done", "--result={result}", "--json", "--", "s-4"]}
```

Replace only `{summary}` and `{result}` (described in `placeholders`) and keep every other element literal. Run each array with its elements as separate arguments; in a shell, quote every element. `publish` reads the note body from standard input.

### Inbox

`inbox [REF]` lists what needs your attention, on every card or only `REF`. It prints `{"ok", "rev", "actor", "items"}` (indented without `--json`). Each item has `kind`, `num`, `cardId` and `subtaskId`:

| `kind` | Shown to | Extra fields | When |
|---|---|---|---|
| `review` | the card's owner | `title`, `text`, `result`, `owner`, `review` | Someone else completed a subtask that requires review, and it is still pending. |
| `blocker` | the card's owner and the worker | `owner`, `blocker` | A delegated subtask is blocked. |
| `changes_requested` | the worker | `review` | Main requested changes on your subtask. |
| `finding` | the card's owner | `title`, `note` | Someone else added a note (not a generated one) that you haven't acknowledged. |

Rows for a card's owner appear only while the card is In Progress. `accept` and `request-changes` clear a review; `resume` or `release` clears a blocker.

`ack REF NOTE_ID [NOTE_ID ...]` acknowledges findings in one write. Only the owner of the In Progress card may run it (`owned`). Every ID must be a finding on that card (`not_found`); one unknown ID refuses the whole call. IDs you already acknowledged cause no write and keep `rev`. Acknowledgements are stored per actor in the card's `inboxAcknowledged` and add no history entry. `--json` `items` holds the acknowledged entries in request order. `ack` takes no `--expected-rev`.

## Portable boards

### `export --out PATH [--no-archives] [--include-backups] [--expected-rev REV]`

Write the board to a new ZIP: `workboard-bundle.json` (`{"format": "workboard-bundle", "version": 1}`), the byte-exact `board.json`, every attachment the included documents reference (`attachments/<id>`), the archived cards (`archive/*.json`; `--no-archives` leaves them and the files only they use out) and, with `--include-backups`, the rolling snapshots (`.backups/board-<rev>.json`).

- The snapshot is taken under the board lock. `--expected-rev` is the exact board revision (`stale` otherwise).
- `PATH` is never replaced: an existing file fails with `invalid`, and one that appears during the export with `io`. A path inside the WorkBoard home, the board's folder or WorkBoard's own package, under a `.workboard` or `.git` folder, or below a folder that holds a `board.json` fails with `scope`.
- Prints `exported <name> rev <rev> → <out> (<n> cards, <n> attachments, <n> archives, <n> backups)`. JSON: `{"ok", "board", "name", "rev", "out", "bytes", "cards", "attachments", "archives", "backups"}`.

### `import FILE --name NAME --dir PROJECT [--apply]`

Create a new board `NAME` from an exported ZIP, linked to the project that contains `PROJECT` (found as for `init`; the folder must exist, else `not_found`). Without `--apply` it only previews: it checks the whole ZIP and that the name and project are free, writes nothing, and fails exactly when `--apply` would. With `--apply` the board is created like `init` creates one: its `name` becomes `NAME`, its `rev` the exported `rev + 1`, and attachments, archives and backups are copied byte for byte. Import never merges into or replaces a board, and writes nothing into the project.

- A taken name or an already linked project fails with `state`, as for `init`. A malformed ZIP fails with `invalid` (`invalid WorkBoard bundle: …`), and an unsupported board `schemaVersion` with `invalid` (409).
- Accepted: stored or deflated members, `\` as a path separator (ZIPs from Windows PowerShell 5.1 `Compress-Archive`), no directory entries, and no attachment bytes that no card references. Limits (413 when exceeded): 4,096 members; 512 MiB for the ZIP and for its uncompressed total; 10 MiB per attachment; 16 MiB per JSON member, fewer than 1,000,000 JSON values and at most 64 levels of nesting.
- Prints `preview: <source> would become board <name> for <project> (counts); rerun with --apply`, or `imported <source> as board <name> for <project> (counts)`. JSON: `{"ok", "applied", "name", "project", "sourceName", "sourceRev", "cards", "attachments", "archives", "backups"}`, plus `board` and `rev` when applied.

## Board maintenance

These are operator tasks. Without `--apply`, `recover`, `sweep` and `columns-core` only preview.

| Command | Effect |
|---|---|
| `wip LIMIT --expected-rev REV` | Set the In Progress WIP limit: `off` or `1`–`20`. |
| `recover [REV] [--apply --expected-rev REV]` | List backup snapshots (newest first) or restore one. The revision and card numbering keep increasing after a restore. |
| `sweep [--days N] [--apply --expected-rev REV]` | Archive Done cards older than N days (default 14) to the board's `archive/` folder. |
| `columns-core [--apply --expected-rev REV]` | Consolidate a legacy board into the five core columns. Removed cards are archived. |

For `--apply` and `wip`, `--expected-rev` is the current board revision you reviewed (see `which --json`), not the snapshot revision.

## Server and browser

### `serve [--port N] [--open] [--service]`

Run the per-user server in the foreground for every registered board. It listens on `127.0.0.1` at `--port`, else `WORKBOARD_PORT`, else `port` in `~/.workboard/config.json`, else `7891`, and prints `serving N boards at http://127.0.0.1:7891/ (Ctrl+C to stop)` (JSON: `{"ok", "url", "pid", "port", "version"}`).

- `--open` opens the current board, if one is found, or the board list.
- `--service` is the background-service mode used by `workboard service`. It writes output to `~/.workboard/logs/server.log` (rotated at 5 MB) and never opens a browser.
- If another WorkBoard server already answers on the port, it prints `already running: <url>` and exits 0. If another program holds the port, it fails with `state` and asks you to pass `--port` or set `WORKBOARD_PORT`.

### `open`

Open the current board in the browser (see [Board resolution](#board-resolution)), or the board list when none is found. With `--board`, a board that can't be found is an error instead. It starts the server in the background if it isn't running. Prints `opened <url>`; JSON `{"ok", "url", "board"}`.

## Installation

### `setup [--no-skills] [--no-service] [--no-codex]`

Run `skills install`, the Codex step and `service install`, printing one summary line per action. The Codex step adds `~/.workboard` to the writable folders in Codex's `config.toml`, so Codex can write boards from its sandbox, and prints `codex: <state>`, plus the reason and the snippet to paste when the state is `manual`. JSON includes a `codex` object with `state` and `config`, plus `reason` and `snippet` when the state is `manual`. `--no-skills`, `--no-codex` and `--no-service` skip a step. The states are described in [install.md](install.md#codex).

### `skills install [--refresh] | remove | status`

- `install` writes the bundled skill to `~/.agents/skills/workboard/SKILL.md` and `~/.claude/skills/workboard/SKILL.md`. `--refresh` only rewrites copies that already exist.
- `remove` deletes skill directories whose `SKILL.md` declares `name: workboard`.
- `status` reports each target as `installed`, `current`, `stale` or `missing`.

### `service install | remove | status | restart`

- `install` registers the server to start at login and leaves it running. Running it again is safe.
- `remove` stops the server and deletes the registration.
- `status` reports whether the registration exists, whether a server answers, whether its version matches the CLI and whether it can read the boards this CLI writes. JSON includes `versionMatch` and `schemaMatch` (`null` when no server answers). When `schemaMatch` is false, the text output says so and suggests `workboard service restart`.
- `restart` stops the server, starts it again and checks it. If the restarted server is another version or can't read the boards this CLI writes, the service starts another installation: it exits 1 with `state`, names that server's version and executable, and asks you to run `workboard service install`, then `workboard service restart`. That server is left running.

Per-OS details are in [install.md](install.md#background-service).

### `version [--check]`

Print `workboard 0.1.2 (<channel>)`, where the channel is `npm`, `script` (the install script), `source` (a git checkout) or `unknown`. JSON adds `python`, `platform`, `executable`, `channel`, `apiVersion`, `schemaVersion`, `supportedSchemaVersions` and `capabilities`. `--check` queries the latest GitHub release and reports current, latest and whether an update is available. A network failure fails with `io`.

### `upgrade [--dry-run]`

Upgrade through the detected channel: `npm` runs `npm install -g @paliverse/workboard@latest`, and `script` runs the install script again. It stops the server, runs the channel's upgrade command, refreshes installed skills and restarts a previously running or installed service through the new version's `service restart`, which checks the restarted server. If the channel command fails, the stopped server is started again; if a later step fails while the server is down, the error says so and names `workboard service restart`. JSON `to` is the new version. `--dry-run` only prints the plan. `source` and `unknown` installations exit 1 with instructions.

### `doctor [--all]`

Check the installation and the current board (or every registered board with `--all`); when no board is found, only the installation and the registry are checked. It covers the executable, version, channel, whether `workboard` on `PATH` is this installation, skills and service status, the running server's version and schema (`server-schema-mismatch` is a blocker when it can't read the boards this CLI writes), whether Codex can write to `~/.workboard`, leftover files from the pre-release per-board viewer or a pre-release `board/` folder in a linked project, board schema (`legacy-schema` for a live board not yet migrated to schema 4), the notes timeline (`log-invalid`), delegated subtasks (`delegation-invalid`), attachments, backups, the registry, linked project folders that no longer exist and folders under `~/.workboard/boards/` that no board uses. Prints a summary; `--json` prints `{"ok", "blockers", "warnings", ...}`. Exits 1 when there are blockers.
