const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

// vitest.ci.config.ts deals the spec files to CI's shards by size (#2592). A
// dealing mistake would fail no test: the specs it dropped would just never
// run. This deals the real suite into every shard count up to past CI's six
// and asserts that each spec runs in exactly one shard, and that the largest
// specs, which are the slowest, never share one.

const frontendRoot = path.resolve(__dirname, '..');
const configUrl = pathToFileURL(path.join(frontendRoot, 'vitest.ci.config.ts')).href;

const specs = fs
  .readdirSync(path.join(frontendRoot, 'src'), { recursive: true })
  .filter((entry) => entry.endsWith('.spec.ts'))
  .map((entry) => ({ moduleId: path.join(frontendRoot, 'src', entry) }));
assert.ok(specs.length > 8, 'found almost no spec files; the scan is broken');

const bySizeDescending = [...specs].sort(
  (a, b) => fs.statSync(b.moduleId).size - fs.statSync(a.moduleId).size,
);

/** The specs shard `index` of `count` runs, as CI's runner config deals them. */
async function deal(index, count) {
  process.env.TEST_SHARD_INDEX = String(index);
  process.env.TEST_SHARD_COUNT = String(count);
  // The config reads its shard from the environment as it loads, so each
  // shard loads its own copy.
  const { default: config } = await import(`${configUrl}?shard=${index}/${count}`);
  const Sequencer = config.test.sequence.sequencer;
  return new Sequencer({}).shard(specs);
}

async function main() {
  for (let count = 1; count <= 8; count += 1) {
    const shardOf = new Map();
    for (let index = 1; index <= count; index += 1) {
      for (const { moduleId } of await deal(index, count)) {
        const relative = path.relative(frontendRoot, moduleId);
        assert.ok(!shardOf.has(moduleId), `${relative} is dealt to shards ${shardOf.get(moduleId)} and ${index} of ${count}.`);
        shardOf.set(moduleId, index);
      }
    }
    for (const { moduleId } of specs) {
      const relative = path.relative(frontendRoot, moduleId);
      assert.ok(shardOf.has(moduleId), `${relative} is dealt to none of ${count} shards, so it would never run.`);
    }
    const largestShards = bySizeDescending.slice(0, count).map(({ moduleId }) => shardOf.get(moduleId));
    assert.equal(new Set(largestShards).size, count, `the ${count} largest specs must each run in a different one of ${count} shards.`);
  }
  console.log('ci shard balance guard ok');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
