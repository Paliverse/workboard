#!/usr/bin/env node
// Runs the prebuilt WorkBoard binary from the matching @paliverse/workboard-<platform>-<arch> package.
"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");

// Scoped: npm's spam filter rejects brand-new unscoped platform names such as workboard-win32-x64.
const platformPackage = `@paliverse/workboard-${process.platform}-${process.arch}`;
let binary;
try {
  binary = path.join(
    path.dirname(require.resolve(`${platformPackage}/package.json`)),
    "workboard",
    process.platform === "win32" ? "workboard.exe" : "workboard",
  );
} catch {
  console.error(
    `workboard: the platform package ${platformPackage} is not installed.\n` +
      "Prebuilt binaries exist for win32, darwin and linux on x64 and arm64. If yours is one of them, reinstall\n" +
      "without skipping optional dependencies (--omit=optional / --no-optional): npm install -g @paliverse/workboard\n" +
      "Other install options: https://github.com/Paliverse/workboard#install",
  );
  process.exit(1);
}

const child = spawn(binary, process.argv.slice(2), { stdio: "inherit", windowsHide: true });

// Ctrl+C reaches the child directly (same console / process group); stay alive so the
// child can shut down cleanly and we can report its exit status.
process.on("SIGINT", () => {});
for (const signal of ["SIGTERM", "SIGHUP"]) {
  process.on(signal, () => child.kill(signal));
}

child.on("error", (error) => {
  console.error(`workboard: could not run ${binary}: ${error.message}`);
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) {
    process.removeAllListeners(signal);
    process.kill(process.pid, signal);
  }
  process.exit(code ?? 1);
});
