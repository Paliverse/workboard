# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-24

First public release.

### Added

- `workboard` command, with the short alias `wb`: a kanban board per project, stored at `board/board.json`, with exactly five columns (Backlog, Task, In Progress, Done, Blocked).
- Agent-oriented CLI:
  - `digest` (including a `MINE @actor` section and READY refs), `next`, `context`, `show`, `list`, `query` (`--mine`, `--owner`, `--fields`) and `search`;
  - lifecycle verbs `add`, `start`, `done`, `fly`, `block`, `resume`, `takeover`, `cancel`, `rework`, `reopen`, `bug` and `improve`;
  - content verbs `update`, `note`, `workpad`, bulk `subtask`, `depends`, `comment` and `attachment` (with a verified, non-overwriting export).
- One-line human output. `--json` output carries `rev`, `actor` and the identity of created or changed items. Errors use stable codes: `stale`, `owned`, `deps`, `wip`, `state`, `invalid`, `not_found`, `lock`, `scope` and `io`.
- Card-scoped `--expected-rev` guards: a conflict occurs only when the target card changed (`changedRev`) after the reviewed revision.
- `--actor` and `WORKBOARD_ACTOR` attribution. `--stdin` variants for comments, notes, write-ups, block reasons and origins.
- Crash-safe persistence: a cross-process lock, fsync and atomic replacement, 10 rolling backups, `recover`, `sweep` archiving, `columns-core` consolidation and an optional In Progress WIP limit (`wip`).
- One per-user local server for every registered board at `http://127.0.0.1:7891/b/<board>/`, with a board chooser at `/`, live updates over Server-Sent Events, Host and Origin checks, and token-protected shutdown.
- Browser UI in a single self-contained file:
  - Board, Ready now, Rework, Canceled, Insights, Git and Calendar views;
  - card and column drag and drop, vertical column stacks and undo;
  - a card side panel with comments, files and lifecycle dialogs;
  - search, filters, themes and a same-tab board switcher.
- `workboard init` creates and registers a board. `workboard open` opens it in the browser, starting the server if needed.
- `workboard setup` installs the agent skill for Claude Code (`~/.claude/skills`) and for Codex, Pi, Oh My Pi, Gemini CLI, Cursor, OpenCode and GitHub Copilot (`~/.agents/skills`). It also installs a background service: HKCU Run on Windows, a LaunchAgent on macOS, and a systemd user unit on Linux.
- `skills install|remove|status`, `service install|remove|status|restart`, `version [--check]`, `upgrade [--dry-run]` and `doctor [--all]`.
- Self-contained binaries for Windows, macOS and Linux (x64 and arm64) on GitHub Releases, installed through npm (`npm install -g workboard`) or one-line install scripts.
- Card notes timeline: `note REF --summary TEXT [--body MARKDOWN | --stdin]` appends an entry with a one-line summary (at most 160 characters) and an optional markdown body. The card panel shows the timeline newest first, with collapsible entries and a composer; the free-form `notes` field stays as pinned notes. `context` returns the newest 10 entries and `show` the newest 5 unless `--full` is given.
- Board schema 3. Schema 1 and 2 boards are still read; their `[YYYY-MM-DD actor] text` note lines move losslessly into the timeline and are saved as schema 3 on the next write.

### Changed

- `note` requires `--summary` and takes its body from `--body` or `--stdin`; `--text` was removed.

[Unreleased]: https://github.com/Paliverse/workboard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Paliverse/workboard/releases/tag/v0.1.0
