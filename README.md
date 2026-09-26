<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo-light.svg" alt="WorkBoard" width="460">
</picture>
<br><br>

**One kanban board for you and your coding agents.**<br>
Agents work the board from the CLI, you work it in the browser, and every project gets its own crash-safe board.

[![CI](https://github.com/Paliverse/workboard/actions/workflows/ci.yml/badge.svg)](https://github.com/Paliverse/workboard/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/@paliverse/workboard?label=npm&color=cb3837)](https://www.npmjs.com/package/@paliverse/workboard)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-2f6fed)](docs/install.md)
[![runtime](https://img.shields.io/badge/runtime-zero%20dependencies-1e9e5a)](docs/architecture.md)
[![license](https://img.shields.io/badge/license-Apache--2.0-8b5cf6)](LICENSE)

[Get started](#install) · [Releases](https://github.com/Paliverse/workboard/releases) · [Agents](docs/agents.md) · [CLI](docs/cli.md) · [HTTP API](docs/http-api.md) · [Architecture](docs/architecture.md) · [Changelog](CHANGELOG.md)

</div>

---

![WorkBoard showing a project board with Backlog, Task, In Progress, Done and Blocked columns](docs/assets/screenshot.png)

## Why

- **People and agents on the same board.** Agents use the `workboard` CLI, which prints one concise line per action or JSON with `--json`. You use the browser. Both follow the same lifecycle rules: ownership, dependencies, an optional WIP limit and required write-ups.
- **One board per project, outside the repository.** Every board lives in `~/.workboard/boards/` and is linked to its project folder. Agents find it from the working directory, and git worktrees of a repository share its board.
- **Crash-safe concurrent writes.** Every write takes a cross-process lock, is fsynced, atomically replaces the file and keeps rolling backups. A stale writer gets a visible 409 conflict. Nothing is silently overwritten or retried automatically.
- **Local and dependency-free.** Written in pure Python with only the standard library; the release binaries don't need Python. The server listens on `127.0.0.1` only.

## Install

With [npm](https://www.npmjs.com/package/@paliverse/workboard) (Node.js 18 or newer) on Windows, macOS and Linux:

```sh
npm install -g @paliverse/workboard
```

Without Node.js, use the install script. On macOS and Linux:

```sh
curl -fsSL https://github.com/Paliverse/workboard/releases/latest/download/install.sh | sh
```

On Windows (PowerShell):

```powershell
irm https://github.com/Paliverse/workboard/releases/latest/download/install.ps1 | iex
```

Both install the `workboard` command and the short alias `wb`. Self-contained binaries for Windows, macOS and Linux (x64 and arm64) are also attached to every [GitHub Release](https://github.com/Paliverse/workboard/releases) for manual installs. See [docs/install.md](docs/install.md) for details and troubleshooting.

## Quickstart

```sh
workboard setup    # install the agent skill and the background server, and let Codex write boards
cd my-project
workboard init     # create this project's board in ~/.workboard/boards/ and link it here
workboard open     # open http://127.0.0.1:7891/b/my-project/ in your browser
```

Then work from the board or the CLI:

```sh
workboard add --title "Ship the login page" --priority mid
workboard digest   # a short summary of the board
```

## Agent setup

`workboard setup` (or `workboard skills install`) installs the bundled agent skill for every supported harness:

| Harness | Skill file |
|---|---|
| Codex, Pi, Oh My Pi, Gemini CLI, Cursor, OpenCode, GitHub Copilot | `~/.agents/skills/workboard/SKILL.md` |
| Claude Code | `~/.claude/skills/workboard/SKILL.md` |

On Windows, `~` is `%USERPROFILE%`. Restart running agent sessions so they load the skill. Give each agent its own label with `--actor NAME` or `WORKBOARD_ACTOR`. Agents only need the CLI; they never need the server. See [docs/agents.md](docs/agents.md).

Codex's default sandbox only lets commands write inside the project, and boards live in `~/.workboard`. `setup` therefore adds `~/.workboard` to the writable folders in Codex's `config.toml` (skip it with `--no-codex`). See [docs/install.md](docs/install.md#codex).

## Board UI

- Views: Board, Ready now, Rework, Canceled, Insights, Git (local and read-only) and Calendar, each with live counts.
- Drag cards between and within columns. Drag a column sideways to reorder it, or onto another column to stack it vertically. Moves and layout changes can be undone with Ctrl+Z.
- Cards open in a side panel for title, priority, tags, dependencies, links, subtasks, notes, write-up, files, activity and comments. Lifecycle actions (Start, Complete, Block, Resume, Take over, Cancel, Rework, Reopen, Improve, Follow-up) go through the same server-side checks as the CLI.
- Card notes are a collapsible timeline, newest first and grouped by day. Each entry shows a one-line summary and expands to its markdown body. Pinned notes above it hold durable context such as acceptance criteria, and Add note appends an entry from the browser.
- Live updates arrive over Server-Sent Events. Only changed cards are patched, and a field you are editing is never overwritten.
- Search with Ctrl/Cmd+K or `/`, filter by status, priority, owner, outcome, rework or tag, and choose a System, Light or Dark theme. `C` creates a card, `[` toggles the sidebar and Esc closes the top layer.
- A board switcher moves between registered boards in the same tab.

## How it works

- **One server for all boards.** A single per-user server at `http://127.0.0.1:7891/` serves every registered board at `/b/<board>/`. The root page lists your boards. `workboard setup` installs it as a background service; `workboard serve` runs it in the foreground.
- **A registry.** `~/.workboard/boards.json` records each board's name, its folder under `~/.workboard/boards/` and its linked project folder. `workboard init` creates and links a board, `workboard link` links it again after the project moves, and `workboard boards` lists them.
- **One store for every board.** Each board is a folder `~/.workboard/boards/<dir>/` holding `board.json` (the source of truth), `.backups/` (the 10 newest snapshots), `archive/` (swept Done cards) and `attachments/` (uploaded files). Nothing is written into your projects, and backing up `~/.workboard` backs up every board. A board deleted from the browser moves to `~/.workboard/deleted/`.
- **Found from the project.** Board commands use the board linked to the current folder or its nearest linked parent. In a git worktree they use the board of the main checkout, wherever the worktree is. `--board NAME` picks any other board.
- **The CLI doesn't need the server.** Board commands lock and write the file directly. They never start a server or open a browser.
- **Revision guards.** CLI card mutations pass `--expected-rev` and conflict only when *that card* changed. The browser conflicts when the board changed. Either way you get a 409 and nothing is overwritten.

Details: [docs/architecture.md](docs/architecture.md).

## Configuration

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `WORKBOARD_HOME` | `~/.workboard` | Boards, registry, settings, server state, logs and the Windows service runtime |
| `WORKBOARD_PORT` | `7891` | Server port on `127.0.0.1` |
| `WORKBOARD_ACTOR` | `agent` | Actor label recorded on CLI writes. The browser records `user`. |
| `WORKBOARD_DEFAULT_BOARD` | unset | Board name or project folder to use when `--board` is not given, instead of looking up from the current directory |

Settings that should persist go in the optional `~/.workboard/config.json`. Both keys are optional:

```json
{"port": 7891, "actor": "agent"}
```

- Port: `serve --port`, else `WORKBOARD_PORT`, else `port`, else `7891`.
- CLI actor: `--actor`, else `WORKBOARD_ACTOR`, else `actor`, else `agent`. The browser keeps its own `user` label.

WorkBoard never writes `config.json`. Changes apply to the next command; restart the service (`workboard service restart`) after changing `port`. An unknown key or an invalid value fails with an `invalid` error that names the file.

## Upgrade

```sh
workboard version --check   # compare with the latest release
workboard upgrade           # upgrade through the channel you installed with
```

`workboard upgrade` stops the running server, upgrades the way you installed (`npm install -g @paliverse/workboard@latest`, or the install script again), refreshes installed skills and restarts the service. `workboard upgrade --dry-run` prints the plan without running it.

## Uninstall

```sh
workboard service remove   # stop the server and remove the background service
workboard skills remove    # remove the agent skill files
```

Then remove the program: `npm uninstall -g @paliverse/workboard`, or delete the install script's directory. On Windows that is `%LOCALAPPDATA%\Programs\WorkBoard`; also remove it from your user `PATH` if the script added it. On macOS and Linux, delete `${XDG_DATA_HOME:-~/.local/share}/workboard` and the `~/.local/bin/workboard` and `~/.local/bin/wb` links. If `setup` gave Codex access, remove the `~/.workboard` entry from `writable_roots` in `~/.codex/config.toml`.

**Every board lives in `~/.workboard`.** Deleting that folder deletes all your boards, along with the registry, settings and logs. Copy any board you want to keep first. See [docs/install.md](docs/install.md#uninstall).

## Documentation

- [Installation](docs/install.md): channels, setup and Codex access, the background service, backups, uninstalling and troubleshooting
- [Agents](docs/agents.md): how agents find the board, harness skill paths, actors and concurrency etiquette
- [CLI reference](docs/cli.md): every command and flag
- [HTTP API](docs/http-api.md): server and per-board endpoints
- [Architecture](docs/architecture.md): modules, the board store and lookup, locking and the server
- [Releasing](docs/releasing.md): the maintainer release process

## Contributing

Bug reports, ideas and pull requests are welcome. Read [CONTRIBUTING.md](.github/CONTRIBUTING.md) and the [Code of Conduct](.github/CODE_OF_CONDUCT.md). Report security issues privately as described in [SECURITY.md](.github/SECURITY.md).

## License

[Apache License 2.0](LICENSE)
