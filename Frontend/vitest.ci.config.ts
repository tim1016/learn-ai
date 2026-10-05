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

/**
 * How much of a shard's time a spec is expected to take, from its size: the
 * best predictor a cold CI runner has. The exponent is measured, not derived.
 * In CI run 37268445501 a spec's size explained its run time well
 * (correlation 0.84), but the largest specs ran longer than their size alone
 * says: the two largest, 113 and 133 KB, took 32 s and 27 s of the suite's
 * 276 s. Dealt in turn by plain size, the six shards carried 32 s to 62 s of
 * tests; dealt by this weight, the same timings come to 42 s to 53 s (#2882).
 */
const SIZE_WEIGHT_EXPONENT = 1.5;

/**
 * Deals spec files to shards heaviest first, each to the shard that is
 * lightest so far, so every shard gets a near-equal share of the expected
 * work. Vitest's own split hashes each path instead, which balances the file
 * count but not the work: it put the two largest specs, also the two slowest,
 * into one shard, which then carried nearly half the suite's test time and ran
 * out of the 120 s budget (#2592). Every shard computes the same deal from the
 * same files, so each spec runs in exactly one of them.
 */
class SizeBalancedSequencer extends BaseSequencer {
  override async shard(files: TestSpecification[]): Promise<TestSpecification[]> {
    const loads = new Array<number>(shardCount).fill(0);
    return files
      .map((spec) => ({ spec, weight: statSync(spec.moduleId).size ** SIZE_WEIGHT_EXPONENT }))
      .sort((a, b) => b.weight - a.weight || (a.spec.moduleId < b.spec.moduleId ? -1 : 1))
      .filter(({ weight }) => {
        const lightest = loads.indexOf(Math.min(...loads));
        loads[lightest] += weight;
        return lightest + 1 === shardIndex;
      })
      .map(({ spec }) => spec);
  }
}

// `shard` is honored from a config file at runtime, but vitest 4's
// `InlineConfig` type declares it only on the CLI-options surface, so the
// test block is built in a variable to keep the excess-property check away.
//
// Each spec runs in its own VM context inside a worker thread, not in a
// worker of its own. Every spec still gets its own module registry, so one
// spec's `vi.mock` cannot bind a module for another, which is why the suite
// is isolated at all. What it no longer pays is a new jsdom and a new process
// per spec: that was a third of a shard's test run. Node keeps the modules a
// context imported for the life of its thread, so a worker past the memory
// limit is replaced; without the limit the whole suite in one run reached
// 8 GB and lost a worker, and with it stays under 4 GB (#2882).
const testConfig = {
  maxWorkers: 2,
  pool: "vmThreads",
  vmMemoryLimit: "1GB",
  shard: `${shardIndex}/${shardCount}`,
  sequence: { sequencer: SizeBalancedSequencer },
};

export default defineConfig({
  test: testConfig,
});
