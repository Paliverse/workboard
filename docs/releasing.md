# Releasing WorkBoard

A release is one annotated tag, `vX.Y.Z`. Pushing the tag runs [`.github/workflows/release.yml`](../.github/workflows/release.yml), which builds, verifies and publishes every channel from the same commit. Maintainers never build or upload artifacts by hand.

## What the release workflow does

| Job | Runs on | Does |
|---|---|---|
| `verify` | `ubuntu-latest` | Fails unless the tag equals `v` + `workboard.__version__` and `CHANGELOG.md` has a `## [X.Y.Z]` section. Runs the full test suite. |
| `binaries` | one native runner per target | Builds the PyInstaller app with `packaging/build.py binary`, then smoke-tests the archive with `packaging/build.py smoke`: `--version`, `init`, `add`, `digest`, `serve --port 0 --json`, `/health`, `/b/<board>/` and token shutdown, all in a scratch home. |
| `python` | `ubuntu-latest` | Builds the sdist and wheel. |
| `github-release` | `ubuntu-latest` | Creates the GitHub Release with the six archives, sdist, wheel, `install.sh`, `install.ps1` and `SHA256SUMS`. The notes are that version's CHANGELOG section. |
| `pypi` | `ubuntu-latest` | Publishes to PyPI through trusted publishing (environment `pypi`). |
| `npm` | `ubuntu-latest` | Builds the seven npm packages from the archives. It publishes the six platform packages first, then `workboard`, all with provenance (environment `npm`). |
| `homebrew` | `macos-15` | Renders `Formula/workboard.rb`, then runs `brew install` and `brew test` on it. Pushes it to `Paliverse/homebrew-workboard`. Skipped without `HOMEBREW_TAP_TOKEN`. |
| `winget` | `windows-latest` | Renders the `Paliverse.WorkBoard` manifests and opens a pull request on `microsoft/winget-pkgs` with `wingetcreate submit`. Skipped without `WINGET_TOKEN`. |
| `scoop` | `ubuntu-latest` | Commits `bucket/workboard.json` to `main`. |

The channel jobs start only after the GitHub Release exists, because every manifest points at its assets. Generated manifests are never committed by hand, and `bucket/workboard.json` exists only after the first release.

Release assets have unversioned names, so `releases/latest/download/<asset>` always works:

| Target | Runner label | Asset |
|---|---|---|
| Windows x64 | `windows-latest` | `workboard-windows-x64.zip` |
| Windows arm64 | `windows-11-arm` | `workboard-windows-arm64.zip` |
| macOS x64 | `macos-15-intel` | `workboard-macos-x64.tar.gz` |
| macOS arm64 | `macos-15` | `workboard-macos-arm64.tar.gz` |
| Linux x64 | `ubuntu-22.04` | `workboard-linux-x64.tar.gz` |
| Linux arm64 | `ubuntu-22.04-arm` | `workboard-linux-arm64.tar.gz` |

The runner labels were checked against GitHub's [hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) on 2026-09-24. The Linux binaries are built on Ubuntu 22.04 so they only need glibc 2.35 or newer. When GitHub retires a label, update the `binaries` matrix and this table. Each archive has a top-level `workboard/` directory that holds the executables, `_internal/` and `LICENSE`.

## One-time setup

Complete these steps before the first tag.

1. **Repository.** Create `github.com/Paliverse/workboard` and push `main`. In *Settings → Actions → General*, allow GitHub Actions. The default `GITHUB_TOKEN` permission can stay read-only, because each job asks for what it needs. If `main` is protected, allow `github-actions[bot]` to push, or the `scoop` job can't commit `bucket/workboard.json`.
2. **Environments.** Create the environments `pypi` and `npm` in *Settings → Environments*. Add required reviewers if you want to approve each publication.
3. **PyPI.** On pypi.org, go to *Your account → Publishing* and add a pending trusted publisher: project `workboard`, owner `Paliverse`, repository `workboard`, workflow `release.yml`, environment `pypi`. The name `workboard` was free on 2026-09-24. No token is needed.
4. **npm names.** The main package `workboard` needs the platform packages `workboard-win32-x64`, `workboard-win32-arm64`, `workboard-darwin-x64`, `workboard-darwin-arm64`, `workboard-linux-x64` and `workboard-linux-arm64` as optional dependencies.
   - **Check the names.** On 2026-09-24 all six platform names were free, and `workboard` itself was fully unpublished (by a previous owner in 2024), so it can be reused. `npm view <name>` must return 404 for each name, or show `Paliverse` under `npm owner ls <name>`.
   - **Claim them promptly.** A platform name taken by someone else would be installed by every `npm install -g workboard`, so claim all seven names with the first release.
   - **First release.** Create a granular npm access token that can publish new packages (bypass 2FA) and store it as the `NPM_TOKEN` secret of the `npm` environment.
   - **After the first release.** Configure trusted publishing on each of the seven packages (*package → Settings → Trusted publishing*: GitHub Actions, `Paliverse/workboard`, workflow `release.yml`, environment `npm`), then delete `NPM_TOKEN`. When no token is present, npm authenticates with the job's OIDC identity. The job uses Node 24, whose npm supports trusted publishing.
5. **Homebrew tap.** Create the public repository `Paliverse/homebrew-workboard` with an initial commit, for example a README, on `main`. Create a fine-grained token with *Contents: read and write* on that repository only, and store it as the repository secret `HOMEBREW_TAP_TOKEN`. Users install with `brew install Paliverse/workboard/workboard`.
6. **winget.** Fork `microsoft/winget-pkgs` into the GitHub account that owns the token. Create a classic personal access token with the `public_repo` scope (see the [wingetcreate token guide](https://aka.ms/winget-create-token)). Store it as the repository secret `WINGET_TOKEN`. winget moderators review the first `Paliverse.WorkBoard` submission by hand; later versions go through automated validation.
7. **Scoop.** No setup. The bucket is this repository: `scoop bucket add workboard https://github.com/Paliverse/workboard`.

### Secrets and environments

| Name | Kind | Used by | When it is missing |
|---|---|---|---|
| `pypi` | environment + PyPI trusted publisher | `pypi` | The PyPI upload fails. |
| `npm` | environment | `npm` | The job can't start. |
| `NPM_TOKEN` | `npm` environment secret | `npm` (first release, or instead of trusted publishing) | npm uses trusted publishing. |
| `HOMEBREW_TAP_TOKEN` | repository secret | `homebrew` | The job is skipped with a notice. |
| `WINGET_TOKEN` | repository secret | `winget` | The job is skipped with a notice. |
| `GITHUB_TOKEN` | automatic | `github-release`, `scoop` | Always present. |

## Cutting a release

1. Make sure CI on `main` is green.
2. Set `__version__` in `src/workboard/__init__.py` to `X.Y.Z`. This is the only version source; `pyproject.toml`, npm and the manifests all read it or the tag.
3. In `CHANGELOG.md`, move the `Unreleased` entries under `## [X.Y.Z] - YYYY-MM-DD` and update the compare links at the bottom.
4. Commit (`Release X.Y.Z`), push, and wait for CI.
5. Tag and push:

   ```sh
   git tag -a vX.Y.Z -m "WorkBoard X.Y.Z"
   git push origin vX.Y.Z
   ```

6. Follow the *Release* workflow run. If the environments have reviewers, approve `pypi` and `npm`.
7. Check the result:
   - the GitHub Release has 6 archives, the sdist, the wheel, both install scripts and `SHA256SUMS`;
   - `pipx run --spec workboard==X.Y.Z workboard --version` and `npx workboard@X.Y.Z --version` print `workboard X.Y.Z`;
   - the tap has a `workboard X.Y.Z` commit, the `winget` job log links the winget-pkgs pull request, and `main` has a `scoop: workboard X.Y.Z` commit;
   - `workboard version --check` on an older install reports the update.

Never move or reuse a published tag. If a release is broken, fix it on `main` and release `X.Y.Z+1`. PyPI and npm refuse to overwrite a published version.

### Re-running a failed release

Use *Re-run failed jobs* on the workflow run. Publishing is safe to repeat:

- PyPI skips files that were already uploaded.
- npm skips packages whose `name@version` already exists.
- Homebrew and Scoop don't commit when the file is unchanged.

`github-release` is the exception: if it failed after creating the release, delete that release (not the tag) before you re-run it.

## Building locally

The build tooling is pinned in `packaging/requirements-build.txt` and is never a runtime dependency. Install it in a throwaway virtual environment under the gitignored `build/`:

```sh
python -m venv build/venv
build/venv/bin/python -m pip install -r packaging/requirements-build.txt   # Windows: build\venv\Scripts\python.exe
build/venv/bin/python packaging/build.py binary          # dist/workboard-<os>-<arch>.{zip,tar.gz}
build/venv/bin/python packaging/build.py smoke dist/workboard-<os>-<arch>.zip
build/venv/bin/python -m build                           # dist/ sdist + wheel
WORKBOARD_TEST_COMMAND="$PWD/build/pyinstaller/dist/workboard/workboard" python -m unittest tests.test_cli
```

`packaging/build.py` has six commands:

- `binary`: runs PyInstaller with `packaging/workboard.spec` and archives the result.
- `smoke PATH`: exercises an archive or an installed `workboard` executable end to end, in a scratch home.
- `checksums [DIR]`: writes `SHA256SUMS`.
- `notes --version X`: prints the CHANGELOG section of that version.
- `npm --version X`: builds `build/npm/` from the archives in `dist/`.
- `manifests --version X --sums SHA256SUMS`: renders the Homebrew formula, the Scoop manifest and the winget manifests into `build/manifests/`.

### Testing the install scripts

`WORKBOARD_INSTALL_BASE_URL` points the install scripts at any http(s) directory that holds the release assets and `SHA256SUMS`. Always test in a scratch home:

- On Windows, set a scratch `LOCALAPPDATA` and pass `-NoModifyPath`, or set `WORKBOARD_NO_MODIFY_PATH=1`, so the real user `PATH` is never touched.
- `install.sh` never edits shell profiles; use a scratch `HOME`.

```sh
python packaging/build.py checksums dist
python -m http.server 8000 --bind 127.0.0.1 --directory dist &
HOME=$(mktemp -d) WORKBOARD_INSTALL_BASE_URL=http://127.0.0.1:8000 sh scripts/install.sh
```

## Where the packaging lives

| Path | Purpose |
|---|---|
| `pyproject.toml` | Hatchling build. The version comes from `src/workboard/__init__.py`, and the wheel ships `web/board.html` and `skills/workboard/SKILL.md`. |
| `packaging/workboard.spec`, `packaging/entry.py` | PyInstaller onedir build. It produces the console `workboard` executable, plus the windowed `workboardw` on Windows for the background service, and bundles the package data at package-relative paths. |
| `packaging/build.py` | The release tool described above (stdlib only). |
| `packaging/npm/` | Template for the main npm package and its `bin/workboard.js` launcher, which runs the platform binary. |
| `scripts/install.sh`, `scripts/install.ps1` | The one-line installers attached to every release. |
| `.github/workflows/ci.yml` | Tests on Ubuntu, macOS and Windows with Python 3.11 to 3.14, a wheel install smoke test, and a PyInstaller build per OS that runs `tests.test_cli` against the binary. |
| `.github/workflows/release.yml` | The release pipeline. Third-party actions are pinned to commit SHAs, and Dependabot keeps them current. |
