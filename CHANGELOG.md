# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Authors couldn't edit or remove their own timeline notes without hand-editing `board.json`. `note REF edit NOTE_ID` and `note REF rm NOTE_ID` now do it (`note.edit` and `note.rm` in `call`, with `id`). Only the author may change a note; others get `owned`. Each change adds a `note-edited` or `note-removed` history event, and an edited note carries `editedAt` and shows "edited" in the browser. Editing a note clears earlier acknowledgements of it, so an edited finding shows in the card owner's inbox again, even if it was posted before its subtask was accepted. Notes the subtask workflow writes itself (results, acceptances, takeovers) can't be changed. `edit` or `rm` without a note ID is plain note text.
- On Windows, npm installed only `workboard.cmd` and `wb.cmd`, so every argument went through `cmd.exe`: line breaks cut the command short, quotes, `%`, `^` and `&` were mangled, Rust-based shells such as omp's refused multi-line arguments with "batch file arguments are invalid", and Python's `subprocess` couldn't find `workboard` at all. Global npm installs now also get `workboard.exe` and `wb.exe`, a small launcher that runs the real binary with the arguments untouched; killing the launcher (a caller's timeout) also ends the binary, while servers and upgrades the binary starts keep running. npm copies it after install, and each run through the `.cmd` shim restores it if it is missing or outdated. `npm uninstall -g` leaves the two launchers behind; run on their own they say what is missing and how to fix it, and the uninstall instructions list them.

### Changed

- A note summary over 160 characters or with a line break was refused, so agents retried or cut their own text. The summary now keeps its first line, cut at a word break with "…" if still too long, and the full text moves to the top of the body. This holds for the CLI, `call` and the browser API. Only a body that ends up over the body limit is refused, and results report `"summaryMoved": true`.
- `note 12 "text"` failed because `--summary` was required. The text can now be given positionally (`"text"` in `call`); giving both is refused with both named.
- Commands that write a card recorded the writer as `agent` when no label was set. They now refuse with `invalid` and ask for `--actor`, `"actor"` in the call request, or `WORKBOARD_ACTOR`. Reads still work without a label, and the browser still records `user`.
- A `call` request with a key its op doesn't take said only `OP doesn't take "x"`, and a guessed key that replaced a required one (`{"op":"search","query":…}`) was reported as `search needs "terms"`. The unknown key is now reported first, with the keys the op takes and, for common guesses, the key to use: `search doesn't take "query" (use "terms"); search takes: terms, limit, fields, board, actor`.
- An unknown `call` op said only `op X is not available in call`. It now lists every call op and names the right one for common guesses (list/get → query or context, move → fly, claim → start or subtask.claim, create/new → add, help → `workboard COMMAND --help` outside call). On the command line, argparse's "invalid choice" for those guesses gains a `did you mean workboard fly?` line.
- Input to `call` that wasn't JSON failed with a bare decoder message such as `Expecting value: line 1 column 1 (char 0)`. The error now quotes the first 60 characters that arrived; when the double quotes are missing (a shell stripped them, or bash brace-expanded the object) it says so and shows the heredoc form. Empty stdin shows the heredoc form too.

## [0.3.9] - 2026-10-11

### Fixed

- The browser's "Sort cards by newest" sorted each column once, so every card added later, including those agents add from the CLI, landed at the bottom of its column. The board menu now has a "Newest cards first" setting instead, which persists for each board in that browser: while it is on, every column except Done lists cards newest first, and dragging a card within a column is refused with a hint to turn it off (moving a card to another column still works). Turning it off restores the manual order. The saved card order is left untouched, and the `sort-cards` structure operation in the HTTP API is unchanged.

## [0.3.8] - 2026-10-11

### Changed

- The card's owner finishes a delegated step in one request: `subtask.done` with a `result` on available work, or on work it holds after `subtask.claim` or `subtask.takeover`, completes the step and accepts it at once, and the timeline records both. Before, available work was refused until claimed, and the owner then had to `subtask.accept` its own result. Work another worker holds still needs `subtask.takeover` with a reason first, and other workers' results still need `subtask.accept`. In the browser, the card owner's checkbox on an available delegated step is enabled and finishes it the same way. The card's `done` refusal points to this, and the skill tells Main to close a step it did, a superseded step or a gone worker's step this way.
- `wait --subtask` returns at once only when that subtask changed after `--after`, not whenever anything on its card did. Each delegated subtask now has `changedRev`, the board revision that last changed it or a note on it, set by the server like the card's. Subtasks already on disk get a stamp the next time their card is saved with a change; until then, and for plain checklist subtasks, `wait` falls back to the card's `changedRev`. The skill drops its line about waiting again after a `changed` with nothing new.
- A card keeps its whole `history`. Before, only the last 40 entries were kept, so a long-running card lost its oldest events; rows already dropped are not restored. `show` without `--full` still prints the last 10, and `context` still leaves history out unless `--full`.

## [0.3.7] - 2026-10-11

### Fixed

- Blocking a card hid its `review`, `blocker` and `claim` rows from `inbox`, because `block` clears the owner, so Main lost sight of finished and running work. The actor who blocked the card now still sees them, by the same holder rule as `digest`'s MINE and `query --mine`. `finding` rows wait until the card is In Progress again, since `ack` needs it. The browser shows a Blocked card's results without Accept and Request changes buttons until it is resumed, since review needs In Progress.
- The `handoff` prompt printed the worker's requests as one JSON object on one line, so pasting it into `workboard call` was `invalid` ("each call request needs a string op"). It now prints each request on its own line: read, claim, resume (blocked work only), publish, complete. `--json` `requests` is unchanged.
- Refusals name the op that clears them. The card's `done` refusal follows each subtask that keeps the card open with its next step: `subtask.takeover` with a reason if the actor holding it is gone, or for available work, claim and finish it or `subtask.rm` it. `subtask accept` on work that isn't completed gives the same hint. In `call`, an unknown `subtask.*` or `comment.*` action said "argument op: invalid choice"; it now names the op and lists the real ones: `unknown op "subtask.edit"; subtask ops: add, done, …`.
- An HTTP card edit (`PATCH api/card/{ref}`) could blank the write-up of a Done card, which `done` refuses. Changing a Done card's `writeup` to blank or whitespace-only text is now `invalid` (422); other columns are unchanged.
- The skill lists `note` as `resume`'s reason and `subtask.rm` among the ops, and tells Main to drop a superseded step with `subtask.rm` (after `subtask.takeover` if another worker still claims it). It tells a worker whose `wait` returned `changed` with nothing new on its subtask to wait again with the returned `rev`, and says a Blocked card you blocked keeps its inbox rows. Its card `takeover` line could be read as allowing a takeover only when the user asks; it now says "only if the user asks or the holder is your own gone worker".

## [0.3.6] - 2026-10-11

### Added

- `inbox` lists a `claim` row for each delegated subtask another actor holds on the owner's In Progress card, with `owner`, `state` (`claimed` or `blocked`), `claimedAt` and `lastActivityAt` (the latest of the claim and that subtask's notes), so after a restart Main sees what is still running and which worker went quiet. A blocked subtask lists its `blocker` row first. The browser's Inbox shows them as "Assignment in progress" or "Assignment blocked", and its count leaves them out.
- `wait --subtask` adds `notes` to its result: the entries posted on that subtask while it waited, so a worker reads Main's answer without another `context`. It is `[]` after a timeout or an immediate return.

### Changed

- `done` works from Backlog and Task too, so work found already done needs no `start` first. Outside In Progress the card's dependencies must be completed (`deps`); a Blocked card is `state` (use `resume`), and so is a Done card (use `rework` or `reopen`). The browser's `complete` and `submit` follow the same rule.
- `add --column inprogress` (`"column":"inprogress"` in `call`) creates and starts a card in one request, with `start`'s dependency and WIP checks, and makes you its owner; a refusal creates nothing. Before, `add` took only Backlog or Task. The browser's card creation uses the same code: titles are trimmed, and a card created In Progress records `start` in its history.
- Every delegated result needs `subtask accept` before the card's `done`, and before subtasks that list it as a prerequisite can be claimed or handed off. A result whose review wasn't required used to count once completed; existing ones now show REVIEW PENDING in `digest` and the browser until accepted. `review.required` is always `true`, and `doctor` doesn't report the stored `false` it replaces.
- Subtasks are one flat list. Boards with nested subtasks load flat, each subtask just before the ones it held, keep their delegations, and are saved flat on the next write; `doctor` doesn't report the change. `context --subtask` returns only that subtask (`focus.ancestorIds` is gone), `wait --subtask` watches only that subtask, and `subtask rm` removes only the listed IDs. The browser drops its fold and "Add sub-item" controls.
- `context` leaves out the card's history unless `--full` (`"full":true`); `omitted.history` counts all of it. Before, it kept the last 25 entries.
- `handoff` prints a prompt of about six lines: the card, the subtask, the scope (or "read-only: edit no files"), a takeover or blocked line when one applies, the requests on one line for `workboard call`, and a pointer to the skill's Worker section. Card text is JSON-quoted as data. `--json` returns only `ok`, `rev`, `num`, `worker`, `previousOwner`, `writeScope`, `scopeWarnings`, `requests` and `prompt`; `commands`, `placeholders`, `call`, `assignment`, `title`, `cardId`, `board`, `boardName`, `parentOwner`, `origin`, `notes`, `peers` and `ancestorIds` are gone. The write scope is always the subtask's saved one.
- `digest --json` has `sweepCandidateCount` (a number) instead of the `sweepCandidates` list of card IDs, and its `mine` rows are `{num, title, column}` without `id`.
- `inbox` leaves out findings on a subtask that come before Main's acceptance of it in the timeline; notes posted later still show.
- `handoff` requests carry the worker's `board` and `actor`, so the prompt names plain `workboard call` and no board or worker name passes through a shell (names such as `R&D` used to split the command on Windows).
- The agent skill is shorter (about 1,700 words instead of 2,500). It asks for `expectedRev` only on cards you don't hold, and covers `add` into In Progress, `done` from Task, accepting every result, the slim handoff, inbox `claim` rows and `wait` notes. Rarely used actions left the skill but not the CLI, and its Commands block became a list of ops. [docs/agents.md](docs/agents.md) now points to the skill instead of repeating it.

### Removed

- `subtask add --parent`: subtasks no longer nest. `"parent"` in a `call` request is `invalid` (`subtask.add doesn't take "parent"`).
- `subtask configure --review-required` and `--no-review-required`, since every result needs acceptance. `"reviewRequired": true` in `call` is `invalid`, and `reviewRequired` in the HTTP `configure` is 422.
- `handoff --write-scope`, `--peer` and `--fields`. Set the scope with `subtask configure --scope` before the handoff.

### Fixed

- `workboard upgrade` through npm could install the previous version right after a release, because npm answered from its cached package data. It now runs `npm install -g --prefer-online @paliverse/workboard@latest`.

## [0.3.5] - 2026-10-11

### Fixed

- The card's owner was refused (`owned`, "only the current claimant or the parent owner can publish") when posting `note --subtask` on a plain checklist step of its own card. The owner may now note any subtask of its card, so a step's evidence stays with that step.
- A worker can post `note --subtask` on its delegated subtask after `subtask done`, including after Main accepted it, for example a recheck verdict. Before, the worker had to `undone`, note and `done` again, which rewrote the subtask's history and briefly showed approved work as open.
- `note --subtask` checks the summary and body before permission, so a too-long summary is `invalid` in the first reply instead of after an `owned` refusal and a retry.
- The `call` hint for `id` on a subtask action no longer suggests a `subtasks` list that delegated actions refuse.
- In `call`, a list for an option that takes one value kept only its last item, and an empty list sent nothing. Both are now `invalid`: `notes takes one value, not a list`, `scope is an empty list; omit it or use clearScope to clear`.
- `call` errors name request keys instead of the parser's names: a missing field is `note needs "summary"` or `subtask.claim needs "subtask" or "subtasks"` instead of "the following arguments are required: what", an unknown key is `context doesn't take "fields"`, and usage errors show `"camelKey"` without the `--*-stdin` alternatives that `call` refuses.
- In `call`, a `card`, `column`, attachment `id` or `ack` note ID starting with `-` (such as `-h`) reached the parser, and a request that printed help ended the whole stream. Both are now `invalid`, and later requests still run.
- `context`, `note`, `handoff` and `wait` in `call` say they take one subtask ID in `subtask` when given `id` or `subtasks`.
- `wait --subtask` missed notes posted on that subtask, so a worker waiting for Main's answer timed out. A note on the watched subtask now wakes it.
- `query --column` and `list --column` compared the raw text, so `in-progress` or `In Progress` matched nothing and a misspelled column returned an empty list. They now take the same column names as `fly`, plus any column the board itself has, and an unknown column is `invalid`.
- A worker holding a delegated subtask couldn't post `note --subtask` once the card left In Progress (`state`). It can now post on any column, for example after the card is Done. A refusal names the subtask's holder and suggests a card note instead.
- A `result` on plain checklist steps (`subtask done`) was silently dropped. It is now `invalid`, and the error says to post the evidence with `note --subtask`.
- Lifecycle and subtask commands check their input (reasons, `until`, write-ups, results, target columns, dependency IDs) before ownership, so bad input from someone who may not act is `invalid` in the first reply instead of `owned`.
- Refusals name what blocks you: the card's `done` lists each subtask that keeps it open and what it waits for; `start`, `depends`, `subtask claim`, `subtask done` and `handoff` name each unmet prerequisite; a refused ID in a multi-ID `accept` and the delegated IDs in another multi-ID action are named.
- `subtask done` with a new result on accepted work said to reopen it with `undone`, which was refused, and `undone` named "Main" as the acceptor. Both now name who accepted it and say to post later evidence with `note --subtask`.
- A worker command on a card that isn't In Progress now says to post the result with `note --subtask` and wait for the owner to start or resume the card. `add --delegated`, `delegate` and `rm` on another owner's card say to ask that owner instead of suggesting `takeover`, and a claimant refusal points to `subtask takeover` for a worker that is gone.

### Added

- `subtask accept` takes several subtask IDs (`"subtasks":[...]` in `call`) and accepts them in one revision; if any can't be accepted, none are.
- `digest --json` adds `mine`: the cards you hold, as listed under MINE in the text output.
- `add --notes TEXT` or `--notes-stdin` (`notes` in `call`) sets the pinned notes when the card is created; with `--from` it replaces the copied notes.
- `subtask REF done ID --result-stdin` reads the result from stdin.
- `search --limit N` keeps the N newest hits, and `search --fields LIST` picks the hit fields, as in `query`.
- `handoff --fields LIST` keeps only the listed keys in `--json`, plus `ok`, `rev` and `num`; `"fields":"prompt"` returns just the brief.
- `handoff` on a subtask another worker holds (claimed or blocked) returns a takeover brief for the replacement worker: `previousOwner` names the holder, and the claim step is `subtask takeover` with a `{reason}` placeholder. Blocked work also gets a `resume` step. Before, it was refused.
- `handoff` without a write scope returns a read-only brief that tells the worker to edit no files. Before, it was refused, and Main had to brief read-only workers by hand.
- `context` has the card's `num` at the top, next to `rev`.
- `scopeWarnings` rows add `claimedAt` and `lastActivityAt` (the latest note on that subtask, or its claim), so Main can tell a stale claim from active work.
- `inbox` also lists completed results that need no review, with `review.required` false; `subtask accept` clears them. They still don't hold up `done`.

### Changed

- `search --json` hits are `{num, title, column}` instead of full cards; ask for more with `--fields`.
- `done` without a write-up, or with a blank one, is `invalid` (was `state`).
- `state` is always status 409. `add` into a later column, `fly REF done`, and moving a card into or out of Blocked with `fly` or `start` returned 422.
- `attachment add` with a missing `--file` is `not_found` (was `io`).
- An existing `export --out` destination is status 422, like other `invalid` errors (was 409).
- On claimed or blocked work, `subtask configure --scope` can only grow the saved scope: send the saved paths plus the new ones. Dropping a path, or `--clear-scope`, is refused with `state`, since the worker may be editing it.
- A worker command (`done`, `undone`, `block`, `resume`, `release`) on a subtask nobody has claimed is `state`, saying to claim it first (was `owned`, "held by nobody").
- `subtask configure`, `accept`, `request-changes` and `ack` on a card that isn't In Progress are `state`, saying to start or resume it first (was `owned`).
- Removing delegated subtasks (`subtask rm`) needs the card's owner, or a card without one (`owned`).
- `handoff`'s `commands.complete` reads the result from stdin (`--result-stdin`) instead of a `--result={result}` argument, so multi-line results survive npm's Windows shim.

## [0.3.4] - 2026-10-10

### Added

- `handoff --json` adds `requests`, the worker's four steps (read, claim, publish, complete) as `call` requests, and `call`, the argument array that runs them. Workers fill in `{summary}`, `{body}` and `{result}` and send the requests on stdin, so multi-line findings and results survive npm's Windows `workboard.cmd` shim; the argument-array `commands` stay unchanged. The handoff prompt now shows the `call` form.

### Fixed

- `digest` marked an In Progress card READY TO CLOSE once every subtask was ticked, even while a delegated subtask still awaited its required review, so `done` was then refused. It now says REVIEW PENDING until `done` would pass, and the browser's card badge does the same ("Review pending").
- In `call`, a subtask action that names the subtask with `id` instead of `subtask` now fails with an error saying to use `subtask`. Before, it said "the following arguments are required: what".

### Changed

- The skill and docs explain that `query --mine` and `digest`'s MINE include Blocked cards you blocked, and that one open `call` can chain requests that need an earlier result's `rev`. They also say to write the remaining review, integration or release work as subtasks, and that Main may take over cards held by its own workers whose sessions ended.

## [0.3.3] - 2026-10-10

### Changed

- The agent skill is rewritten for `call`: a table of request keys, a whole session and a worker's requests as JSON, and advice to batch related requests into one `call`. It now covers the refusals agents actually hit (`priority` is `critical|mid|low`, `add` only into Backlog or Task, `done` only from In Progress, subtask IDs go in `subtask` rather than `id`), read-only delegated work without `handoff`, and replacing a worker that is gone with `subtask takeover`. Every JSON example and command form in it runs against the CLI. It no longer has a 6 KB size limit.
- The contributor guide, pull request template and issue forms are gone. Running from source and the tests are described in [docs/install.md](docs/install.md#from-source).

## [0.3.2] - 2026-10-10

### Fixed

- 0.3.1 made a one-shot command's board read slower: the first read in each process also stored a parsed copy for reuse, which only `call` and the server ever reuse. The first read now keeps only the text it read, and the second read of an unchanged board makes the copy. On a 3.7 MB board, the first `context` in a process took 107 ms instead of 133 ms (0.3.0: 116 ms).

## [0.3.1] - 2026-10-10

### Changed

- A WorkBoard process reuses its parse of a board while `board.json` is unchanged, so `call` and the server answer reads without parsing the whole board again. Any commit, by any process, forces a fresh parse, and each reader gets its own copy. On a real 790-card, 3.7 MB board, 20 `context` requests in one `call` took 343 ms instead of 1319 ms: about 7 ms per request after the first instead of 64 ms. `call` also builds its argument parser once instead of per request.
- Writes parse the board once instead of three times. `save` compares cards against the board the transaction loaded under the lock instead of reading it again, and the backup no longer re-parses the bytes it writes to find their revision. On the same board, one `note` took 316 ms instead of 436 ms, and 10 notes in one `call` 1.9 s instead of 3.2 s.

## [0.3.0] - 2026-10-10

### Added

- `workboard call` accepts JSON requests on stdin (a stream of objects or an array), runs agent board commands in-process without a server, and prints one compact `--json` result per request. Requests run independently and continue after errors. Text stays out of shell arguments, so multi-line notes, comments, write-ups, results and reasons survive Windows' npm `.cmd` shim.
- `workboard wait REF [--subtask ID] [--after REV] [--timeout SECONDS]` blocks until a card or subtask subtree changes, or a timeout elapses, without a server or lock. Main can await results and workers can await review or requested changes; it also works as a `wait` request in `call`.

### Changed

- The skill recommends `call` for any request that carries text, and the docs tell Windows scripts to spawn the real `workboard.exe` (`executable` in `workboard version --json`) instead of npm's `workboard.cmd` shim, which cuts arguments at line breaks and which Node and Bun refuse to start when an argument holds `"`, `&` or `|`.

## [0.2.1] - 2026-10-10

### Changed

- The card's owner can change the scope of a delegated subtask a worker has already claimed or blocked: `subtask REF configure ID --scope PATH...` or `--clear-scope` keeps the claim, the worker and any result, and records the change in the card history. Prerequisites and the review requirement still change only while the subtask is available. Approving extra files mid-work no longer means leaving the saved scope wrong.
- The owner of the In Progress card can post `note REF --subtask ID` on any delegated subtask of that card, in any state, so an approval or answer lands on the assignment instead of a card-level note. Anyone else still gets `owned`, and Main's own notes are not inbox findings.
- `digest` stays a short pulse on big boards: `MINE`, `CONTRIBUTIONS`, In Progress and Blocked list at most 8 rows each, then `… +N more` with the read that lists the rest (`query --mine`, `context REF --mine`, `query --column inprogress`, `query --column blocked`), and Blocked reasons, exit conditions and cancel reasons are cut to 80 characters. `digest --json` is unchanged. On a real board with 28 cards In Progress and 23 Blocked, `digest` had printed 61 lines and 17 KB.
- `handoff` commands name the card by its number instead of its long ID, and the prompt says that scope paths are relative to the board's project root.
- The agent skill opens with why the board matters (shared memory the user watches, peers coordinate through and the next session resumes from) and adds that one read's `rev` guards every card it covers, that Main may widen a claimed subtask's scope and post `note --subtask`, that `accept` accepts the worker's deliverable (even a verification that found a bug), and that work is delegated only to workers that can run the CLI.
- The docs cover reading many cards with one `query --json --fields …` instead of a `context` per card, filtering one `query` locally instead of many `search` calls, calling the Windows npm shim `workboard.cmd` from scripts that start commands without a shell (`shutil.which("workboard")`), what `accept` means, sending an outside reviewer's findings back with `request-changes` on the author's subtask (it works after `accept` too), and relaying for workers without CLI access.
- The skill and docs now require `--stdin`/`--*-stdin` for multi-line text. On Windows, npm's `workboard.cmd` shim runs through `cmd.exe`, which ends the command at the first line break inside an argument: the rest of that text and every later argument, including `--json`, were dropped without an error, so a multi-line `--writeup` or `--body` was saved as its first line only.

### Fixed

- The error for an invalid delegation scope shows an example path relative to the project root (`packages/web/src/app/App.tsx`).
- A card-scoped `stale` error names the card's last change (event and actor) and says which `context` command re-reads it, for example `card #761 changed at rev 5130 after reviewed rev 5128 (last: subtask-configure by Main); re-read with context 761`. The code, status and JSON fields are unchanged.
- `--expected-rev` help says which revision to pass. On card verbs: "rev from your last read or mutation; card verbs fail only if this card changed since"; on `wip`, `recover`, `sweep`, `columns-core` and `export` it is the exact board revision.

## [0.2.0] - 2026-10-10

### Added

- Delegated subtasks. The card owner keeps the parent card and hands complementary pieces to other agents: `subtask REF add TEXT --delegated` (or `subtask REF delegate ID`) makes work claimable, and `subtask REF configure ID` saves an advisory write scope, prerequisites between subtasks and a required review. Workers `claim`, `block`/`resume`, `release`, complete with `done ID --result TEXT` and reopen with `undone`; `takeover ID --reason TEXT` moves claimed work to another worker. The owner reviews with `accept` or `request-changes ID --reason TEXT`. A parent can't be completed while delegated work is unfinished or a required review is pending, and claims never expire. The board UI and the HTTP API run the same lifecycle, and `digest` shows a `CONTRIBUTIONS @actor` section.
- Focused context: `context REF --subtask ID`, `--mine` or `--assigned-to ACTOR` returns only the selected subtasks with their ancestors and prerequisites, plus the card's shared comments, notes, attachments and dependencies.
- `handoff REF --subtask ID --worker ACTOR` prepares a read-only worker brief with its write scope and peers. `--json` adds `commands`, argument arrays to read, claim, publish and complete the subtask. It refuses a subtask whose prerequisites are unfinished (`deps`) and never claims, spawns or messages anything.
- `note REF --subtask ID` publishes a finding for the subtask you have claimed.
- `inbox [REF]` lists what needs your attention: results waiting for your review, blockers, requested changes and new findings from others on cards you own. `ack REF NOTE_ID...` acknowledges findings in one write. The board UI has an Inbox view with a count.
- `add --from REF` creates a card from another card's title, pinned notes, tags, priority and subtasks, with fresh subtask IDs and all progress reset.
- `export --out PATH` writes a board, its attachments and its archives (and, with `--include-backups`, its backups) to a portable ZIP. `import FILE --name NAME --dir PROJECT` previews the ZIP and `--apply` creates a new board from it. The board menu in the browser downloads the same ZIP.
- `version --json` reports `apiVersion`, `schemaVersion`, `supportedSchemaVersions` and `capabilities`, as `/health` does.
- `service restart`, which `upgrade` also runs, checks that the restarted server is this version and can read schema 4 boards. If the service starts another installation, it fails with `state` and says to run `workboard service install`. `service status` reports `schemaMatch`, and `doctor` reports a running server that can't read boards that use delegated subtasks as the blocker `server-schema-mismatch`.

### Changed

- **A board stays schema 3 until it first uses delegated subtasks or inbox acknowledgements (`ack`). From then on that board is schema 4, and WorkBoard 0.1.x CLIs and servers can neither read nor write it.** Other boards stay readable and writable by 0.1.x; schema 1 and 2 boards still move to schema 3 on their next write, as in 0.1.x. A schema 4 board never goes back to 3. **Restart the server before the first delegated write:** `workboard upgrade` does it, and after a manual update run `workboard service restart`. Update every installation that writes the same board before delegating on it. Downgrading a schema 4 board is unsupported; the way back is its last schema 3 snapshot in `.backups/`, see [Upgrading to 0.2.0](docs/install.md#upgrading-to-020).
- `context` (and `GET api/card/{ref}/context`) returns only the newest 10 comments and reports the number left out as `omitted.comments`, like the notes timeline; `show` keeps the newest 5. Comment IDs are unchanged, and `--full` still returns every comment. Agents that coordinate through comments no longer pull the whole thread on every read: across 16 real boards, the largest card's `context` dropped from 470 KB to 30 KB.
- The agent skill no longer asks for `--expected-rev` on `comment` and `note`. They only append, and on a card shared by several agents a guarded comment went stale on every peer's write: two of six agents hit this in a parallel-work test.
- Editing or deleting a comment and detaching an attachment are limited to the entry's author, the card's owner, or anyone when no author is recorded. Anyone else gets `owned`.
- HTTP workflow errors carry the CLI's error `code` (`owned`, `state`, `deps`, …) next to `error`. Attachment downloads, like the new export download, refuse requests marked `Sec-Fetch-Site: cross-site` (403).
- `doctor`: a schema 4 board whose delegated subtasks are inconsistent is the blocker `delegation-invalid`. `legacy-schema` warns only for schema 1 and 2 boards, and only for the live board, not for its backups and archives.

### Fixed

- If `init` failed after the new board was registered, its rollback could delete the registered board's folder. Rollback now never deletes a folder the registry references.

## [0.1.3] - 2026-10-10

### Fixed

- On Windows, concurrent CLI writers could fail with spurious `lock` timeouts or `io` errors naming a temporary `board.json~RF….TMP` file. Resolving the write path no longer touches the board file another writer is replacing.
- On Windows, the board page and browser edits could fail with `410 board_missing` while another write was replacing `board.json`. The server now treats only a lasting absence as a missing board.

## [0.1.2] - 2026-09-26

### Fixed

- WorkBoard is on npm as `@paliverse/workboard`: `npm install -g @paliverse/workboard`. The commands are still `workboard` and `wb`, and `workboard upgrade` uses the new name. The unscoped name `workboard` belongs to someone else's unpublished 2023 package, so npm refuses to publish it. 0.1.0 and 0.1.1 were released on GitHub only.

## [0.1.1] - 2026-09-25

### Fixed

- The npm platform packages are published as `@paliverse/workboard-<os>-<arch>`, because npm's spam filter rejects new unscoped names such as `workboard-win32-x64`.

### Changed

- Releases publish to npm through trusted publishing (OIDC), with provenance and without a stored npm token.

## [0.1.0] - 2026-09-25

First public release.

### Added

- `workboard` command, with the short alias `wb`: a kanban board per project with exactly five columns (Backlog, Task, In Progress, Done, Blocked).
- Agent-oriented CLI:
  - `digest` (including a `MINE @actor` section and READY refs), `next`, `context`, `show`, `list`, `query` (`--mine`, `--owner`, `--fields`) and `search`;
  - lifecycle verbs `add`, `start`, `done`, `fly`, `block`, `resume`, `takeover`, `cancel`, `rework`, `reopen`, `bug` and `improve`;
  - content verbs `update`, `note`, `workpad`, bulk `subtask`, `depends`, `comment` and `attachment` (with a verified, non-overwriting export).
- One-line human output. `--json` output carries `rev`, `actor` and the identity of created or changed items. Errors use stable codes: `stale`, `owned`, `deps`, `wip`, `state`, `invalid`, `not_found`, `lock`, `scope` and `io`.
- Card-scoped `--expected-rev` guards: a conflict occurs only when the target card changed (`changedRev`) after the reviewed revision.
- `--actor` and `WORKBOARD_ACTOR` attribution. Unlabeled CLI writes record `agent`; the web UI records `user`. `--stdin` variants for comments, notes, write-ups, block reasons and origins.
- Crash-safe persistence: a cross-process lock, fsync and atomic replacement, 10 rolling backups, `recover`, `sweep` archiving, `columns-core` consolidation and an optional In Progress WIP limit (`wip`).
- One per-user board store, `~/.workboard/boards/`, with each board linked to a project folder, so repositories stay free of board files. Board commands find the board from the current folder or its parents, and git worktrees of a repository share its board. `--board` and `WORKBOARD_DEFAULT_BOARD` take a board name or a project folder. Deleting a board from the board chooser moves its folder to `~/.workboard/deleted/`.
- One per-user local server for every registered board at `http://127.0.0.1:7891/b/<board>/`, with a board chooser at `/`, live updates over Server-Sent Events, Host and Origin checks, and token-protected shutdown.
- The WorkBoard logo ("Card in Motion"): browser favicon, board-chooser mark, and `docs/assets/` lockups for dark and light themes.
- Browser UI in a single self-contained file:
  - Board, Ready now, Rework, Canceled, Insights, Git and Calendar views;
  - card and column drag and drop, vertical column stacks and undo;
  - a card side panel with comments, files and lifecycle dialogs;
  - search, filters, themes and a same-tab board switcher.
- `workboard init` creates a board for the current project and links it. `workboard open` opens it in the browser, starting the server if needed.
- `workboard link NAME` links a board to its project again after the project folder moves. `boards` and `which` show each board's project.
- Optional `~/.workboard/config.json` with `port` and `actor` defaults. Flags and environment variables take precedence.
- `workboard setup` installs the agent skill for Claude Code (`~/.claude/skills`) and for Codex, Pi, Oh My Pi, Gemini CLI, Cursor, OpenCode and GitHub Copilot (`~/.agents/skills`). It also installs a background service: HKCU Run on Windows, a LaunchAgent on macOS, and a systemd user unit on Linux.
- `workboard setup` adds `~/.workboard` to Codex's writable folders so Codex can write boards from its sandbox (`--no-codex` skips it), and `doctor` warns when Codex's config lacks that grant.
- `skills install|remove|status`, `service install|remove|status|restart`, `version [--check]`, `upgrade [--dry-run]` and `doctor [--all]`.
- Self-contained binaries for Windows, macOS and Linux (x64 and arm64) on GitHub Releases, with one-line install scripts.
- Card notes timeline: `note REF --summary TEXT [--body MARKDOWN | --stdin]` appends an entry with a one-line summary (at most 160 characters) and an optional markdown body. The card panel shows the timeline newest first, with collapsible entries and a composer; the free-form `notes` field stays as pinned notes. `context` returns the newest 10 entries and `show` the newest 5 unless `--full` is given.
- Board schema 3. Schema 1 and 2 boards are still read; their `[YYYY-MM-DD actor] text` note lines move losslessly into the timeline and are saved as schema 3 on the next write.

[Unreleased]: https://github.com/Paliverse/workboard/compare/v0.3.9...HEAD
[0.3.9]: https://github.com/Paliverse/workboard/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/Paliverse/workboard/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/Paliverse/workboard/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/Paliverse/workboard/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/Paliverse/workboard/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/Paliverse/workboard/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/Paliverse/workboard/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/Paliverse/workboard/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Paliverse/workboard/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Paliverse/workboard/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Paliverse/workboard/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Paliverse/workboard/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/Paliverse/workboard/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Paliverse/workboard/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Paliverse/workboard/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Paliverse/workboard/releases/tag/v0.1.0
