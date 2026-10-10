# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Delegated subtasks. The card owner keeps the parent card and hands complementary pieces to other agents: `subtask REF add TEXT --delegated` (or `subtask REF delegate ID`) makes work claimable, and `subtask REF configure ID` saves an advisory write scope, prerequisites between subtasks and a required review. Workers `claim`, `block`/`resume`, `release`, complete with `done ID --result TEXT` and reopen with `undone`; `takeover ID --reason TEXT` moves claimed work to another worker. The owner reviews with `accept` or `request-changes ID --reason TEXT`. A parent can't be completed while delegated work is unfinished or a required review is pending, and claims never expire. The board UI and the HTTP API run the same lifecycle, and `digest` shows a `CONTRIBUTIONS @actor` section.
- Focused context: `context REF --subtask ID`, `--mine` or `--assigned-to ACTOR` returns only the selected subtasks with their ancestors and prerequisites, plus the card's shared comments, notes, attachments and dependencies.
- `handoff REF --subtask ID --worker ACTOR` prepares a read-only worker brief with its write scope and peers. `--json` adds `commands`, argument arrays to read, claim, publish and complete the subtask. It refuses a subtask whose prerequisites are unfinished (`deps`) and never claims, spawns or messages anything.
- `note REF --subtask ID` publishes a finding for the subtask you have claimed.
- `inbox [REF]` lists what needs your attention: results waiting for your review, blockers, requested changes and new findings from others on cards you own. `ack REF NOTE_ID...` acknowledges findings in one write. The board UI has an Inbox view with a count.
- `add --from REF` creates a card from another card's title, pinned notes, tags, priority and subtasks, with fresh subtask IDs and all progress reset.
- `export --out PATH` writes a board, its attachments and its archives (and, with `--include-backups`, its backups) to a portable ZIP. `import FILE --name NAME --dir PROJECT` previews the ZIP and `--apply` creates a new board from it. The board menu in the browser downloads the same ZIP.
- `version --json` reports `apiVersion`, `schemaVersion`, `supportedSchemaVersions` and `capabilities`, as `/health` does.
- `service restart`, which `upgrade` also runs, checks that the restarted server is this version and can read the boards this CLI writes. If the service starts another installation, it fails with `state` and says to run `workboard service install`. `service status` reports `schemaMatch`, and `doctor` reports a running server that can't read them as the blocker `server-schema-mismatch`.

### Changed

- **Boards are now written as schema 4.** Reading a schema 1–3 board changes nothing; the first write migrates it. **After that, WorkBoard 0.1.x CLIs and servers can neither read nor write the board.** Restart the server before the first write: `workboard upgrade` does it, and after a manual update run `workboard service restart`. Update every installation that writes the same boards. Downgrading is unsupported; see [Upgrading to 0.2.0](docs/install.md#upgrading-to-020).
- `context` (and `GET api/card/{ref}/context`) returns only the newest 10 comments and reports the number left out as `omitted.comments`, like the notes timeline; `show` keeps the newest 5. Comment IDs are unchanged, and `--full` still returns every comment. Agents that coordinate through comments no longer pull the whole thread on every read: across 16 real boards, the largest card's `context` dropped from 470 KB to 30 KB.
- The agent skill no longer asks for `--expected-rev` on `comment` and `note`. They only append, and on a card shared by several agents a guarded comment went stale on every peer's write: two of six agents hit this in a parallel-work test.
- Editing or deleting a comment and detaching an attachment are limited to the entry's author, the card's owner, or anyone when no author is recorded. Anyone else gets `owned`.
- HTTP workflow errors carry the CLI's error `code` (`owned`, `state`, `deps`, …) next to `error`. Attachment downloads, like the new export download, refuse requests marked `Sec-Fetch-Site: cross-site` (403).
- `doctor`: a schema 4 board whose delegated subtasks are inconsistent is the blocker `delegation-invalid`. `legacy-schema` warnings are reported only for the live board, not for its backups and archives.

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

[Unreleased]: https://github.com/Paliverse/workboard/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/Paliverse/workboard/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Paliverse/workboard/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Paliverse/workboard/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Paliverse/workboard/releases/tag/v0.1.0
