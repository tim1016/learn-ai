"use strict";

const { spawn } = require("node:child_process");

const TEST_BUDGET_MS = 120_000;
const npm = process.platform === "win32" ? "npm.cmd" : "npm";
const child = spawn(
  npm,
  ["run", "test:unbounded", "--", ...process.argv.slice(2)],
  {
    detached: process.platform !== "win32",
    stdio: "inherit",
  },
);

let exceededBudget = false;
const timer = setTimeout(() => {
  exceededBudget = true;
  process.stderr.write(
    "Frontend tests exceeded the hard 120-second budget. " +
      "Move expensive coverage to the daily suite or make it faster.\n",
  );
  if (process.platform === "win32") {
    child.kill("SIGKILL");
  } else {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
  }
}, TEST_BUDGET_MS);

child.on("error", (error) => {
  clearTimeout(timer);
  process.stderr.write(`Unable to start the frontend tests: ${error.message}\n`);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  clearTimeout(timer);
  if (exceededBudget) {
    process.exitCode = 124;
    return;
  }
  if (signal !== null) {
    process.stderr.write(`Frontend tests ended from signal ${signal}.\n`);
    process.exitCode = 1;
    return;
  }
  process.exitCode = code ?? 1;
});
