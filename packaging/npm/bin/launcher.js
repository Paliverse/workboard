// Windows global installs, in <prefix> next to npm's shims:
// - copy the platform package's launcher to workboard.exe and wb.exe. Shells and CreateProcess callers then run a
//   real exe, never cmd.exe, which mangles quotes, %, ^ and &, and which Rust and Node refuse to start with line
//   breaks in arguments;
// - rewrite npm's extensionless sh shims (Git Bash, MSYS, omp's bash) to exec the binary directly instead of
//   `node bin/workboard.js`, which costs about 100 ms per call.
// Runs as postinstall and again on every Windows run of bin/workboard.js (covers --ignore-scripts, Bun and pnpm).
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const SCRIPT = "node_modules/@paliverse/workboard/bin/workboard.js";

// npm's sh shim with a fast path in front; exe is relative to the shim's folder. $EPOCHREALTIME (bash) stamps the
// launch for the usage log in FILETIME ticks, as launcher.cs does. npm keeps treating the file as its own shim
// (and restores its original on reinstall) because the "$basedir/<script>" "$@" line still points into the package.
function shim(exe) {
  return `#!/bin/sh
# @paliverse/workboard (bin/launcher.js): runs the binary directly; npm's node shim below is the fallback.
exe="\${0%/*}/${exe}"
if [ -x "$exe" ]; then
  t=$EPOCHREALTIME
  case $t in [0-9]*[.,][0-9][0-9][0-9][0-9][0-9][0-9])
    export WORKBOARD_T0=$(( (\${t%[.,]*} + 11644473600) * 10000000 + (1\${t#*[.,]} - 1000000) * 10 )) ;;
  esac
  exec "$exe" "$@"
fi

basedir=$(dirname "$(echo "$0" | sed -e 's,\\\\,/,g')")

case \`uname\` in
    *CYGWIN*|*MINGW*|*MSYS*)
        if command -v cygpath > /dev/null 2>&1; then
            basedir=\`cygpath -w "$basedir"\`
        fi
    ;;
esac

if [ -x "$basedir/node" ]; then
  exec "$basedir/node"  "$basedir/${SCRIPT}" "$@"
else
  exec node  "$basedir/${SCRIPT}" "$@"
fi
`;
}

function read(file) {
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    return "";
  }
}

// appDir: the platform package's workboard/ folder. Never throws; reports at most one problem through warn.
function installLauncher(appDir, warn) {
  // bin/ -> workboard -> @paliverse -> node_modules -> prefix. Only npm's global layout puts the shims there.
  const prefix = path.resolve(__dirname, "..", "..", "..", "..");
  const text = shim(path.relative(prefix, path.join(appDir, "workboard.exe")).replaceAll("\\", "/"));
  const names = ["workboard", "wb"];
  // npm restores its sh shims on every (re)install, so shims that are already ours mean this ran since the last
  // install: later runs of bin/workboard.js stop here, without the launcher byte compares.
  if (names.every((name) => read(path.join(prefix, name)) === text && fs.existsSync(path.join(prefix, `${name}.exe`)))) {
    return;
  }
  const source = path.join(appDir, "_internal", "workboard-launcher.exe");
  if (!fs.existsSync(source) || !fs.existsSync(path.join(prefix, "workboard.cmd"))) return;
  let warned = false;
  const fail = (dest, error) => {
    if (!warned) warn(`workboard: could not install ${dest} (npm's shims still work): ${error.message}`);
    warned = true;
  };
  for (const name of names) {
    const dest = path.join(prefix, `${name}.exe`);
    const old = `${dest}.old`;
    try {
      fs.rmSync(old, { force: true });
    } catch {} // still running from the last update; the next run removes it
    try {
      if (!fs.existsSync(dest) || !fs.readFileSync(dest).equals(fs.readFileSync(source))) {
        try {
          fs.copyFileSync(source, dest);
        } catch {
          fs.renameSync(dest, old); // a running exe can be renamed, not overwritten
          fs.copyFileSync(source, dest);
        }
      }
    } catch (error) {
      fail(dest, error);
    }
    // Only npm's shim for this package (or an older one of ours): never another package's `wb`.
    const sh = path.join(prefix, name);
    const current = read(sh);
    if (current === text || !current.includes(`"$basedir/${SCRIPT}" "$@"`)) continue;
    try {
      fs.writeFileSync(`${sh}.tmp`, text);
      fs.renameSync(`${sh}.tmp`, sh); // replace, don't rewrite: a running sh may still be reading the old file
    } catch (error) {
      fs.rmSync(`${sh}.tmp`, { force: true });
      fail(sh, error);
    }
  }
}

module.exports = { installLauncher };

if (require.main === module && process.platform === "win32") {
  let appDir;
  try {
    appDir = path.join(path.dirname(require.resolve(`@paliverse/workboard-win32-${process.arch}/package.json`)), "workboard");
  } catch {
    process.exit(0); // no platform package: bin/workboard.js explains that when it runs
  }
  installLauncher(appDir, console.warn);
}
