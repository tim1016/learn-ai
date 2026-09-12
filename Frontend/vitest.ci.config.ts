import { defineConfig } from "vitest/config";

// The Angular test builder executes this config under Node, but @types/node is
// not a dependency, so declare the one Node global this file touches.
declare const process: {
  env: Record<string, string | undefined>;
};

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

// `shard` is honored from a config file at runtime, but vitest 4's
// `InlineConfig` type declares it only on the CLI-options surface, so the
// test block is built in a variable to keep the excess-property check away.
const testConfig = {
  maxWorkers: 2,
  shard: `${shardIndex}/${shardCount}`,
};

export default defineConfig({
  test: testConfig,
});
