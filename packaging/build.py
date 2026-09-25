"""WorkBoard release tool (stdlib only). Run from anywhere; paths are repo-relative.

  binary                          PyInstaller app -> dist/workboard-<os>-<arch>.{zip,tar.gz}
  smoke PATH                      run a release archive (or an installed executable) end to end
                                  in a scratch home: --version, init, add, digest, serve + HTTP
  checksums [DIR]                 DIR/SHA256SUMS over every file in DIR (default: dist)
  notes --version X               print the CHANGELOG.md section of X (GitHub Release notes)
  npm --version X                 build/npm/: the `workboard` package + one package per archive in dist/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
PYINSTALLER = BUILD / "pyinstaller"
# Release target -> (npm os, npm cpu). Asset names are unversioned so releases/latest/download works.
TARGETS = {
    "windows-x64": ("win32", "x64"), "windows-arm64": ("win32", "arm64"),
    "macos-x64": ("darwin", "x64"), "macos-arm64": ("darwin", "arm64"),
    "linux-x64": ("linux", "x64"), "linux-arm64": ("linux", "arm64"),
}


def asset(target: str) -> str:
    return f"workboard-{target}." + ("zip" if target.startswith("windows") else "tar.gz")


def current_target() -> str:
    system = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(sys.platform)
    machine = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if not system or not machine:
        raise SystemExit(f"unsupported build platform: {sys.platform} {platform.machine()}")
    return f"{system}-{machine}"


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def extract(archive: Path, dest: Path) -> Path:
    """Unpack a release archive into dest and return its top-level workboard/ dir."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    return dest / "workboard"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def binary(args) -> None:
    target = current_target()
    app = PYINSTALLER / "dist" / "workboard"
    # PYINSTALLER_CONFIG_DIR keeps PyInstaller's cache in build/ so --clean never touches user dirs.
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(app.parent), "--workpath", str(PYINSTALLER / "work"),
                    str(ROOT / "packaging" / "workboard.spec")],
                   check=True, env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(PYINSTALLER / "cache")})
    shutil.copy2(ROOT / "LICENSE", app / "LICENSE")
    DIST.mkdir(exist_ok=True)
    out = DIST / asset(target)
    if out.suffix == ".zip":
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(app.rglob("*")):
                zf.write(path, Path("workboard", path.relative_to(app)).as_posix())
    else:
        def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
            # In place: TarInfo.replace() deep-copies the entry, including the open archive, and fails.
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info
        with tarfile.open(out, "w:gz") as tf:
            tf.add(app, "workboard", filter=normalize)
    print(out)


def smoke(args) -> None:
    sys.path.insert(0, str(ROOT))
    from tests.support import make_env  # the scratch-home environment every test uses

    version = package_version()
    with tempfile.TemporaryDirectory(prefix="wb-smoke-") as tmp:
        tmp = Path(tmp)
        target = Path(args.path).resolve()
        if target.name.endswith((".zip", ".tar.gz")):
            target = extract(target, tmp / "app") / ("workboard.exe" if target.suffix == ".zip" else "workboard")
        project = tmp / "project"
        project.mkdir()
        env = make_env(tmp / "home")
        env.pop("PYTHONPATH", None)  # exercise the packaged code only
        (tmp / "home").mkdir()

        def cli(*argv: str) -> str:
            proc = subprocess.run([str(target), *argv], cwd=project, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            print(f"$ workboard {' '.join(argv)}  [exit {proc.returncode}]\n{proc.stdout}{proc.stderr}".rstrip())
            if proc.returncode:
                raise SystemExit(f"smoke failed: workboard {' '.join(argv)} exited {proc.returncode}")
            return proc.stdout

        if cli("--version").strip() != f"workboard {version}":
            raise SystemExit(f"smoke failed: --version does not report workboard {version}")
        cli("init", "smoke")
        cli("add", "--title", "Smoke card")
        cli("digest")
        serve_smoke(target, project, env, tmp / "serve.log")
    print(f"smoke ok: {args.path}")


def serve_smoke(exe: Path, project: Path, env: dict, log: Path) -> None:
    """serve --port 0 --json, check /health, /api/boards and the board page, then token-shutdown."""
    state = Path(env["WORKBOARD_HOME"]) / "server.json"
    with log.open("w+", encoding="utf-8") as out:
        proc = subprocess.Popen([str(exe), "serve", "--port", "0", "--json"], cwd=project, env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not state.is_file():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit("smoke failed: server did not start")
                time.sleep(0.2)
            info = json.loads(state.read_text(encoding="utf-8"))
            base = f"http://127.0.0.1:{info['port']}/"

            def get(path: str) -> bytes:
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    return response.read()

            health = json.loads(get("health"))
            print(f"GET /health -> {health}")
            if health.get("app") != "workboard" or health.get("port") != info["port"] or info["port"] == 7891:
                raise SystemExit("smoke failed: unexpected /health response")
            board = json.loads(get("api/boards"))["boards"][0]
            page = get(board["url"].lstrip("/"))
            if page != (ROOT / "src" / "workboard" / "web" / "board.html").read_bytes():
                raise SystemExit(f"smoke failed: {board['url']} did not serve the bundled board.html")
            if b"Smoke card" not in get(board["url"].lstrip("/") + "board.json"):
                raise SystemExit(f"smoke failed: {board['url']}board.json lacks the smoke card")
            print(f"GET {board['url']} -> board.html ({len(page)} bytes); board.json has the smoke card")
            request = urllib.request.Request(base + "api/shutdown", data=b"{}", method="POST", headers={
                "X-WorkBoard-Token": info["token"], "Content-Type": "application/json",
                "Origin": base.rstrip("/")})
            urllib.request.urlopen(request, timeout=10).close()
            proc.wait(timeout=15)
        except BaseException:
            print(f"--- serve output ---\n{log.read_text(encoding='utf-8')}", file=sys.stderr)
            raise
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        out.seek(0)
        started = json.loads(next(line for line in out if line.startswith("{")))
        if not started.get("ok") or started.get("port") != info["port"]:
            raise SystemExit(f"smoke failed: unexpected serve --json line {started}")
        print(f"POST /api/shutdown -> server exited {proc.returncode}")


def checksums(args) -> None:
    folder = Path(args.dir)
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (folder / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files),
                                       encoding="utf-8", newline="\n")
    print(folder / "SHA256SUMS")


def notes(args) -> None:
    version = args.version.removeprefix("v")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |^\[[^\]]+\]:\s|\Z)",
                      changelog, re.M | re.S)
    if not match or not match.group(1).strip():
        raise SystemExit(f"CHANGELOG.md has no '## [{version}]' section")
    print(match.group(1).strip())


def npm(args) -> None:
    version = args.version.removeprefix("v")
    template = json.loads((ROOT / "packaging" / "npm" / "package.json").read_text(encoding="utf-8"))
    shared = {key: template[key] for key in ("license", "author", "homepage", "bugs", "repository")}
    out = BUILD / "npm"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for target, (os_name, cpu) in TARGETS.items():
        archive = DIST / asset(target)
        name = f"workboard-{os_name}-{cpu}"
        if not archive.is_file():
            print(f"skipped {name}: no dist/{archive.name}")
            continue
        package = out / name
        with tempfile.TemporaryDirectory() as tmp:
            # npm pack drops symlinks (PyInstaller keeps some on macOS/Linux): copy their targets instead.
            shutil.copytree(extract(archive, Path(tmp)), package / "workboard", symlinks=False)
        (package / "workboard" / ("workboard.exe" if os_name == "win32" else "workboard")).chmod(0o755)
        write_json(package / "package.json", {
            "name": name, "version": version,
            "description": f"The {os_name}-{cpu} binary for the workboard npm package.",
            **shared, "os": [os_name], "cpu": [cpu], "files": ["workboard/"], "preferUnplugged": True})
        (package / "README.md").write_text(
            f"# {name}\n\nThe prebuilt {os_name}-{cpu} WorkBoard binary. Install "
            f"[`workboard`](https://www.npmjs.com/package/workboard) instead: `npm install -g workboard`.\n",
            encoding="utf-8", newline="\n")
        print(package)
    main = out / "workboard"
    shutil.copytree(ROOT / "packaging" / "npm", main)
    write_json(main / "package.json", {
        "name": template["name"], "version": version, "description": pyproject()["description"],
        **{key: value for key, value in template.items() if key != "name"},
        "optionalDependencies": {f"workboard-{o}-{c}": version for o, c in TARGETS.values()}})
    shutil.copy2(ROOT / "README.md", main / "README.md")
    shutil.copy2(ROOT / "LICENSE", main / "LICENSE")
    print(main)


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def package_version() -> str:
    text = (ROOT / "src" / "workboard" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("binary").set_defaults(fn=binary)
    p = sub.add_parser("smoke")
    p.add_argument("path", help="release archive (.zip/.tar.gz) or workboard executable")
    p.set_defaults(fn=smoke)
    p = sub.add_parser("checksums")
    p.add_argument("dir", nargs="?", default=str(DIST))
    p.set_defaults(fn=checksums)
    for name, fn in (("notes", notes), ("npm", npm)):
        p = sub.add_parser(name)
        p.add_argument("--version", required=True)
        p.set_defaults(fn=fn)
    for stream in (sys.stdout, sys.stderr):  # CI consoles on Windows are cp1252; CLI output is UTF-8.
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
