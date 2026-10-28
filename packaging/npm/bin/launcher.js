// Windows global installs: copy the platform package's launcher to <prefix>\workboard.exe and wb.exe, next to
// npm's .cmd shims. Shells and CreateProcess callers then run a real exe, never cmd.exe, which mangles quotes,
// %, ^ and &, and which Rust and Node refuse to start with line breaks in arguments. Runs as postinstall and
// again on every Windows run of bin/workboard.js (covers --ignore-scripts, Bun and pnpm).
"use strict";

const fs = require("node:fs");
const path = require("node:path");

// appDir: the platform package's workboard/ folder. Never throws; reports at most one problem through warn.
function installLauncher(appDir, warn) {
  const source = path.join(appDir, "_internal", "workboard-launcher.exe");
  // bin/ -> workboard -> @paliverse -> node_modules -> prefix. Only npm's global layout puts the shims there.
  const prefix = path.resolve(__dirname, "..", "..", "..", "..");
  if (!fs.existsSync(source) || !fs.existsSync(path.join(prefix, "workboard.cmd"))) return;
  let warned = false;
  for (const name of ["workboard.exe", "wb.exe"]) {
    const dest = path.join(prefix, name);
    const old = `${dest}.old`;
    try {
      fs.rmSync(old, { force: true });
    } catch {} // still running from the last update; the next run removes it
    try {
      if (fs.existsSync(dest) && fs.readFileSync(dest).equals(fs.readFileSync(source))) continue;
      try {
        fs.copyFileSync(source, dest);
      } catch {
        fs.renameSync(dest, old); // a running exe can be renamed, not overwritten
        fs.copyFileSync(source, dest);
      }
    } catch (error) {
      if (!warned) warn(`workboard: could not install ${dest} (the .cmd shim still works): ${error.message}`);
      warned = true;
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
