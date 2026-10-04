"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");

// CI shards this same suite across 6 jobs (.github/workflows/ci.yml) by
// setting TEST_SHARD_INDEX/TEST_SHARD_COUNT and NG_BUILD_MAX_WORKERS=2, then
// passing --runner-config=vitest.ci.config.ts, which refuses to load without
// both shard vars set. Mirror that locally instead of reinventing it: a dev
// who exports TEST_SHARD_INDEX/TEST_SHARD_COUNT before `npm test` gets the
// same sharded config auto-appended, so one shard's worth of tests fits the
// two-minute target instead of the full unsharded suite OOMing the 6G
// container. NG_BUILD_MAX_WORKERS defaults the same way CI pins it, so even a
// caller who runs the unsharded suite isn't left with unbounded worker
// concurrency.
//
// A CI job is sized to finish inside the target and fails only past the
// limit (#2873). Past the target the run keeps its own exit code and says so;
// past the limit it is killed and exits 124.
const TEST_TARGET_MS = 120_000;
const TEST_LIMIT_MS = 180_000;
const DEFAULT_MAX_WORKERS = "2";
const CI_RUNNER_CONFIG_ARG = "--runner-config=vitest.ci.config.ts";

// The frontend-test-shard job records its own start here as its first step,
// so checkout, Node setup and `npm ci` spend the same target and limit the
// tests do. Without it (a local run) both are measured from this run's start.
// GitHub ends a hosted job at six hours, so an older value is not this job's.
const JOB_STARTED_AT_ENV = "CI_JOB_STARTED_AT_MS";
const MAX_JOB_AGE_MS = 6 * 60 * 60 * 1000;

// Why `raw` cannot be this job's start, or null when it can.
function jobStartProblem(raw, nowMs) {
  if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    return "is not a whole number of milliseconds";
  }
  if (Number(raw) > nowMs) return "is in the future";
  if (nowMs - Number(raw) > MAX_JOB_AGE_MS) return "is more than six hours old";
  return null;
}

// The job's start, or null when none was recorded. A recorded value that
// cannot be this job's start is named on stderr and ignored: a bad timestamp
// costs the measurement the job's earlier steps, never the run its tests.
function readJobStartedAtMs(nowMs) {
  const raw = process.env[JOB_STARTED_AT_ENV];
  if (raw === undefined) return null;
  const problem = jobStartProblem(raw, nowMs);
  if (problem === null) return Number(raw);
  process.stderr.write(
    `${JOB_STARTED_AT_ENV}=${JSON.stringify(raw)} ${problem}, so the job's earlier ` +
      "steps are not counted: timing the frontend tests from their own start.\n",
  );
  return null;
}

// Each shard prints its own time (#2632 item 3), to stderr for local runs
// and to the CI step summary when GitHub Actions sets the path, so a shard
// that drifts past its target is visible before it reaches the limit. The
// line says what was measured: the job when its start is known, with the
// tests' own share beside it, and the tests alone otherwise.
function reportElapsed(run, exceededLimit) {
  const { startedAtMs, jobStartedAtMs, subject, shardLabel, targetMs, limitMs } = run;
  const nowMs = Date.now();
  const seconds = (ms) => (ms / 1000).toFixed(1);
  const measuredMs = nowMs - (jobStartedAtMs ?? startedAtMs);
  const overTarget = !exceededLimit && measuredMs > targetMs;
  if (overTarget) {
    process.stderr.write(
      `${subject} took longer than the ${targetMs / 1000}-second target. ` +
        `That alone does not fail the run; only the ${limitMs / 1000}-second limit does. ` +
        "Rebalance the shards or move expensive coverage to the daily suite.\n",
    );
  }
  const verdict = exceededLimit ? " (limit exceeded)" : overTarget ? " (over target)" : "";
  const ofTheLimits = `of the ${targetMs / 1000}s target (${limitMs / 1000}s limit)`;
  const measured =
    jobStartedAtMs === null
      ? `${seconds(measuredMs)}s ${ofTheLimits}`
      : `job ${seconds(measuredMs)}s ${ofTheLimits}, tests ${seconds(nowMs - startedAtMs)}s`;
  const message = `Frontend tests${shardLabel}: ${measured}${verdict}`;
  process.stderr.write(`${message}\n`);
  if (process.env.GITHUB_STEP_SUMMARY) {
    // The time is a diagnostic; it must never change the run's exit code.
    try {
      fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${message}\n`);
    } catch (error) {
      process.stderr.write(
        `Unable to append the test time to GITHUB_STEP_SUMMARY: ${error.message}\n`,
      );
    }
  }
}

// The limits are parameters only so the guard
// (verify-run-test-budget-shard-guard.cjs) can cross them in milliseconds;
// `npm test` always runs with the two constants above.
function runTestBudget({
  forwardedArgs = process.argv.slice(2),
  targetMs = TEST_TARGET_MS,
  limitMs = TEST_LIMIT_MS,
} = {}) {
  const startedAtMs = Date.now();
  const shardIndexSet = Boolean(process.env.TEST_SHARD_INDEX);
  const shardCountSet = Boolean(process.env.TEST_SHARD_COUNT);
  if (shardIndexSet !== shardCountSet) {
    process.stderr.write(
      "TEST_SHARD_INDEX and TEST_SHARD_COUNT must both be set (or neither) to run one CI shard locally.\n",
    );
    process.exit(1);
  }
  const isShardRequested = shardIndexSet && shardCountSet;
  const shardLabel = isShardRequested
    ? ` shard ${process.env.TEST_SHARD_INDEX}/${process.env.TEST_SHARD_COUNT}`
    : "";
  const jobStartedAtMs = readJobStartedAtMs(startedAtMs);
  const run = {
    startedAtMs,
    jobStartedAtMs,
    subject:
      jobStartedAtMs === null
        ? `Frontend tests${shardLabel}`
        : `The CI job for frontend tests${shardLabel}`,
    shardLabel,
    targetMs,
    limitMs,
  };
  const exceededTheLimit =
    `${run.subject} exceeded the hard ${limitMs / 1000}-second limit ` +
    `(the target is ${targetMs / 1000} seconds)`;

  // What the limit leaves the tests once the job's earlier steps are counted.
  const allowanceMs = limitMs - (startedAtMs - (jobStartedAtMs ?? startedAtMs));
  if (allowanceMs <= 0) {
    process.stderr.write(
      `${exceededTheLimit} before the tests started, so they were not run. ` +
        "The job's earlier steps took too long.\n",
    );
    reportElapsed(run, true);
    process.exitCode = 124;
    return;
  }

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

  let exceededLimit = false;
  const timer = setTimeout(() => {
    exceededLimit = true;
    process.stderr.write(
      `${exceededTheLimit}. Move expensive coverage to the daily suite or make it faster.\n`,
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
  }, allowanceMs);

  child.on("error", (error) => {
    clearTimeout(timer);
    process.stderr.write(`Unable to start the frontend tests: ${error.message}\n`);
    process.exitCode = 1;
  });

  child.on("exit", (code, signal) => {
    clearTimeout(timer);
    reportElapsed(run, exceededLimit);
    if (exceededLimit) {
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
}

module.exports = { runTestBudget };

if (require.main === module) {
  runTestBudget();
}
