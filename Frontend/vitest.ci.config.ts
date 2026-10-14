import { defineConfig } from "vitest/config";
import { BaseSequencer, type TestSpecification } from "vitest/node";

// The Angular test builder executes this config under Node, but @types/node is
// not a dependency, so declare the Node surface this file touches. `node:fs`
// comes through `getBuiltinModule` (Node 22.3+) for the same reason.
declare const process: {
  env: Record<string, string | undefined>;
  getBuiltinModule(id: "node:fs"): {
    statSync(path: string): { size: number };
    readFileSync(path: URL, encoding: "utf8"): string;
  };
};

const { statSync, readFileSync } = process.getBuiltinModule("node:fs");

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
 * What a shard is dealt by: the seconds each spec took in CI, from
 * `ci-spec-durations.json`, which `scripts/update-ci-spec-durations.cjs`
 * regenerates from shard logs. A missing or unreadable file fails the run
 * loudly: reading it as empty would quietly deal by size again.
 *
 * The file replaced a deal by file size (#2882), whose predictor weakened as
 * page specs outgrew it. Over run 38494475556 size explained a spec's time
 * with correlation 0.69, and the options-backtest page specs ran 5 to 10 times
 * longer per byte than the rest. That deal left Frontend shard 3/6 at 117 s of
 * tests beside shard 2/6 at 59 s; the same run's durations, dealt from another
 * run's timings, score 83 s to 98 s (#3123).
 */
interface CiSpecDurations {
  /** Seconds per unit of size weight: the estimate for a spec the file has not measured yet. */
  secondsPerSizeWeight: number;
  /** Seconds a spec took, keyed by its path from `Frontend/`. */
  specs: Record<string, number>;
}

function loadCiSpecDurations(): CiSpecDurations {
  const url = new URL("./ci-spec-durations.json", import.meta.url);
  try {
    return JSON.parse(readFileSync(url, "utf8")) as CiSpecDurations;
  } catch (cause) {
    throw new Error(
      `ci-spec-durations.json could not be read (${String(cause)}); regenerate it with scripts/update-ci-spec-durations.cjs`,
    );
  }
}

const durations = loadCiSpecDurations();

/**
 * The exponent that turns a size into an estimate of time, for a spec the
 * durations file has not measured: a new spec is dealt by its size until the
 * file is regenerated, and still runs in exactly one shard. Measured in #2882.
 */
const SIZE_WEIGHT_EXPONENT = 1.5;

/** How much of a shard's time a spec is expected to take, in seconds. */
export function specWeight(moduleId: string): number {
  const path = moduleId.replaceAll("\\", "/");
  const measured = durations.specs[path.slice(path.lastIndexOf("/src/app/") + 1)];
  return measured ?? statSync(moduleId).size ** SIZE_WEIGHT_EXPONENT * durations.secondsPerSizeWeight;
}

/**
 * Deals spec files to shards heaviest first, each to the shard that is
 * lightest so far, so every shard gets a near-equal share of the expected
 * work. Vitest's own split hashes each path instead, which balances the file
 * count but not the work: it put the two largest specs, also the two slowest,
 * into one shard, which then carried nearly half the suite's test time and ran
 * out of the 120 s budget (#2592). Every shard computes the same deal from the
 * same files, so each spec runs in exactly one of them.
 */
class DurationBalancedSequencer extends BaseSequencer {
  override async shard(files: TestSpecification[]): Promise<TestSpecification[]> {
    const loads = new Array<number>(shardCount).fill(0);
    return files
      .map((spec) => ({ spec, weight: specWeight(spec.moduleId) }))
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
  sequence: { sequencer: DurationBalancedSequencer },
};

export default defineConfig({
  test: testConfig,
});
