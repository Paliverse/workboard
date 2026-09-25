# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-24

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
- Self-contained binaries for Windows, macOS and Linux (x64 and arm64) on GitHub Releases, installed through npm (`npm install -g workboard`) or one-line install scripts.
- Card notes timeline: `note REF --summary TEXT [--body MARKDOWN | --stdin]` appends an entry with a one-line summary (at most 160 characters) and an optional markdown body. The card panel shows the timeline newest first, with collapsible entries and a composer; the free-form `notes` field stays as pinned notes. `context` returns the newest 10 entries and `show` the newest 5 unless `--full` is given.
- Board schema 3. Schema 1 and 2 boards are still read; their `[YYYY-MM-DD actor] text` note lines move losslessly into the timeline and are saved as schema 3 on the next write.

### Changed

- `note` requires `--summary` and takes its body from `--body` or `--stdin`; `--text` was removed.
- Boards live in one per-user store, `~/.workboard/boards/`, each linked to a project folder, instead of a `board/` folder inside the project. Board commands find the board from the current folder or its parents, git worktrees of a repository share its board, and `--board` and `WORKBOARD_DEFAULT_BOARD` take a board name or a project folder. Deleting a board from the board chooser moves its folder to `~/.workboard/deleted/`.

### Removed

- `WORKBOARD_SCOPE_ROOT`. Boards no longer live in project folders, so the per-project write fence is gone.

[Unreleased]: https://github.com/Paliverse/workboard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Paliverse/workboard/releases/tag/v0.1.0
