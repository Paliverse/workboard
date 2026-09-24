# Installing WorkBoard

WorkBoard installs a `workboard` command, plus the short alias `wb` on every channel except winget. The release binaries bundle their own Python runtime. The PyPI package needs Python 3.11 or newer and has no dependencies.

## Channels

### npm

```sh
npm install -g workboard
```

The `workboard` package installs the `workboard` and `wb` commands. They launch a prebuilt binary from a platform package (`workboard-win32-x64`, `workboard-darwin-arm64`, `workboard-linux-x64`, …), which npm selects automatically. Node.js is only used to launch the binary.

### Homebrew (macOS, Linux)

```sh
brew install Paliverse/workboard/workboard
```

This command taps `Paliverse/homebrew-workboard` and installs the binary for your OS and architecture. The formula also has a `service` block. You can use `brew services start workboard` instead of `workboard service install`, but don't use both.

### winget (Windows)

```powershell
winget install Paliverse.WorkBoard
```

This installs the portable binary and exposes the `workboard` command. Open a new terminal afterwards so the updated `PATH` takes effect.

### Scoop (Windows)

```powershell
scoop bucket add workboard https://github.com/Paliverse/workboard
scoop install workboard
```

### uv, pipx or pip (Python 3.11+)

```sh
uv tool install workboard
pipx install workboard
python -m pip install workboard   # into the current environment
```

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

Each archive has a top-level `workboard/` directory. Keep the whole directory together, because the executables need `_internal/` next to them. Put the directory on your `PATH`. The release also includes `SHA256SUMS`, the install scripts, the wheel and the sdist.

### From source

See [CONTRIBUTING.md](../CONTRIBUTING.md#development-setup).

## Setup

```sh
workboard setup
```

`setup` installs the agent skill (see [agents.md](agents.md)) and the background service, then prints one line per action. Use `--no-skills` or `--no-service` to skip either part. Run it again after changing your installation; it is idempotent.

Then, in each project:

```sh
workboard init     # creates board/board.json and registers the board
workboard open     # opens it in the browser
```

## Background service

The service runs `workboard serve --service` at login. It serves every registered board at `http://127.0.0.1:7891/` and writes its output to `~/.workboard/logs/server.log`.

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

- **Windows:** the service starts at login. Binary installs run a private copy from `~/.workboard/runtime/<version>/`, so package managers can replace the installed files while it runs.
- **macOS:** the agent starts at login and is restarted if it crashes.
- **Linux:** the unit restarts on failure. systemd stops user services when you log out unless lingering is enabled (`loginctl enable-linger $USER`). Without a working `systemctl --user` (some containers and WSL setups), `service install` writes `~/.config/autostart/workboard.desktop` and starts the server immediately.

You don't need the service: `workboard serve` runs the same server in the foreground, and `workboard open` starts it in the background when needed. Board commands never need a server.

## Upgrade

```sh
workboard version --check
workboard upgrade --dry-run   # show the plan
workboard upgrade
```

`upgrade` stops the server, runs your channel's upgrade command, refreshes installed skills and restarts the service:

| Channel | Command it runs |
|---|---|
| npm | `npm install -g workboard@latest` |
| Homebrew | `brew upgrade workboard` |
| winget | `winget upgrade --id Paliverse.WorkBoard --exact` |
| Scoop | `scoop update workboard` |
| pipx | `pipx upgrade workboard` |
| uv | `uv tool upgrade workboard` |
| pip | `python -m pip install --upgrade workboard` |
| Install script | the install script again |

A source checkout or an unrecognized installation exits with instructions. Update it yourself, then run `workboard skills install --refresh` and `workboard service restart`.

## Uninstall

First remove what `setup` installed, while `workboard` still exists:

```sh
workboard service remove
workboard skills remove
```

Then remove the program:

| Channel | Command |
|---|---|
| npm | `npm uninstall -g workboard` |
| Homebrew | `brew uninstall workboard` (optionally `brew untap Paliverse/workboard`) |
| winget | `winget uninstall --id Paliverse.WorkBoard` |
| Scoop | `scoop uninstall workboard` (optionally `scoop bucket rm workboard`) |
| uv | `uv tool uninstall workboard` |
| pipx | `pipx uninstall workboard` |
| pip | `python -m pip uninstall workboard` |
| Script (Windows) | Delete `%LOCALAPPDATA%\Programs\WorkBoard\` and remove it from your user `PATH` |
| Script (macOS, Linux) | Delete `~/.local/share/workboard/` (or `$XDG_DATA_HOME/workboard/`) and `~/.local/bin/workboard` and `~/.local/bin/wb` |

Boards stay in their projects (`board/`). Delete `~/.workboard` to remove the registry, logs and runtime copies.

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
| `doctor` says `workboard` on `PATH` is a different installation | You have two installations. Uninstall one, or reorder `PATH`. |
| `port 7891 is in use by another program` | Stop that program, or choose another port with `workboard serve --port N` or the `WORKBOARD_PORT` environment variable. |
| Browser shows an old version after an upgrade | `workboard service restart`. `doctor` and `service status` report a version mismatch. |
| The server doesn't start at login | Run `workboard service status`, read `~/.workboard/logs/server.log`, then `workboard service install` again. Run `workboard serve` in a terminal to see errors directly. |
| An agent doesn't use the board | `workboard skills status`, then restart the agent session. Check that the agent runs inside the project or passes `--board`. |
| `no board found at/above cwd` | Run the command inside the project, pass `--board PATH`, or create a board with `workboard init`. |
| `error [lock]` | Another writer held the board for more than 5 s. Find the stuck process; don't delete `board/.board.lock` while writers are running. |
| `doctor` warns about legacy files | They are left over from the pre-release per-board viewer and are no longer used. Delete them after stopping any old viewer process. |
| A board shows as missing | The project moved or was deleted. Delete the stale entry from the board chooser. If the project moved, run `workboard open` inside it to register it again. |
