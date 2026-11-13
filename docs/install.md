# Installing WorkBoard

WorkBoard installs a `workboard` command and the short alias `wb`. npm and the install scripts install the same self-contained binary, which bundles its own Python runtime. Binaries exist for Windows, macOS and Linux on x64 and arm64. The Linux binaries need glibc 2.35 or newer, so musl-based distributions such as Alpine aren't supported.

## Channels

### npm (Windows, macOS, Linux)

```sh
npm install -g @paliverse/workboard
```

This needs Node.js 18 or newer. The `@paliverse/workboard` package installs the `workboard` and `wb` commands. They launch a prebuilt binary from a platform package (`@paliverse/workboard-win32-x64`, `@paliverse/workboard-darwin-arm64`, `@paliverse/workboard-linux-x64`, …), which npm selects automatically as an optional dependency. Node.js is only used to launch the binary.

On Windows, npm installs the commands as `workboard.cmd` and `wb.cmd` shims, which pass every argument through `cmd.exe`: it cuts text at line breaks and mangles quotes, `%`, `^` and `&`, and Rust and Node programs refuse to start a `.cmd` with such arguments. So the package also copies a small launcher to `workboard.exe` and `wb.exe` in the same folder (npm's global prefix, usually `%APPDATA%\npm`). Lookups that follow `PATHEXT`, such as `cmd.exe` and Python's `subprocess`, pick the `.exe` first; it runs the platform package's `workboard.exe` with the arguments untouched. Bash (Git Bash, MSYS, omp's bash) runs npm's extensionless `workboard` and `wb` sh shims instead, so the package rewrites them to run the binary directly rather than through Node.js; npm's Node.js line stays in them as the fallback. The package's postinstall does both, and every run through npm's own shims does them again if the launchers are missing or npm restored its original sh shims, so `--ignore-scripts` installs get them too. Only npm's global layout gets the launcher; other package managers keep their own shims. If `where workboard` lists `workboard.cmd` before any `workboard.exe`, scripts should spawn the real `workboard.exe` that `workboard version --json` reports as `executable`; see [Windows scripts](agents.md#machine-readable-output).

### Install script

macOS and Linux:

```sh
curl -fsSL https://github.com/Paliverse/workboard/releases/latest/download/install.sh | sh
```

This installs to `${XDG_DATA_HOME:-$HOME/.local/share}/workboard/` and links `workboard` and `wb` into `~/.local/bin`. The script never edits shell profiles. If `~/.local/bin` isn't on your `PATH`, it tells you what to add.

Windows (PowerShell):

```powershell
irm https://github.com/Paliverse/workboard/releases/latest/download/install.ps1 | iex
```

This installs `workboard.exe`, `wb.exe` and `workboardw.exe` to `%LOCALAPPDATA%\Programs\WorkBoard\` and adds that directory to your user `PATH`. Open a new terminal afterwards.

Both scripts:

- verify the download against the release's `SHA256SUMS`;
- write `install-receipt.json` next to the executable, which `workboard upgrade` uses to detect the channel;
- finish by printing `Run: workboard setup`, without running it themselves.

| Variable | Effect |
|---|---|
| `WORKBOARD_VERSION` | Install a specific release (`0.1.0` or `v0.1.0`) instead of the latest. |
| `WORKBOARD_INSTALL_BASE_URL` | Download the assets and `SHA256SUMS` from this http(s) base URL instead of GitHub (for mirrors). |
| `WORKBOARD_NO_MODIFY_PATH=1` | Windows only: don't change the user `PATH` (same as `install.ps1 -NoModifyPath`). |

```sh
curl -fsSL https://github.com/Paliverse/workboard/releases/latest/download/install.sh | WORKBOARD_VERSION=0.1.0 sh
```

```powershell
$env:WORKBOARD_VERSION = "0.1.0"; irm https://github.com/Paliverse/workboard/releases/latest/download/install.ps1 | iex
```

### GitHub Releases

Every [release](https://github.com/Paliverse/workboard/releases) has self-contained archives:

- `workboard-windows-x64.zip` and `workboard-windows-arm64.zip`
- `workboard-macos-x64.tar.gz` and `workboard-macos-arm64.tar.gz`
- `workboard-linux-x64.tar.gz` and `workboard-linux-arm64.tar.gz`

Each archive has a top-level `workboard/` directory. Keep the whole directory together, because the executables need `_internal/` next to them. Put the directory on your `PATH`. The release also includes `SHA256SUMS` and the install scripts. A manually unpacked archive has no install receipt, so `workboard upgrade` can't upgrade it; download the new archive yourself, or switch to npm or the install script.

### From source

You need Python 3.11 or newer and git. There are no runtime dependencies, so a checkout runs as is:

```sh
git clone https://github.com/Paliverse/workboard
cd workboard
PYTHONPATH=src python -m workboard --version   # PowerShell: $env:PYTHONPATH = "src"; python -m workboard --version
```

For `workboard`, `wb` and `workboardw` commands, install it editable in a virtual environment: `python -m venv .venv`, then `.venv/bin/python -m pip install -e .` (Windows: `.venv\Scripts\python.exe -m pip install -e .`).

To try changes without touching your real boards, settings or service, point `WORKBOARD_HOME` at a scratch folder and use a port other than 7891:

```sh
export PYTHONPATH=src WORKBOARD_HOME="$PWD/build/dev-home" WORKBOARD_PORT=7990
python -m workboard init demo   # a scratch board linked to this checkout
python -m workboard serve       # http://127.0.0.1:7990/b/demo/
```

Don't run `setup`, `skills install` or `service install` from a checkout unless you mean to replace your installed skill and service.

Run the tests from the repository root; they use only the standard library and always run in scratch homes on free ports:

```sh
python -m unittest discover -s tests -t .   # everything
python -m unittest tests.test_cli -v        # one module
```

`WORKBOARD_TEST_COMMAND=path/to/workboard` runs the CLI suites against a built binary instead of `python -m workboard`. Building binaries is described in [releasing.md](releasing.md#building-locally).

## Setup

```sh
workboard setup
```

`setup` installs the agent skill (see [agents.md](agents.md)) and the background service, lets Codex write to `~/.workboard` (see [Codex](#codex)), then prints one line per action. Use `--no-skills`, `--no-service` or `--no-codex` to skip a part. Run it again after changing your installation; it is idempotent.

Then, in each project:

```sh
workboard init     # creates the project's board in ~/.workboard/boards/ and links it
workboard open     # opens it in the browser
```

Boards live in `~/.workboard/boards/`, not in the project. Run `init` once per repository; its git worktrees share the board. After moving or renaming a project folder, run `workboard link NAME` in the new location.

### Codex

Codex's sandbox only lets commands write inside the workspace by default, so `workboard` commands run by Codex couldn't write the board. The setup step checks Codex's config (`$CODEX_HOME/config.toml`, default `~/.codex/config.toml`) and prints `codex: <state>`:

| State | Meaning |
|---|---|
| `granted` | The WorkBoard home is in `writable_roots`. If `setup` added it, the previous file is saved as `config.toml.workboard-bak`. |
| `absent` | There is no Codex config file, so nothing was changed. Run `workboard setup` again after you start using Codex. |
| `full-access` | Codex runs with `sandbox_mode = "danger-full-access"` and can already write anywhere. |
| `manual` | WorkBoard doesn't edit this config: it can't be parsed, it uses permission profiles (`default_permissions`), its `writable_roots` spans several lines, or the edit didn't verify and the original file was restored. `setup` prints the reason and the snippet to add yourself. |

When `setup` edits the file, it adds your WorkBoard home to the `[sandbox_workspace_write]` table:

```toml
[sandbox_workspace_write]
writable_roots = ['/home/me/.workboard']
```

Restart running Codex sessions afterwards. `workboard doctor` warns with `codex-sandbox` when Codex's config exists but doesn't grant the WorkBoard home. Other harnesses that sandbox file writes need the same access.

## Background service

The service runs `workboard serve --service` at login. It serves every registered board at `http://127.0.0.1:7891/` (or the `port` set in `~/.workboard/config.json`) and writes its output to `~/.workboard/logs/server.log`.

```sh
workboard service install    # register and start (safe to repeat)
workboard service status     # registration, running server, version match
workboard service restart    # stop and start again
workboard service remove     # stop and unregister
```

| OS | Registration | Inspect |
|---|---|---|
| Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `WorkBoard` | Task Manager → Startup apps |
| macOS | `~/Library/LaunchAgents/io.github.paliverse.workboard.plist` | `launchctl print gui/$(id -u)/io.github.paliverse.workboard` |
| Linux | `~/.config/systemd/user/workboard.service` | `systemctl --user status workboard` |

- **Windows:** the service starts at login. Binary installs run a private copy from `~/.workboard/runtime/<version>-<fp>/`, where `<fp>` identifies the build, so npm and the install script can replace the installed files while it runs.
- **macOS:** the agent starts at login and is restarted if it crashes.
- **Linux:** the unit restarts on failure. systemd stops user services when you log out unless lingering is enabled (`loginctl enable-linger $USER`). Without a working `systemctl --user` (some containers and WSL setups), `service install` writes `~/.config/autostart/workboard.desktop` and starts the server immediately.

You don't need the service: `workboard serve` runs the same server in the foreground, and `workboard open` starts it in the background when needed. Board commands never need a server.

## Upgrade

```sh
workboard version --check
workboard upgrade --dry-run   # show the plan
workboard upgrade
```

`upgrade` detects how WorkBoard was installed (`workboard version` shows it), stops the server, runs that channel's upgrade command, refreshes installed skills and restarts the service:

| Channel | Command it runs |
|---|---|
| `npm` | `npm install -g --prefer-online @paliverse/workboard@latest` |
| `script` (Windows) | `powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://github.com/Paliverse/workboard/releases/latest/download/install.ps1 \| iex"` |
| `script` (macOS, Linux) | `sh -c "curl -fsSL https://github.com/Paliverse/workboard/releases/latest/download/install.sh \| sh"` |

The `script` channel is detected by the `install-receipt.json` next to the executable. The other channels exit 1 with instructions:

- `source`, a git checkout: pull it (`git pull`); an editable install picks the change up (see [From source](#from-source)).
- `unknown`, anything else, such as a manually unpacked archive: reinstall with `npm install -g @paliverse/workboard` or the install script.

After updating by hand, run `workboard skills install --refresh` and `workboard service restart`.

`workboard service restart` checks the server it started. If that server is another version, the registered service points at another installation: the command exits 1 with `state`, names the running server's version and executable, and asks you to run `workboard service install`, then `workboard service restart`. `upgrade` restarts through the new version's `service restart`, so the same check guards it. If a later step fails after `upgrade` stopped the server, the error says so; `workboard service restart` brings it back.

`workboard service status` and `workboard doctor` (`server-schema-mismatch`) flag a running server whose version differs from the CLI's, since it may not read your boards.

### Upgrading to 0.4.0

0.4.0 keeps each board in a SQLite file, `board.db`, instead of one large `board.json` (see [architecture.md](architecture.md#persistence)).

- **Boards migrate on first use.** The first 0.4.0 command or server request that opens a board moves it into `board.db` and replaces `board.json` with a small stub that points at it. There is nothing to run. Cards that `sweep` had moved to `archive/` become cards on the board again; cards that `columns-core --apply` removed along with their column stay out.
- **The original stays.** The `board.json` a board had before the migration is kept as `~/.workboard/boards/<dir>/.backups/board-<rev>.json`, where `<rev>` is its last revision. WorkBoard never changes or deletes it, and the older snapshots in `.backups/` stay as they were.
- **Restart the server.** `workboard upgrade` does it. After a manual update (npm, the install script or a new archive), run `workboard service restart`. A 0.3.x server that keeps running can't read migrated boards: its pages and API fail with `unsupported board schemaVersion 5` until the restart. Update every installation that writes your boards, and restart running agent sessions so they pick up the refreshed skill.
- **Older versions refuse migrated boards.** 0.3.x and earlier CLIs and servers reject the stub with `unsupported board schemaVersion 5` instead of reading the board as empty, and they never write it.
- **There is no downgrade, except restoring the pre-migration file.** To go back for one board, reinstall the version you used before and stop its server and your agents. In the board's folder, move `board.db` (and `board.db-wal` and `board.db-shm`, if present) out of the folder, then copy `.backups/board-<rev>.json` over `board.json`. Every change made since the migration is lost. With both a full `board.json` and a `board.db` in the folder, 0.4.0 refuses to open the board instead of choosing one.
- **Removed commands.** `export` and `import` are gone: to back up or move boards, copy `~/.workboard` (see [Backups](#backups)). `sweep`, `next`, `ack`, `workpad`, `subtask block`, `resume` and `release`, subtask prerequisites, card links and comments are gone too; existing comments become timeline notes. The [CHANGELOG](../CHANGELOG.md) lists every change.
- **Windows binary installs:** `upgrade` continues in a temporary copy, so your shell never sees its exit code. Run `workboard service status` or `workboard doctor` afterwards.

### Upgrading to 0.2.0

0.2.0 adds schema 4 for delegated subtasks. A board stays schema 3, which 0.1.x reads and writes, until its first delegated subtask or inbox acknowledgement (`ack`). That write moves the board to schema 4, and from then on 0.1.x CLIs and servers can neither read nor write it. Boards that never use delegation keep working with 0.1.x, and a schema 4 board never goes back to 3. From 0.4.0 on, every board moves to `board.db` on first use instead; see [Upgrading to 0.4.0](#upgrading-to-040).

- **Restart the server before the first delegated write.** `workboard upgrade` does it. After a manual update (npm, the install script or a new archive), run `workboard service restart`. Before delegating on a board, update every installation that writes it, and restart running agent sessions.
- **Windows binary installs:** `upgrade` continues in a temporary copy, so your shell never sees its exit code. Run `workboard service status` or `workboard doctor` afterwards.
- **Downgrading a schema 4 board is unsupported.** The only way back is its last snapshot from before the switch, `~/.workboard/boards/<dir>/.backups/board-<rev>.json` with `"schemaVersion": 3`. Each board keeps only its 10 newest snapshots, so copy it elsewhere if you may need it. To go back, reinstall 0.1.x, stop its server and your agents, and copy the snapshot over the board's `board.json`; every change since the switch is lost.

## Backups

Every board lives in `~/.workboard/boards/`, outside your repositories, so committing a project doesn't save its board. Include `~/.workboard` in your backups: `boards/` and `boards.json` hold every board and its project link. Copy it while no agent, command or browser tab is writing, and keep each `board.db` with its `board.db-wal`: a copy taken during a write can miss the newest changes, and `workboard doctor` checks a copied store. Each board's `board.db` also keeps the previous version of every card a revision changed, for up to the last 2,000 revisions and about 64 MB, which `workboard recover` restores; that protects against a bad write, not against losing the disk. Boards deleted from the board chooser stay in `~/.workboard/deleted/` until you delete them.

## Uninstall

First remove what `setup` installed, while `workboard` still exists:

```sh
workboard service remove
workboard skills remove
```

If `setup` gave Codex access, remove the WorkBoard home from `writable_roots` in `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`). `config.toml.workboard-bak` is the file as it was before `setup` edited it.

Then remove the program:

| Installed with | Remove |
|---|---|
| npm | `npm uninstall -g @paliverse/workboard`. On Windows, npm leaves the launchers behind: delete `workboard.exe` and `wb.exe` (and any `workboard.exe.old` or `wb.exe.old`) from npm's global folder, `npm prefix -g` (usually `%APPDATA%\npm`). Left behind, they print what is missing and exit 1. |
| Script (Windows) | Delete `%LOCALAPPDATA%\Programs\WorkBoard\`, and remove it from your user `PATH` if the script added it |
| Script (macOS, Linux) | Delete `${XDG_DATA_HOME:-~/.local/share}/workboard/` and the `~/.local/bin/workboard` and `~/.local/bin/wb` links |
| GitHub Release archive | Delete the unpacked `workboard/` directory and remove it from your `PATH` |

**Deleting `~/.workboard` deletes every board**, along with the registry, settings, logs and runtime copies. Copy the boards you want to keep first.

## Troubleshooting

Start with:

```sh
workboard doctor          # installation + current board
workboard doctor --all    # + every registered board
workboard doctor --json   # full report: {"ok", "blockers", "warnings", ...}
```

`doctor` exits 1 when it finds blockers. Include `workboard version --json` and the doctor output in bug reports.

| Symptom | Fix |
|---|---|
| `workboard: command not found` | Open a new terminal. For the POSIX script, add `~/.local/bin` to `PATH`. For npm, make sure npm's global `bin` directory is on `PATH`. |
| `the platform package @paliverse/workboard-<os>-<arch> is not installed` | npm skipped optional dependencies. Reinstall without `--omit=optional` or `--no-optional`: `npm install -g @paliverse/workboard`. |
| `workboard: ...\workboard-win32-x64\workboard\workboard.exe is missing` | The `workboard.exe` or `wb.exe` launcher in npm's global folder outlived its package: npm uninstalled it or skipped optional dependencies. Reinstall with `npm install -g @paliverse/workboard`, or delete the launcher it names. |
| `doctor` says `workboard` on `PATH` is a different installation | You have two installations. Uninstall one, or reorder `PATH`. |
| `port 7891 is in use by another program` | Stop that program, or choose another port with `workboard serve --port N`, the `WORKBOARD_PORT` environment variable or `port` in `~/.workboard/config.json` (the background service reads the file). |
| Browser shows an old version after an upgrade | `workboard service restart`. `doctor` and `service status` report a version mismatch. |
| The browser or API fails with `unsupported board schemaVersion 5` | A 0.3.x server is still running and can't read boards 0.4.0 migrated. Run `workboard service restart`; if it names another executable, run `workboard service install`, then `workboard service restart`. Any 0.3.x CLI left on `PATH` fails the same way (`doctor` warns `path-other-install`). |
| The server doesn't start at login | Run `workboard service status`, read `~/.workboard/logs/server.log`, then `workboard service install` again. Run `workboard serve` in a terminal to see errors directly. |
| An agent doesn't use the board | `workboard skills status`, then restart the agent session. Check that the agent runs inside the project, a subfolder or a worktree of it, or passes `--board NAME`. |
| Codex can't write the board (permission denied or read-only file system) | Run `workboard setup`, or add the snippet from [Codex](#codex), then restart the Codex session. `workboard doctor` warns with `codex-sandbox` while the grant is missing. |
| `no board for <dir>` | Run the command inside a project that has a board, pass `--board NAME`, or create one with `workboard init`. If the project moved, run `workboard link NAME` in its new location; `workboard boards` lists the names. |
| `doctor` warns `project-missing` | The linked project folder moved or was deleted. Run `workboard link NAME` in its new location. |
| `error [lock]` | Other writers kept the board's store busy for 30 s, or the registry lock (`init`, `link`, board deletion) for 5 s. Nothing was written; run the command again. |
| `doctor` warns `home-not-local` | `WORKBOARD_HOME` is on a network drive, a network filesystem or in a synced folder such as OneDrive or Dropbox. SQLite's write-ahead log needs a local disk, so boards there can be corrupted or lose writes. Stop your agents and the server (`workboard service remove`), move `~/.workboard` to a local folder, set `WORKBOARD_HOME` to it, then run `workboard setup`. |
| `… holds a full board document next to board.db` | Someone restored `board.json` by hand while `board.db` still exists, and WorkBoard never merges the two. Move `board.json` aside to keep `board.db`, or move `board.db` (with `board.db-wal` and `board.db-shm`) aside to migrate `board.json` again. |
| `error [scope]` | A write would cross a symbolic link or junction, for example when `~/.workboard` is a link. Set `WORKBOARD_HOME` to the real folder instead. |
| `error [invalid]` naming `config.json` | Fix or delete `~/.workboard/config.json`. It may only hold `port` (1–65535) and `actor`. See the [README](../README.md#configuration). |
| `doctor` warns about legacy files | They are left over from the pre-release per-board viewer and are no longer used. Delete them after stopping any old viewer process. |
| A board shows as missing | Its folder under `~/.workboard/boards/` was moved or deleted. Put it back, or delete the stale entry from the board chooser. |
