const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// scripts/run-test-budget.cjs mirrors .github/workflows/ci.yml's
// frontend-test-shard matrix locally: reject a half-set shard env pair
// instead of silently falling back to the full suite, auto-append
// --runner-config=vitest.ci.config.ts when both shard vars are set (unless
// the caller already passed a --runner-config), preselect that shard's specs
// before Angular compiles them, and run them without
// the guard scripts CI runs once elsewhere (#2882), and default
// NG_BUILD_MAX_WORKERS=2 on the spawned child. It also holds a run to two
// time limits (#2873): past the two-minute target the run keeps its own exit
// code and says so, and past the three-minute limit it is killed and fails.
// In CI both are measured from the job's start, which the workflow records as
// the job's first step; locally, from the wrapper's own start.
// This stubs the "npm" the wrapper spawns so each case runs in milliseconds
// and asserts on the exact argv/env/exit status the wrapper produces.

const wrapperPath = path.resolve(__dirname, 'run-test-budget.cjs');

// The fake records how it was spawned, then runs for FAKE_NPM_RUN_MS and
// leaves `finishedFile` behind, so a case can tell a run that ended on its
// own from one the wrapper killed.
function makeFakeNpm(captureFile, finishedFile) {
  const binDir = fs.mkdtempSync(path.join(os.tmpdir(), 'fake-npm-bin-'));
  const fakeNpmSource = [
    '#!/usr/bin/env node',
    'const fs = require("fs");',
    `fs.writeFileSync(${JSON.stringify(captureFile)}, JSON.stringify({`,
    '  argv: process.argv.slice(2),',
    '  env: {',
    '    NG_BUILD_MAX_WORKERS: process.env.NG_BUILD_MAX_WORKERS ?? null,',
    '    TEST_SHARD_INDEX: process.env.TEST_SHARD_INDEX ?? null,',
    '    TEST_SHARD_COUNT: process.env.TEST_SHARD_COUNT ?? null,',
    '    CI_SHARD_PRESELECTED: process.env.CI_SHARD_PRESELECTED ?? null,',
    '  },',
    '}));',
    'setTimeout(() => {',
    `  fs.writeFileSync(${JSON.stringify(finishedFile)}, "");`,
    '  process.exit(Number(process.env.FAKE_NPM_EXIT_CODE ?? "0"));',
    '}, Number(process.env.FAKE_NPM_RUN_MS ?? "0"));',
    '',
  ].join('\n');
  const npmPath = path.join(binDir, 'npm');
  fs.writeFileSync(npmPath, fakeNpmSource, { mode: 0o755 });
  return binDir;
}

// Vars this guard drives per test case. This guard's own process can inherit
// them: a developer reproducing a CI shard exports the shard pair, and a CI
// job may set any of them at the job level. Strip them from the base env
// before layering on each case's explicit overrides, or a "neither set" case
// would silently inherit the host's shard vars, and its job's start.
const CASE_DRIVEN_ENV_VARS = [
  'TEST_SHARD_INDEX',
  'TEST_SHARD_COUNT',
  'CI_JOB_STARTED_AT_MS',
  'NG_BUILD_MAX_WORKERS',
  'FAKE_NPM_EXIT_CODE',
  'FAKE_NPM_RUN_MS',
  'GITHUB_STEP_SUMMARY',
];

// Runs the real wrapper as a child process with a stubbed "npm" ahead of the
// real one on PATH, so we assert on what the wrapper actually spawns rather
// than on its source text. `limits` swaps the wrapper's two-minute target and
// three-minute limit for ones a case can cross in milliseconds; without it
// the wrapper runs exactly as `npm test` runs it.
function runWrapper({ args = [], env = {}, limits = null } = {}) {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-guard-'));
  const captureFile = path.join(scratch, 'capture.json');
  const finishedFile = path.join(scratch, 'finished');
  const fakeNpmBinDir = makeFakeNpm(captureFile, finishedFile);
  const baseEnv = { ...process.env };
  for (const key of CASE_DRIVEN_ENV_VARS) delete baseEnv[key];
  const nodeArgs =
    limits === null
      ? [wrapperPath, ...args]
      : [
          '-e',
          `require(${JSON.stringify(wrapperPath)}).runTestBudget(${JSON.stringify({ forwardedArgs: args, ...limits })})`,
        ];
  try {
    const result = spawnSync(process.execPath, nodeArgs, {
      cwd: path.resolve(__dirname, '..'),
      encoding: 'utf8',
      env: {
        ...baseEnv,
        ...env,
        PATH: `${fakeNpmBinDir}${path.delimiter}${process.env.PATH}`,
      },
    });
    const capture = fs.existsSync(captureFile)
      ? JSON.parse(fs.readFileSync(captureFile, 'utf8'))
      : null;
    return { result, capture, finished: fs.existsSync(finishedFile) };
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
    fs.rmSync(fakeNpmBinDir, { recursive: true, force: true });
  }
}

// Only one of TEST_SHARD_INDEX/TEST_SHARD_COUNT set: must fail loudly and
// never spawn npm (the pre-fix wrapper had no such guard and would silently
// run the full unsharded suite instead).
{
  const { result, capture } = runWrapper({ env: { TEST_SHARD_INDEX: '1' } });
  assert.equal(result.status, 1, 'a half-set shard env pair must exit 1');
  assert.match(
    result.stderr,
    /TEST_SHARD_INDEX and TEST_SHARD_COUNT must both be set/,
    'a half-set shard env pair must explain itself on stderr',
  );
  assert.equal(capture, null, 'a half-set shard env pair must never spawn npm');
}

{
  const { result, capture } = runWrapper({ env: { TEST_SHARD_COUNT: '6' } });
  assert.equal(result.status, 1, 'a half-set shard env pair (other side) must exit 1');
  assert.equal(capture, null, 'a half-set shard env pair (other side) must never spawn npm');
}

// Both shard vars set: auto-append the CI runner config to the forwarded
// args, and run the specs only. The guards are not a shard's to run.
{
  const { result, capture } = runWrapper({
    env: { TEST_SHARD_INDEX: '1', TEST_SHARD_COUNT: '6', FAKE_NPM_EXIT_CODE: '0' },
  });
  assert.equal(result.status, 0);
  assert.ok(capture, 'both shard vars set must spawn npm');
  assert.deepEqual(capture.argv.slice(0, 4), [
    'run',
    'test:specs',
    '--',
    '--runner-config=vitest.ci.config.ts',
  ]);
  const includes = capture.argv.slice(4);
  assert.ok(includes.length > 0 && includes.every((arg) => arg.startsWith('--include=src/') && arg.endsWith('.spec.ts')));
  assert.equal(new Set(includes).size, includes.length);
  assert.equal(capture.env.CI_SHARD_PRESELECTED, '1');
}

// Both shard vars set but the caller already passed --runner-config=...:
// the wrapper must not append a second one.
{
  const { capture } = runWrapper({
    args: ['--runner-config=custom.config.ts'],
    env: { TEST_SHARD_INDEX: '1', TEST_SHARD_COUNT: '6' },
  });
  assert.ok(capture, 'both shard vars set (custom runner-config) must spawn npm');
  assert.deepEqual(capture.argv, ['run', 'test:specs', '--', '--runner-config=custom.config.ts']);
  assert.equal(capture.env.CI_SHARD_PRESELECTED, '0');
}

// Neither shard var set: forwarded args pass through unchanged, no runner
// config is appended, and the guards run first (the plain, unsharded local run).
{
  const { capture } = runWrapper({ args: ['--coverage'] });
  assert.ok(capture, 'the unsharded run must still spawn npm');
  assert.deepEqual(capture.argv, ['run', 'test:unbounded', '--', '--coverage']);
}

// NG_BUILD_MAX_WORKERS defaults to "2" on the spawned child when the caller
// hasn't set it, and is left alone when the caller has.
{
  const { capture } = runWrapper({});
  assert.ok(capture, 'the default run must spawn npm');
  assert.equal(capture.env.NG_BUILD_MAX_WORKERS, '2');
}

{
  const { capture } = runWrapper({ env: { NG_BUILD_MAX_WORKERS: '4' } });
  assert.ok(capture, 'the caller-overridden run must spawn npm');
  assert.equal(capture.env.NG_BUILD_MAX_WORKERS, '4');
}

// Exit status passes through from the spawned child untouched.
{
  const { result } = runWrapper({ env: { FAKE_NPM_EXIT_CODE: '7' } });
  assert.equal(result.status, 7, "the wrapper's exit code must match the spawned child's");
}

{
  const { result } = runWrapper({ env: { FAKE_NPM_EXIT_CODE: '0' } });
  assert.equal(result.status, 0);
}

// The wrapper reports the run's elapsed time on stderr (#2632 item 3), and
// appends the same line to the CI step summary when GitHub Actions provides
// the path — one line per shard so budget drift is readable from a run's
// summary page without opening each job. A run under its target says nothing
// more than that line, which names both limits `npm test` runs under.
{
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-summary-'));
  const summaryFile = path.join(scratch, 'summary.md');
  try {
    const { result } = runWrapper({
      env: {
        TEST_SHARD_INDEX: '2',
        TEST_SHARD_COUNT: '6',
        GITHUB_STEP_SUMMARY: summaryFile,
      },
    });
    assert.equal(result.status, 0);
    const expected = /^Frontend tests shard 2\/6: \d+(\.\d+)?s of the 120s target \(180s limit\)\n$/;
    assert.match(
      fs.readFileSync(summaryFile, 'utf8'),
      expected,
      'the wrapper must append the shard time to the step summary',
    );
    assert.match(result.stderr, expected, 'a run under its target must print its time and nothing else');
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

// Without a step-summary path (a local dev run) the wrapper still prints
// the time to stderr and exits cleanly.
{
  const { result } = runWrapper({
    env: { TEST_SHARD_INDEX: '3', TEST_SHARD_COUNT: '6' },
  });
  assert.equal(result.status, 0);
  assert.match(
    result.stderr,
    /Frontend tests shard 3\/6: \d+(\.\d+)?s of the 120s target \(180s limit\)\n$/,
  );
}

// A step summary that cannot be written (here: the path is a directory)
// warns on stderr and leaves the child's exit code untouched.
{
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-summary-'));
  try {
    const { result } = runWrapper({
      env: { GITHUB_STEP_SUMMARY: scratch, FAKE_NPM_EXIT_CODE: '3' },
    });
    assert.equal(result.status, 3, 'a failed summary write must not change the exit code');
    assert.match(result.stderr, /Unable to append the test time to GITHUB_STEP_SUMMARY/);
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

// Past the target and inside the limit (#2873): the run is not failed for
// its time. It keeps the child's own exit code, here a failing one (the job
// case below keeps a passing one), and says so on stderr and in the step
// summary.
{
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-summary-'));
  const summaryFile = path.join(scratch, 'summary.md');
  try {
    const { result, finished } = runWrapper({
      limits: { targetMs: 0 },
      env: {
        TEST_SHARD_INDEX: '4',
        TEST_SHARD_COUNT: '6',
        FAKE_NPM_EXIT_CODE: '7',
        GITHUB_STEP_SUMMARY: summaryFile,
      },
    });
    assert.equal(result.status, 7, 'a run past its target must keep the exit code of its tests');
    assert.ok(finished, 'a run past its target must be left to finish');
    const timeLine = /Frontend tests shard 4\/6: \d+(\.\d+)?s of the 0s target \(180s limit\) \(over target\)\n$/;
    assert.match(result.stderr, timeLine);
    assert.match(
      result.stderr,
      /Frontend tests shard 4\/6 took longer than the 0-second target\. That alone does not fail the run; only the 180-second limit does\./,
      'a run past its target must say so',
    );
    assert.match(
      fs.readFileSync(summaryFile, 'utf8'),
      new RegExp(`^${timeLine.source}`),
      'a run past its target must say so in the step summary',
    );
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

// Past the limit (#2873): the run is killed where it stands and fails with
// 124, and the message names both numbers.
{
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-summary-'));
  const summaryFile = path.join(scratch, 'summary.md');
  try {
    const { result, finished } = runWrapper({
      limits: { targetMs: 100, limitMs: 300 },
      env: { FAKE_NPM_RUN_MS: '30000', GITHUB_STEP_SUMMARY: summaryFile },
    });
    assert.equal(result.status, 124, 'a run past its limit must exit 124');
    assert.ok(!finished, 'a run past its limit must be killed, not waited for');
    assert.match(
      result.stderr,
      /Frontend tests exceeded the hard 0\.3-second limit \(the target is 0\.1 seconds\)\./,
    );
    const timeLine = /Frontend tests: \d+(\.\d+)?s of the 0\.1s target \(0\.3s limit\) \(limit exceeded\)\n$/;
    assert.match(result.stderr, timeLine);
    assert.doesNotMatch(result.stderr, /does not fail the run/);
    assert.match(fs.readFileSync(summaryFile, 'utf8'), new RegExp(`^${timeLine.source}`));
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

// In CI the target and the limit bind the whole job, not only its test step
// (#2873): the workflow records the job's start as its first step, and the
// time the job spent before the tests counts. `agoMs` is how long ago that
// first step ran.
const jobStartedAgo = (agoMs) => ({ CI_JOB_STARTED_AT_MS: String(Date.now() - agoMs) });

// A job that started 20 s ago and is still under its target: one quiet line
// that says what was measured, the job and the tests' own share of it.
{
  const { result } = runWrapper({
    env: { TEST_SHARD_INDEX: '2', TEST_SHARD_COUNT: '6', ...jobStartedAgo(20_000) },
  });
  assert.equal(result.status, 0);
  const line = result.stderr.match(
    /^Frontend tests shard 2\/6: job (\d+\.\d)s of the 120s target \(180s limit\), tests (\d+\.\d)s\n$/,
  );
  assert.ok(line, `a job under its target must print its time and nothing else, got ${JSON.stringify(result.stderr)}`);
  assert.ok(Number(line[1]) >= 20, 'the time the job spent before the tests must count');
  assert.ok(Number(line[2]) < 20, "the tests' own time must be reported beside the job's");
}

// A job already past its target before the tests start: the tests run, the
// job passes on their exit code, and it says so.
{
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-summary-'));
  const summaryFile = path.join(scratch, 'summary.md');
  try {
    const { result, finished } = runWrapper({
      env: {
        TEST_SHARD_INDEX: '4',
        TEST_SHARD_COUNT: '6',
        GITHUB_STEP_SUMMARY: summaryFile,
        ...jobStartedAgo(130_000),
      },
    });
    assert.equal(result.status, 0, 'a job past its target before its tests start must still pass');
    assert.ok(finished, 'a job past its target must have its tests left to finish');
    const timeLine =
      /Frontend tests shard 4\/6: job 1[3-7]\d\.\ds of the 120s target \(180s limit\), tests \d+\.\ds \(over target\)\n$/;
    assert.match(result.stderr, timeLine);
    assert.match(
      result.stderr,
      /The CI job for frontend tests shard 4\/6 took longer than the 120-second target\. That alone does not fail the run; only the 180-second limit does\./,
      'a job past its target must say so',
    );
    assert.match(
      fs.readFileSync(summaryFile, 'utf8'),
      new RegExp(`^${timeLine.source}`),
      'a job past its target must say so in the step summary',
    );
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

// The kill comes when the JOB reaches its limit: the tests get what the
// job's earlier steps left of it, not a fresh limit of their own. Here the
// job is 20 s into a 20.5 s limit, and tests that need 4 s are stopped.
{
  const { result, finished } = runWrapper({
    limits: { targetMs: 10_000, limitMs: 20_500 },
    env: { FAKE_NPM_RUN_MS: '4000', ...jobStartedAgo(20_000) },
  });
  assert.equal(result.status, 124, 'a job that reaches its limit during its tests must exit 124');
  assert.ok(!finished, 'the tests must be killed when the job reaches its limit');
  assert.match(
    result.stderr,
    /The CI job for frontend tests exceeded the hard 20\.5-second limit \(the target is 10 seconds\)/,
  );
  assert.match(
    result.stderr,
    /Frontend tests: job 2\d\.\ds of the 10s target \(20\.5s limit\), tests \d+\.\ds \(limit exceeded\)\n$/,
  );
}

// A job already past its limit before the tests start fails without running
// them, and says that is why.
{
  const { result, capture } = runWrapper({ env: jobStartedAgo(200_000) });
  assert.equal(result.status, 124, 'a job already past its limit must exit 124');
  assert.equal(capture, null, 'a job already past its limit must not start its tests');
  assert.match(
    result.stderr,
    /The CI job for frontend tests exceeded the hard 180-second limit \(the target is 120 seconds\) before the tests started, so they were not run\./,
  );
  assert.match(
    result.stderr,
    /Frontend tests: job 2\d\d\.\ds of the 120s target \(180s limit\), tests \d+\.\ds \(limit exceeded\)\n$/,
  );
}

// A recorded start that cannot be this job's is named in one line and
// ignored: the run is timed from its own start and keeps its exit code.
for (const [unusable, problem] of [
  ['yesterday', 'is not a whole number of milliseconds'],
  [String(Date.now() + 3_600_000), 'is in the future'],
  // Seconds where milliseconds belong read as a start in January 1970.
  [String(Math.floor(Date.now() / 1000)), 'is more than six hours old'],
]) {
  const { result } = runWrapper({
    env: { CI_JOB_STARTED_AT_MS: unusable, FAKE_NPM_EXIT_CODE: '5' },
  });
  assert.equal(result.status, 5, 'an unusable job start must not change the exit code');
  assert.match(
    result.stderr,
    new RegExp(
      `^CI_JOB_STARTED_AT_MS="${unusable}" ${problem}, so the job's earlier steps are not counted: ` +
        'timing the frontend tests from their own start\\.\\n' +
        'Frontend tests: \\d+\\.\\ds of the 120s target \\(180s limit\\)\\n$',
    ),
    `a job start that ${problem} must be named and ignored`,
  );
}

console.log('run-test-budget shard guard ok');
