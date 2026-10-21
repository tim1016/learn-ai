"use strict";

const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");

// CI shards this same suite across 6 jobs (.github/workflows/ci.yml) by
// setting TEST_SHARD_INDEX/TEST_SHARD_COUNT and NG_BUILD_MAX_WORKERS=2, then
// passing --runner-config=vitest.ci.config.ts, which refuses to load without
// both shard vars set. Select the shard's specs before Angular compiles them;
// Vitest then runs that exact selection without dealing it a second time.
// Mirror that locally instead of reinventing it: a dev
// who exports TEST_SHARD_INDEX/TEST_SHARD_COUNT before `npm test` gets the
// same sharded config auto-appended, so one shard's worth of tests fits the
// two-minute target instead of the full unsharded suite OOMing the 6G
// container. NG_BUILD_MAX_WORKERS defaults the same way CI pins it, so even a
// caller who runs the unsharded suite isn't left with unbounded worker
// concurrency.
//
// A shard runs the specs only. The guard scripts check the tree, not a shard
// of it, so CI runs them once, in its Frontend Lint job, and not six times
// over (#2882). The unsharded `npm test` still runs them first.
//
// A CI job is sized to finish inside the target (#2873). Time never fails a
// run (owner, 2026-10-20): past the target the run keeps its own exit code and
// warns, on stderr, in the step summary and as a GitHub annotation. That
// shard is the next one to trim.
const TEST_TARGET_MS = 120_000;
const DEFAULT_MAX_WORKERS = "2";
const CI_RUNNER_CONFIG_ARG = "--runner-config=vitest.ci.config.ts";
const GUARDS_THEN_SPECS_SCRIPT = "test:unbounded";
const SPECS_ONLY_SCRIPT = "test:specs";

// The frontend-test-shard job records its own start here as its first step,
// so checkout, Node setup and `npm ci` spend the same target the tests do. Without it (a local run) both are measured from this run's start.
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
// that drifts past its target is visible. A shard past it also raises a
// GitHub warning annotation, which shows on the run without opening the log.
// The line says what was measured: the job when its start is known, with the
// tests' own share beside it, and the tests alone otherwise.
function reportElapsed(run) {
  const { startedAtMs, jobStartedAtMs, subject, shardLabel, targetMs } = run;
  const nowMs = Date.now();
  const seconds = (ms) => (ms / 1000).toFixed(1);
  const measuredMs = nowMs - (jobStartedAtMs ?? startedAtMs);
  const overTarget = measuredMs > targetMs;
  if (overTarget) {
    process.stderr.write(
      `${subject} took longer than the ${targetMs / 1000}-second target. ` +
        "That does not fail the run. Trim this shard: move expensive coverage to the daily suite " +
        "or rebalance the shards.\n",
    );
    if (process.env.GITHUB_ACTIONS === "true") {
      // A workflow command is read from stdout; the lines above go to stderr.
      process.stdout.write(
        `::warning title=Frontend tests${shardLabel} are over their ${targetMs / 1000}-second target::` +
          `${jobStartedAtMs === null ? "The run" : "The job"} took ${seconds(measuredMs)}s. ` +
          "This does not fail the run. To trim it, move expensive coverage to the daily suite " +
          "or rebalance the shards.\n",
      );
    }
  }
  const verdict = overTarget ? " (over target)" : "";
  const ofTheTarget = `of the ${targetMs / 1000}s target`;
  const measured =
    jobStartedAtMs === null
      ? `${seconds(measuredMs)}s ${ofTheTarget}`
      : `job ${seconds(measuredMs)}s ${ofTheTarget}, tests ${seconds(nowMs - startedAtMs)}s`;
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

// The target is a parameter only so the guard
// (verify-run-test-budget-shard-guard.cjs) can cross it in milliseconds;
// `npm test` always runs with the constant above.
function runTestBudget({
  forwardedArgs = process.argv.slice(2),
  targetMs = TEST_TARGET_MS,
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
  };

  const hasRunnerConfigArg = forwardedArgs.some((arg) =>
    arg.startsWith("--runner-config="),
  );
  const testArgs = [...forwardedArgs];
  if (isShardRequested && !hasRunnerConfigArg) testArgs.push(CI_RUNNER_CONFIG_ARG);
  const preselected = isShardRequested && testArgs.includes(CI_RUNNER_CONFIG_ARG) &&
    !testArgs.some((arg) => arg.startsWith("--include"));
  if (preselected) {
    try {
      const selected = JSON.parse(execFileSync(process.execPath, [
        "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "scripts/verify-ci-shard-balance-guard.cjs", "--list",
      ], { encoding: "utf8", env: { ...process.env, CI_SHARD_PRESELECTED: "0" } }));
      if (!Array.isArray(selected) || selected.length === 0 || !selected.every((path) =>
        typeof path === "string" && path.startsWith("src/") && path.endsWith(".spec.ts"))) {
        throw new Error("the selected shard has an invalid spec list");
      }
      testArgs.push(...selected.map((path) => `--include=${path}`));
    } catch (error) {
      process.stderr.write(`Unable to select frontend shard specs: ${error.message}\n`);
      process.exitCode = 1;
      return;
    }
  }

  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const script = isShardRequested ? SPECS_ONLY_SCRIPT : GUARDS_THEN_SPECS_SCRIPT;
  const child = spawn(npm, ["run", script, "--", ...testArgs], {
    stdio: "inherit",
    env: {
      ...process.env,
      NG_BUILD_MAX_WORKERS: process.env.NG_BUILD_MAX_WORKERS ?? DEFAULT_MAX_WORKERS,
      CI_SHARD_PRESELECTED: preselected ? "1" : "0",
    },
  });

  child.on("error", (error) => {
    process.stderr.write(`Unable to start the frontend tests: ${error.message}\n`);
    process.exitCode = 1;
  });

  child.on("exit", (code, signal) => {
    reportElapsed(run);
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
