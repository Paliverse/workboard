# WorkBoard contributor rules

These rules are for people and coding agents changing this repository. For using WorkBoard to track work, see [docs/agents.md](docs/agents.md) and the bundled skill.

## Product direction

- WorkBoard is a minimal, standalone kanban board for people and coding agents. It has one board per project, one `workboard` CLI (alias `wb`) and one per-user local server for every registered board.
- The runtime is Python standard library only (3.11+). Don't add package dependencies, a database, an MCP server, a custom supervisor, accounts or a frontend build step.
- Keep the core features:
  - crash-safe concurrent writes;
  - an animated local web board;
  - card and column drag and drop, and vertical column stacks;
  - same-tab board switching;
  - a concise, scriptable agent CLI.
- Prefer deleting complexity to adding infrastructure. A feature belongs here only if it directly improves the board, the CLI, persistence safety or multi-project work.

## Source ownership

| Path | Owns |
|---|---|
| `src/workboard/core.py` | Schema normalization, board discovery, locking, atomic persistence, backups, archives, the board registry, lifecycle rules, attachments and paths (`home()`, `registry_path()`, `server_state_path()`, `logs_dir()`) |
| `src/workboard/cli.py` | The argument parser, board commands, one-line and `--json` output, and the error envelope |
| `src/workboard/server.py` | The per-user HTTP/SSE server, browser mutation endpoints and lifecycle helpers (`server_info`, `stop`, `start_background`, `open_board`) |
| `src/workboard/web/board.html` | The whole browser UI, served directly with no generated bundle |
| `src/workboard/install.py` | `setup`, `skills …`, `service …`, `server_command()` and the Windows runtime copy |
| `src/workboard/update.py` | `version`, channel detection and `upgrade` |
| `src/workboard/doctor.py` | Read-only installation and data-integrity checks |
| `src/workboard/skills/workboard/SKILL.md` | The single portable agent skill |
| `packaging/`, `scripts/`, `bucket/`, `.github/workflows/` | Binaries, npm, Homebrew, winget, Scoop, the install scripts and CI/release |
| `docs/`, `README.md` | User, operator and API documentation |
| `tests/` | Stdlib `unittest` suites; `tests/support.py` provides scratch environments |

Skill rules:

- `SKILL.md` holds only the agent card protocol. Keep it at 6 KB or less, with frontmatter containing only `name` and `description`.
- It uses the bare `workboard` command, with no absolute paths, placeholders or shell-specific syntax.
- Operator and maintenance procedures belong in `docs/`.

## Data and concurrency invariants

- `board/board.json` is each project's single source of truth. Never hand-edit it while the server or an agent may be writing.
- Route every product write through `core`'s locking and save paths. Preserve:
  - lock timeouts that fail visibly;
  - fsync and atomic replacement;
  - revision bumps;
  - `changedRev` stamping;
  - rolling backups.
- Don't write `~/.workboard/boards.json` or `server.json` by hand. Use the registry and server helpers.
- Fields written by the browser must survive every normalization and API round trip. Column order is the order of `columns`, vertical layout is `stackUnder`, and `wipLimit` is optional.
- The persisted column set is exactly `backlog`, `task`, `inprogress`, `done` and `blocked`. Don't add Review, Discarded, Notes, Ideas or custom columns; use tags, priorities, notes and docs.
- Conflicts:
  - A guarded card mutation returns a visible 409 when its target card changed after the reviewed revision (card `changedRev` greater than expected).
  - Board-scoped operations (`wip`, `sweep`, `recover`, `columns-core`, board deletion) and browser mutations conflict when the board revision differs.
  - Check under the same board lock. Never silently overwrite, retry automatically or turn a failure into apparent success.
- CLI output: ordinary mutations print one concise line. `--json` stays machine-readable. It contains the resulting revision, the card identity and the identity (`item`/`items`) of any created or changed subtask, comment or attachment. JSON errors carry a stable `code` (`stale|owned|deps|wip|state|not_found|invalid|lock|scope|io`) and the current `rev`.

## Server and browser behavior

- There is exactly one server per user. It binds `127.0.0.1:<port>` (default 7891, `WORKBOARD_PORT`) and serves every registered board at `/b/<name>/`. Don't reintroduce per-board servers, port scans or per-board port files.
- Board commands must never start a server or open a browser. Only `serve`, `open`, `setup`, `service` and `upgrade` may start the server. Only `open` and `serve --open` may open a browser. `serve --service` never does.
- Keep module imports cheap. `http.server`, `winreg`, `plistlib`, `urllib.request` and `subprocess` are imported inside functions, and `workboard digest` must not import `server`.
- Keep the Host, Origin and Content-Type checks and the shutdown token.
- `server.stop()` never kills processes.
- Keep the browser mutation contract ([docs/http-api.md](docs/http-api.md)) and the server endpoints in sync. A UI affordance hasn't shipped if its server round trip drops or rewrites the state it changes.
- Column drag must support both horizontal reordering and bottom-zone vertical stacking. Reordering a stacked child promotes it to a root, and stacking persists `stackUnder` across reloads.
- In `board.html`, requests are relative to the page (`api/...`, `events`, `board.json`). Root-level calls resolve against the server root. Per-board `localStorage` keys are namespaced by board name.
- SSE connections are intentionally long-lived. Readiness checks must use `/health` or `rev`, never network-idle heuristics.

## Tests

Run from the repository root:

| Change | Command |
|---|---|
| CLI, schema, locking, backups, recovery, registry | `python -m unittest tests.test_cli -v` |
| Server, HTTP endpoints, SSE, board routing | `python -m unittest tests.test_server -v` |
| Skills, service, version, upgrade | `python -m unittest tests.test_setup -v` |
| Doctor | `python -m unittest tests.test_doctor -v` |
| Before a pull request | `python -m unittest discover -s tests -t .` |

- Every test and manual run uses a scratch `HOME`/`USERPROFILE`/`WORKBOARD_HOME`/`APPDATA`/`LOCALAPPDATA`/`XDG_*` from `tests/support.py`, and sets `PYTHONDONTWRITEBYTECODE=1`. Never register boards, skills or services in the real home.
- Scratch servers use port 0 or a free ephemeral port, never 7891, and are always stopped.
- Tests that register real OS services run only with `WORKBOARD_TEST_REAL_SERVICE=1` (CI).
- Keep tests that defend real contracts. Delete tests that pin wording or implementation details.
- Changes to `board.html` interactions require exercising the affected gesture or state transition in a real browser, with no console errors. Endpoint tests alone don't prove drag, stacking, focus, theme or layout behavior.
- During parallel multi-agent edits, each worker runs only its own test module. The coordinating agent runs the full suite once all changes land.

## Running server lifecycle

- After changing code or assets loaded by a running WorkBoard server (`server.py`, `core.py` or `web/board.html`), restart that server before reporting completion. Use `workboard service restart` for the background service, or stop and rerun your foreground `workboard serve`.
- Verify the restarted server through `GET /health` (version, pid), then exercise the changed behavior against it. A temporary verification server doesn't replace restarting the one the user has open.
- Restart only servers you started or the user asked you to restart. Never kill an unverified process; `workboard service restart` uses the shutdown token.
- CLI-only, test-only and documentation-only changes don't require a restart.

## Docs and releases

- Behavior changes update `docs/`, `README.md` and the `## [Unreleased]` section of `CHANGELOG.md` in the same change.
- The version lives only in `src/workboard/__init__.py`. Releases follow [docs/releasing.md](docs/releasing.md).
