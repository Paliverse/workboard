# Using WorkBoard with coding agents

Agents use the `workboard` CLI on the board of the project they are working in. They don't need the server or a browser. You watch and edit the same board at `http://127.0.0.1:7891/`.

## Finding the board

Agents never pass a path. Every board lives in `~/.workboard/boards/` and is linked to a project folder, and `workboard` finds the board linked to the current directory, its nearest linked parent, or the main checkout of a git worktree. A project without a board gets one with `workboard init`; any other board is selected with `--board NAME` or `WORKBOARD_DEFAULT_BOARD`. `workboard which` shows the board a directory resolves to. See [Board resolution](cli.md#board-resolution).

Codex's default sandbox only lets commands write inside the workspace. `workboard setup` adds `~/.workboard` to Codex's writable folders (see [install.md](install.md#codex)); any other harness that sandboxes file writes needs the same access.

## Install the skill

```sh
workboard skills install     # or: workboard setup (skill + background server)
workboard skills status      # installed / current / stale / missing, per target
```

| Harness | Reads |
|---|---|
| Codex | `~/.agents/skills/workboard/SKILL.md` |
| Pi | `~/.agents/skills/workboard/SKILL.md` |
| Oh My Pi | `~/.agents/skills/workboard/SKILL.md` |
| Gemini CLI | `~/.agents/skills/workboard/SKILL.md` |
| Cursor | `~/.agents/skills/workboard/SKILL.md` |
| OpenCode | `~/.agents/skills/workboard/SKILL.md` |
| GitHub Copilot | `~/.agents/skills/workboard/SKILL.md` |
| Claude Code | `~/.claude/skills/workboard/SKILL.md` |

On Windows, `~` is `%USERPROFILE%`. Start a new agent session, or restart the running one, after installing or upgrading. Sessions load skills when they start.

- `workboard upgrade` runs `workboard skills install --refresh`, which rewrites only the copies that already exist.
- `workboard skills remove` deletes only skill directories whose `SKILL.md` declares `name: workboard`.

The skill is a single portable file. It calls the bare `workboard` command, contains no paths to your installation, and works in any shell. If your harness reads skills from somewhere else, copy the file there or point your project instructions at it. To make a project's agents use the board even without skills, add a line to its `AGENTS.md` or `CLAUDE.md`:

```text
Track substantive work on the WorkBoard: follow the workboard skill, starting with `workboard --actor <your label> digest` unless you were handed a delegated subtask.
```

## Actors

Every write records an actor label. The effective label is `--actor NAME`, else `WORKBOARD_ACTOR`, else `actor` in `~/.workboard/config.json`. With none of them, a command that writes a card fails with `invalid` instead of recording an anonymous `agent`; reads still work. The browser records `user` unless you change the label in the sidebar, so agents and people in the web UI stay distinguishable.

- Give each concurrent agent a distinct label, such as `codex-auth` or `claude-docs`. Ownership, the digest's `MINE @actor` section and `query --mine` all depend on it.
- Pass `--actor` on every command, including reads.
- Labels are attribution, not authentication. Any local process can write any label. A parent card still has one accountable holder; a delegated subtask may have a different owner.

## JSON requests for agents

Prefer `workboard --actor YOUR_LABEL call` for requests carrying text: notes, comments, write-ups, results and reasons. Send JSON on stdin, not as an argument, with a quoted heredoc: `workboard --actor YOUR_LABEL call <<'EOF'`, one JSON request per line, then `EOF`. Don't `echo` JSON into `call`: shells strip its double quotes (bash brace-expands `{"op":"context","card":1}` into `op:context card:1`), and `call` refuses it as invalid JSON. This avoids shell quoting and Windows npm shim truncation, works without a server, and runs multiple requests in one CLI process.

Use a verb or dotted action as `op`, `card` for a card reference, and the command's long option names in camelCase or kebab-case (`expectedRev` or `expected-rev`). Text positionals use `text`; subtask actions use `subtask` or `subtasks`. Per-request `actor` and `board` override the invocation defaults. For example, feed this object to `workboard --actor codex-auth call`:

```json
{"op":"note","card":12,"summary":"Migration written; 42/42 tests pass","body":"Evidence:\n- src/migration.py\n- Tests: 42/42"}
```

Input may be a stream of objects (one per line or pretty-printed) or an array. Each request prints one compact `--json` result line; commands run independently and later requests still run after an error. Exit 0 means all succeeded, 1 means at least one failed. No stdin-text flags inside a request: put text directly in `body`, `writeup`, `reason`, `result` or `text`. See [JSON requests](cli.md#json-requests) for all positional keys, value types and available operations.

## The protocol

The [WorkBoard skill](../src/workboard/skills/workboard/SKILL.md) is the protocol agents follow: the loop from `digest` to `done`, notes, revision guards, delegating subtasks to workers and the worker's side of an assignment, and what each refusal means. `skills install` puts that same file where each harness reads it, so this page doesn't repeat it. Every command's options, output and error codes are in the [CLI reference](cli.md), and the rules for delegated work in [Delegated subtasks](cli.md#delegated-subtasks).

The first delegated subtask (or `ack`) moves a board to schema 4, which WorkBoard 0.1.x can't read or write, so update every installation that uses the board first (see [Upgrading to 0.2.0](install.md#upgrading-to-020)).

## Machine-readable output

With `--json`, every command prints one JSON line. Mutations include `ok`, `action`, `num`, `id`, `column`, `rev` and `actor`, plus the created or changed `item`/`items` (subtasks, comments, attachments, note entries) with their ids. Failures print `{"ok": false, "status", "code", "error", "rev"}` and exit 1. See the [CLI reference](cli.md) for every command.

- **Batch in one `call`.** A `call` process keeps the parsed board between requests and parses it again only after a commit. A script that sends its reads and writes through one long-running `call` skips a process start and a full board read per command; on a 790-card board a `context` request took about 7 ms inside `call` and about 190 ms as a separate command.
- **Read many cards at once.** `query --json --fields num,title,column,owner,tags,changedRev` returns owners, tags and change revisions for every card in one read, so a script doesn't need one `context` per card. Its `rev` guards each of those cards (see [Revision guards](cli.md#revision-guards)).
- **Filter locally.** To match many keywords against titles or tags, filter one `query --json` result in your script instead of running one `search` per keyword; each `search` scans the whole board again.
- **Windows scripts.** npm's global install puts `workboard.exe` and `wb.exe` launchers next to its `workboard.cmd` and `wb.cmd` shims (see [npm](install.md#npm-windows-macos-linux)), so shells and Python's `subprocess.run(["workboard", …])` start a real executable and arguments arrive intact, line breaks included. Older npm installs, other package managers and a prefix the launcher couldn't be copied to have only the shim, and every argument to a `.cmd` file passes through `cmd.exe`: a line break silently ends the command (the rest of that text and every later argument, including `--json`, are lost), `%NAME%` expands, `^` can disappear and `"` plus `&` can start another command. Rust programs refuse to start a `.cmd` with a line break in an argument (`batch file arguments are invalid`), and Node and Bun refuse arguments that hold `"`, `&` or `|`. If `where workboard` lists `workboard.cmd` first, run `workboard version --json` once and spawn the `executable` it reports (the real `workboard.exe`) with list arguments. Either way, send long text through `call` (JSON on stdin) or the `--stdin`/`--*-stdin` options, which never pass through a command line.
