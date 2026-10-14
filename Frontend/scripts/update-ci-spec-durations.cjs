const fs = require('node:fs');
const path = require('node:path');

// Regenerates ci-spec-durations.json, which vitest.ci.config.ts deals the spec
// files to CI's shards by (#3123). Feed it the output of vitest runs: a CI
// shard's job log or a local run, one or many. Each run's reporter prints a line
// per spec file with the milliseconds it took; a spec's recorded time is the mean
// over the runs that ran it, because one run's timing jitters by a third either
// way and the shards' own timings are what the deal must beat.
//
//   gh api repos/<owner>/<repo>/actions/jobs/<job-id>/logs > shard3.log
//   node scripts/update-ci-spec-durations.cjs shard1.log shard2.log ...
//
// Every shard of a run must be passed, or the specs of the missing shards fall
// back to a size estimate. Regenerate when the shard times in the CI log drift
// toward the two-minute target, not on every change: a spec missing from the file
// is estimated from its size and still runs in exactly one shard.

const frontendRoot = path.resolve(__dirname, '..');
const outputPath = path.join(frontendRoot, 'ci-spec-durations.json');
// Must match SIZE_WEIGHT_EXPONENT in vitest.ci.config.ts: the estimate for a spec
// the file has not measured is this weight of its size, times the fitted rate.
const SIZE_WEIGHT_EXPONENT = 1.5;

const ANSI = /\x1b\[[0-9;]*m/g;
const LOG_TIMESTAMP = /^\d{4}-\d\d-\d\dT[\d:.]+Z /;
// `✓ frontend src/app/x.spec.ts (12 tests) 345ms`, with ` 2 failed` for a red file.
const SPEC_LINE = /^\s*[✓✗×❯]\s+\S+\s+(src\/app\/\S+\.spec\.ts)\s+\(\d+ tests?[^)]*\)\s*(?:\d+ failed\s*)?(\d+)ms/;

const logs = process.argv.slice(2);
if (logs.length === 0) {
  console.error('usage: node scripts/update-ci-spec-durations.cjs <vitest-output.log>...');
  process.exit(2);
}

const samples = new Map();
for (const log of logs) {
  const seenInThisLog = new Set();
  for (const raw of fs.readFileSync(log, 'utf8').split('\n')) {
    const match = SPEC_LINE.exec(raw.replace(ANSI, '').replace(LOG_TIMESTAMP, ''));
    if (match === null || seenInThisLog.has(match[1])) continue;
    seenInThisLog.add(match[1]);
    samples.set(match[1], [...(samples.get(match[1]) ?? []), Number(match[2]) / 1000]);
  }
  if (seenInThisLog.size === 0) {
    console.error(`${log}: no spec timing lines found; is it a vitest run's output?`);
    process.exit(1);
  }
}

const specs = {};
let measuredSeconds = 0;
let sizeWeight = 0;
const dropped = [];
for (const [spec, seconds] of [...samples].sort(([a], [b]) => (a < b ? -1 : 1))) {
  const file = path.join(frontendRoot, spec);
  if (!fs.existsSync(file)) {
    dropped.push(spec);
    continue;
  }
  const mean = seconds.reduce((sum, value) => sum + value, 0) / seconds.length;
  specs[spec] = Math.round(mean * 1000) / 1000;
  measuredSeconds += mean;
  sizeWeight += fs.statSync(file).size ** SIZE_WEIGHT_EXPONENT;
}

const inTree = fs
  .readdirSync(path.join(frontendRoot, 'src'), { recursive: true })
  .map((entry) => `src/${String(entry).replaceAll(path.sep, '/')}`)
  .filter((entry) => entry.endsWith('.spec.ts'));
const unmeasured = inTree.filter((spec) => !(spec in specs));

fs.writeFileSync(
  outputPath,
  `${JSON.stringify({ secondsPerSizeWeight: Number((measuredSeconds / sizeWeight).toPrecision(4)), specs }, null, 2)}\n`,
);
console.log(
  `${path.relative(frontendRoot, outputPath)}: ${Object.keys(specs).length} specs from ${logs.length} log(s), ` +
    `${measuredSeconds.toFixed(1)} s combined; ${unmeasured.length} in the tree unmeasured, ${dropped.length} dropped (no longer in the tree).`,
);
