## Summary

<!-- What does this change, and why? Link the issue: "Fixes #123". -->

## Tests run

<!-- Paste the commands you ran and their results. -->

- [ ] `python -m unittest discover -s tests -t .`
- [ ] Targeted module(s):

## Browser check (UI changes only)

<!-- Required for any change to src/workboard/web/board.html. -->

- [ ] Exercised the changed gesture or state transition in a real browser against a scratch server: <!-- browser + what you did -->
- [ ] No console errors
- [ ] Not a UI change

## Checklist

- [ ] Standard library only; no new runtime dependencies
- [ ] Invariants kept: five columns, writes through `core` with locking, atomic save and backups, visible 409 conflicts, no server or browser side effects from board commands
- [ ] Tests used scratch homes and ephemeral ports (`tests/support.py`)
- [ ] Docs updated (`README.md`, `docs/`) and a line added under `## [Unreleased]` in `CHANGELOG.md`
- [ ] If `SKILL.md` changed: still 6 KB or less, bare `workboard` command, no absolute paths or shell-specific syntax
