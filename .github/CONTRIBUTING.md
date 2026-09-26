# Contributing to WorkBoard

Thanks for helping. Bug reports, ideas, docs fixes and pull requests are all welcome. Report security issues privately as described in [SECURITY.md](SECURITY.md), not in public issues.

## Development setup

You need Python 3.11 or newer and git. There are no runtime dependencies.

Install an editable copy into a virtual environment. `pyproject.toml` exists only for this development install; releases ship PyInstaller binaries.

```sh
git clone https://github.com/Paliverse/workboard
cd workboard
python -m venv .venv
.venv/bin/python -m pip install -e .   # Windows: .venv\Scripts\python.exe -m pip install -e .
.venv/bin/workboard --version          # Windows: .venv\Scripts\workboard --version
```

With plain Python, no install needed:

```sh
PYTHONPATH=src python -m workboard --version
```

```powershell
$env:PYTHONPATH = "src"; python -m workboard --version
```

Keep development runs away from your real boards, settings and service. Point `WORKBOARD_HOME` at a scratch directory (every board lives there), and use a free port instead of 7891:

```sh
export PYTHONPATH=src WORKBOARD_HOME="$PWD/build/dev-home" WORKBOARD_PORT=7990
python -m workboard init demo  # a scratch board linked to this checkout
python -m workboard serve      # http://127.0.0.1:7990/b/demo/
```

Never run `setup`, `skills install` or `service install` from a development checkout unless you mean to replace your installed skill and service. `setup` also edits your Codex config; `--no-codex` skips that.

## Running tests

From the repository root:

```sh
python -m unittest discover -s tests -t .      # everything
python -m unittest tests.test_cli -v           # one module
```

The tests use only the standard library and need no install: `tests/support.py` puts `src` on `sys.path`.

| Area | Module |
|---|---|
| CLI, schema, locking, backups, recovery | `tests.test_cli` |
| Board store, registry, lookup, worktrees, `config.json` | `tests.test_boards` |
| HTTP server, browser endpoints, SSE | `tests.test_server` |
| Skills, service, version, upgrade | `tests.test_setup` |
| Doctor | `tests.test_doctor` |

- `WORKBOARD_TEST_COMMAND="path/to/workboard"` runs the CLI tests against a built binary instead of `python -m workboard`.
- `WORKBOARD_TEST_REAL_SERVICE=1` enables tests that register a real OS service. CI sets it; don't set it on your own machine.

Test rules:

- **Scratch homes only.** Every test runs against a scratch `HOME`, `USERPROFILE`, `WORKBOARD_HOME`, `CODEX_HOME`, `APPDATA`, `LOCALAPPDATA` and `XDG_*` from `tests/support.py` (`scratch()`, `make_env()`, `run()`, `last_json()`). A test must never read or write the real user's boards, registry, skills, service registrations or Codex config.
- **Test servers** listen on port 0 or a free ephemeral port and are always stopped.
- **Test contracts, not implementation.** A test should fail on a plausible bug in observable behavior: output, exit codes, persisted data, HTTP responses or conflicts. Delete tests that only pin wording or implementation details.

CI runs the suite on Windows, macOS and Linux with Python 3.11 to 3.14, and runs the CLI suite against the PyInstaller binaries.

## Rules for changes

WorkBoard's safety depends on a few rules. Pull requests that weaken them won't be merged.

- **Standard library only.** No runtime dependencies, database, frontend build step or network services. `board.html` stays one self-contained file.
- **Exactly five columns:** `backlog`, `task`, `inprogress`, `done` and `blocked`. Use tags, priorities and notes for anything else.
- **Every write goes through `core`:** locking, fsync and atomic replacement, revision bumps and rolling backups. Lock timeouts stay visible.
- **Conflicts are visible.** Stale state returns a 409 (`stale` for card-scoped CLI guards; board-scoped for the browser and maintenance). Never retry automatically, overwrite silently or report a failure as success.
- **Fields written by the browser** (`stackUnder`, `wipLimit`, column order) and unknown extension fields survive every round trip.
- **No surprise side effects.** Board commands never start a server or open a browser. Only `serve`, `open`, `setup`, `service` and `upgrade` may start the server, and only `open` and `serve --open` open a browser.
- **Stable output.** Mutations print one concise line. `--json` prints one line with `rev` and card identity. Error codes (`stale|owned|deps|wip|state|not_found|invalid|lock|scope|io`) are a public contract.
- **Cheap imports.** Heavy or OS-specific modules are imported inside functions, and `workboard digest` must not import the server.
- **Portable skill.** `src/workboard/skills/workboard/SKILL.md` holds only the agent card protocol, with frontmatter of only `name` and `description`. It stays at 6 KB or less, uses the bare `workboard` command, and contains no absolute paths or shell-specific syntax. Operator procedures belong in `docs/`.
- **License header.** New source files start with the same two-line `SPDX-License-Identifier` and copyright header as the existing modules (after any `#!` line).

See [docs/architecture.md](../docs/architecture.md) for how the pieces fit together.

## UI changes need a real browser

Endpoint tests don't prove drag and drop, stacking, focus, theme or layout behavior. For any change to `src/workboard/web/board.html`:

- run it in a real browser against a scratch server;
- exercise the gesture or state transition you changed;
- check that the console shows no errors.

Say in the pull request what you exercised and in which browser. Keep interactions smooth: render cards by key and animate the side panel with compositor-only transforms.

## Maintainer and agent rules

These rules are for maintainers and coding agents changing this repository, on top of the rules above. To track work with WorkBoard itself, see [docs/agents.md](../docs/agents.md).

**Scope.** WorkBoard is a minimal, standalone kanban board: one board per project, one `workboard` CLI (alias `wb`) and one per-user local server. Don't add accounts, an MCP server or a custom supervisor. A feature belongs here only if it directly improves the board, the CLI, persistence safety or multi-project work; prefer deleting complexity to adding infrastructure.

### Source ownership

| Path | Owns |
|---|---|
| `src/workboard/core.py` | Schema normalization, the board store (`home()`, `boards_dir()`, `deleted_dir()`), the registry, `config.json`, board lookup from project folders and git worktrees, locking, atomic persistence, backups, archives, lifecycle rules and attachments |
| `src/workboard/cli.py` | The argument parser, board commands, one-line and `--json` output, and the error envelope |
| `src/workboard/server.py` | The per-user HTTP/SSE server, browser mutation endpoints and lifecycle helpers (`server_info`, `stop`, `start_background`, `open_board`) |
| `src/workboard/web/board.html` | The whole browser UI, served directly with no generated bundle |
| `src/workboard/install.py` | `setup` (including the Codex writable-folder grant), `skills …`, `service …`, `server_command()` and the Windows runtime copy |
| `src/workboard/update.py` | `version`, channel detection (`npm`, `script`, `source`, `unknown`) and `upgrade` |
| `src/workboard/doctor.py` | Read-only installation and data-integrity checks |
| `src/workboard/skills/workboard/SKILL.md` | The single portable agent skill |
| `packaging/`, `scripts/`, `.github/workflows/` | PyInstaller binaries, npm packages, the install scripts and CI/release |
| `README.md`, `docs/`, `.github/*.md` | User, operator, API and contributor documentation |
| `tests/` | Stdlib `unittest` suites; `tests/support.py` provides scratch environments |

### Data and concurrency

- Each board is one folder, `~/.workboard/boards/<dir>/`, and its `board.json` is the single source of truth. Nothing is written into project folders. Never hand-edit a board while the server or an agent may be writing.
- The registry `~/.workboard/boards.json` (version 2) maps each board name to its `dir` and linked `project`. Change it only through `core` (`create_board`, `link_board`, `delete_registered_board`), which takes the registry lock, then the board lock. `dir` stays a single safe path component, and a project has at most one board. Don't write `boards.json` or `server.json` by hand, and never write `config.json` from code: it belongs to the user.
- Deleting a board moves its whole folder to `~/.workboard/deleted/`. Nothing is erased.
- A guarded card mutation is a 409 `stale` only when its card's `changedRev` is greater than the reviewed revision. Board-scoped operations (`wip`, `sweep`, `recover`, `columns-core`, board deletion) and browser writes need the exact board revision. Check under the same board lock as the write.
- `--json` mutations carry the identity (`item`/`items`) of any created or changed subtask, comment, attachment or note entry, and JSON errors carry the current `rev`.

### Server and browser

- There is exactly one server per user. It binds `127.0.0.1` on the configured port (`serve --port`, else `WORKBOARD_PORT`, else `port` in `config.json`, else 7891) and serves every registered board at `/b/<name>/`. Don't reintroduce per-board servers, port scans or per-board port files.
- Keep the Host, Origin and Content-Type checks and the shutdown token. `server.stop()` never kills processes.
- Keep the browser mutation contract ([docs/http-api.md](../docs/http-api.md)) and the server endpoints in sync. A UI affordance hasn't shipped if its server round trip drops or rewrites the state it changes.
- In `board.html`, requests are relative to the page (`api/...`, `events`, `board.json`), root-level calls resolve against the server root, and per-board `localStorage` keys are namespaced by board name.
- Column drag supports both horizontal reordering and vertical stacking in the bottom zone. Reordering a stacked child promotes it to a root, and `stackUnder` persists across reloads.
- SSE connections stay open. Readiness checks use `/health` or `rev`, never network-idle heuristics.

### Restarting a running server

After changing `core.py`, `server.py` or `web/board.html`, restart the WorkBoard server that loads them before reporting the change as done: `workboard service restart` for the background service, or stop and rerun your foreground `workboard serve`. Check it with `GET /health` (version, pid), then exercise the change against it; a temporary verification server doesn't replace the one the user has open. Restart only servers you started or were asked to restart, and never kill an unverified process. CLI-only, test-only and docs-only changes need no restart.

### Parallel agents

When several agents edit at once, each runs only its own test module. The coordinating agent runs the full suite once every change has landed.

## Pull requests

- Keep each pull request focused, and describe the behavior it changes.
- Update the docs in `docs/` and `README.md` when behavior changes, in the same pull request. Add a line under `## [Unreleased]` in [CHANGELOG.md](../CHANGELOG.md).
- Fill in the pull request template: which tests you ran, and the browser check for UI changes.
- CI must pass on all three operating systems.

## Releases

Maintainers cut releases from tags. The version lives only in `src/workboard/__init__.py`, and `LICENSE` ships in every distributable. The checklist, required secrets and npm setup are in [docs/releasing.md](../docs/releasing.md).

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](../LICENSE).
