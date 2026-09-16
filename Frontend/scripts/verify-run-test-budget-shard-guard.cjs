const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// scripts/run-test-budget.cjs mirrors .github/workflows/ci.yml's
// frontend-test-shard matrix locally: reject a half-set shard env pair
// instead of silently falling back to the full suite, auto-append
// --runner-config=vitest.ci.config.ts when both shard vars are set (unless
// the caller already passed a --runner-config), and default
// NG_BUILD_MAX_WORKERS=2 on the spawned child. None of that was under
// regression coverage — only the pre-existing TEST_BUDGET_MS literal was
// (PythonDataService/tests/contracts/test_pytest_configuration.py). This
// stubs the "npm" the wrapper spawns so each case runs in milliseconds and
// asserts on the exact argv/env/exit status the wrapper produces.

const wrapperPath = path.resolve(__dirname, 'run-test-budget.cjs');

function makeFakeNpm(captureFile) {
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
    '  },',
    '}));',
    'process.exit(Number(process.env.FAKE_NPM_EXIT_CODE ?? "0"));',
    '',
  ].join('\n');
  const npmPath = path.join(binDir, 'npm');
  fs.writeFileSync(npmPath, fakeNpmSource, { mode: 0o755 });
  return binDir;
}

// Vars this guard drives per test case. CI's frontend-test-shard job sets
// TEST_SHARD_INDEX/TEST_SHARD_COUNT at the job level, and test:guards runs
// inside the wrapper's own spawned child in real usage — so this guard's own
// process can inherit them. Strip them from the base env before layering on
// each case's explicit overrides, or a "neither set" case would silently
// inherit the host's shard vars on CI.
const CASE_DRIVEN_ENV_VARS = [
  'TEST_SHARD_INDEX',
  'TEST_SHARD_COUNT',
  'NG_BUILD_MAX_WORKERS',
  'FAKE_NPM_EXIT_CODE',
];

// Runs the real wrapper as a child process with a stubbed "npm" ahead of the
// real one on PATH, so we assert on what the wrapper actually spawns rather
// than on its source text.
function runWrapper({ args = [], env = {} } = {}) {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'run-test-budget-guard-'));
  const captureFile = path.join(scratch, 'capture.json');
  const fakeNpmBinDir = makeFakeNpm(captureFile);
  const baseEnv = { ...process.env };
  for (const key of CASE_DRIVEN_ENV_VARS) delete baseEnv[key];
  try {
    const result = spawnSync(process.execPath, [wrapperPath, ...args], {
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
    return { result, capture };
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

// Both shard vars set: auto-append the CI runner config to the forwarded args.
{
  const { result, capture } = runWrapper({
    env: { TEST_SHARD_INDEX: '1', TEST_SHARD_COUNT: '6', FAKE_NPM_EXIT_CODE: '0' },
  });
  assert.equal(result.status, 0);
  assert.ok(capture, 'both shard vars set must spawn npm');
  assert.deepEqual(capture.argv, [
    'run',
    'test:unbounded',
    '--',
    '--runner-config=vitest.ci.config.ts',
  ]);
}

// Both shard vars set but the caller already passed --runner-config=...:
// the wrapper must not append a second one.
{
  const { capture } = runWrapper({
    args: ['--runner-config=custom.config.ts'],
    env: { TEST_SHARD_INDEX: '1', TEST_SHARD_COUNT: '6' },
  });
  assert.ok(capture, 'both shard vars set (custom runner-config) must spawn npm');
  assert.deepEqual(capture.argv, ['run', 'test:unbounded', '--', '--runner-config=custom.config.ts']);
}

// Neither shard var set: forwarded args pass through unchanged, no runner
// config is appended (the plain, unsharded local run).
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

console.log('run-test-budget shard guard ok');
