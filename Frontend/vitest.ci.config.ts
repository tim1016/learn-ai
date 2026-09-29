import { defineConfig } from "vitest/config";
import { BaseSequencer, type TestSpecification } from "vitest/node";

// The Angular test builder executes this config under Node, but @types/node is
// not a dependency, so declare the Node surface this file touches. `node:fs`
// comes through `getBuiltinModule` (Node 22.3+) for the same reason.
declare const process: {
  env: Record<string, string | undefined>;
  getBuiltinModule(id: "node:fs"): { statSync(path: string): { size: number } };
};

const { statSync } = process.getBuiltinModule("node:fs");

function positiveInteger(name: string): number {
  const value = Number.parseInt(process.env[name] ?? "", 10);
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

const shardIndex = positiveInteger("TEST_SHARD_INDEX");
const shardCount = positiveInteger("TEST_SHARD_COUNT");

if (shardIndex > shardCount) {
  throw new Error("TEST_SHARD_INDEX must not exceed TEST_SHARD_COUNT");
}

/** The shard (1-based) that runs the spec of size rank `rank` (0 = largest):
 * 1, 2, …, count, then count, …, 2, 1, and again. */
function snakeShard(rank: number, count: number): number {
  const position = rank % count;
  return Math.floor(rank / count) % 2 === 0 ? position + 1 : count - position;
}

/**
 * Deals spec files to shards largest first, in snake order, so every shard
 * gets the same number of files, give or take one, and a near-equal share of
 * spec source. A spec's size is the best predictor of its run time that a
 * cold CI runner has. Vitest's own split hashes each path instead, which
 * balances the file count but not the work: it put the two largest specs,
 * also the two slowest, into one shard, which then carried nearly half the
 * suite's test time and ran out of the 120 s budget (#2592).
 */
class SizeBalancedSequencer extends BaseSequencer {
  override async shard(files: TestSpecification[]): Promise<TestSpecification[]> {
    return files
      .map((spec) => ({ spec, size: statSync(spec.moduleId).size }))
      .sort((a, b) => b.size - a.size || (a.spec.moduleId < b.spec.moduleId ? -1 : 1))
      .filter((_, rank) => snakeShard(rank, shardCount) === shardIndex)
      .map(({ spec }) => spec);
  }
}

// `shard` is honored from a config file at runtime, but vitest 4's
// `InlineConfig` type declares it only on the CLI-options surface, so the
// test block is built in a variable to keep the excess-property check away.
const testConfig = {
  maxWorkers: 2,
  shard: `${shardIndex}/${shardCount}`,
  sequence: { sequencer: SizeBalancedSequencer },
};

export default defineConfig({
  test: testConfig,
});
