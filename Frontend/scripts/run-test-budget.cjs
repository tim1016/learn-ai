"use strict";

const { spawn } = require("node:child_process");

// CI shards this same suite across 6 jobs (.github/workflows/ci.yml) by
// setting TEST_SHARD_INDEX/TEST_SHARD_COUNT and NG_BUILD_MAX_WORKERS=2, then
// passing --runner-config=vitest.ci.config.ts, which refuses to load without
// both shard vars set. Mirror that locally instead of reinventing it: a dev
// who exports TEST_SHARD_INDEX/TEST_SHARD_COUNT before `npm test` gets the
// same sharded config auto-appended, so one shard's worth of tests fits the
// 120s budget instead of the full unsharded suite OOMing the 6G container.
// NG_BUILD_MAX_WORKERS defaults the same way CI pins it, so even a caller
// who runs the unsharded suite isn't left with unbounded worker concurrency.
const TEST_BUDGET_MS = 120_000;
const DEFAULT_MAX_WORKERS = "2";
const CI_RUNNER_CONFIG_ARG = "--runner-config=vitest.ci.config.ts";

const forwardedArgs = process.argv.slice(2);
const shardIndexSet = Boolean(process.env.TEST_SHARD_INDEX);
const shardCountSet = Boolean(process.env.TEST_SHARD_COUNT);
if (shardIndexSet !== shardCountSet) {
  process.stderr.write(
    "TEST_SHARD_INDEX and TEST_SHARD_COUNT must both be set (or neither) to run one CI shard locally.\n",
  );
  process.exit(1);
}
const isShardRequested = shardIndexSet && shardCountSet;
const hasRunnerConfigArg = forwardedArgs.some((arg) =>
  arg.startsWith("--runner-config="),
);
const testArgs =
  isShardRequested && !hasRunnerConfigArg
    ? [...forwardedArgs, CI_RUNNER_CONFIG_ARG]
    : forwardedArgs;

const npm = process.platform === "win32" ? "npm.cmd" : "npm";
const child = spawn(npm, ["run", "test:unbounded", "--", ...testArgs], {
  detached: process.platform !== "win32",
  stdio: "inherit",
  env: {
    ...process.env,
    NG_BUILD_MAX_WORKERS: process.env.NG_BUILD_MAX_WORKERS ?? DEFAULT_MAX_WORKERS,
  },
});

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
