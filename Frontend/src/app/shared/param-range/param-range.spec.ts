import { describe, expect, it } from "vitest";
import { computeGridSize, parseValueList, rangeProblem, type StrategyRangeConfig } from "./param-range";

describe("computeGridSize", () => {
  it("multiplies symbol count by the cartesian product of param ranges", () => {
    const strategies: StrategyRangeConfig[] = [
      {
        strategyKey: "ema_crossover_2_bps",
        paramRanges: {
          gap_bps: { type: "value_list", values: [1, 2] },
          rsi_min: { type: "value_list", values: [50] },
        },
      },
    ];
    expect(computeGridSize(strategies, ["SPY", "AAPL"])).toBe(4); // 2 symbols x (2 x 1)
  });

  it("sums combo counts across multiple strategies", () => {
    const strategies: StrategyRangeConfig[] = [
      { strategyKey: "a", paramRanges: { p: { type: "value_list", values: [1, 2] } } },
      { strategyKey: "b", paramRanges: { p: { type: "value_list", values: [1, 2, 3] } } },
    ];
    expect(computeGridSize(strategies, ["SPY"])).toBe(5); // 1 x (2 + 3)
  });

  it("counts a low/high/step range inclusively", () => {
    const strategies: StrategyRangeConfig[] = [
      { strategyKey: "a", paramRanges: { p: { type: "low_high_step", low: 1, high: 3, step: 1 } } },
    ];
    expect(computeGridSize(strategies, ["SPY"])).toBe(3); // 1, 2, 3
  });

  it("returns 0 for no symbols or no strategies", () => {
    const strategies: StrategyRangeConfig[] = [
      { strategyKey: "a", paramRanges: { p: { type: "value_list", values: [1] } } },
    ];
    expect(computeGridSize(strategies, [])).toBe(0);
    expect(computeGridSize([], ["SPY"])).toBe(0);
  });

  it("treats a strategy with no param ranges as one combo", () => {
    const strategies: StrategyRangeConfig[] = [{ strategyKey: "a", paramRanges: {} }];
    expect(computeGridSize(strategies, ["SPY", "AAPL"])).toBe(2);
  });
});

describe("low/high/step counts match the canonical Python range size", () => {
  const count = (low: number, high: number, step: number): number =>
    computeGridSize([{ strategyKey: "s", paramRanges: { p: { type: "low_high_step", low, high, step } } }], ["SPY"]);

  it("never counts a cell past high, even when high sits just below a step boundary", () => {
    expect(count(0, 0.29999999999999993, 0.1)).toBe(3);
    expect(count(0, 1, 0.6)).toBe(2);
    expect(count(0, 1, 0.3)).toBe(4);
  });

  it("counts a clean decimal grid exactly", () => {
    expect(count(0.15, 0.6, 0.15)).toBe(4);
    expect(count(0.15, 0.25, 0.01)).toBe(11);
    expect(count(5, 5, 1e-20)).toBe(1);
  });

  it("is exact over a long span and with exponent-form inputs", () => {
    expect(count(1e-30, 0.9, 0.1)).toBe(9);
    expect(count(0, 1e-9, 1e-10)).toBe(11);
    expect(count(0, 1.5e21, 5e20)).toBe(4);
  });

  it("treats non-finite bounds as an empty grid rather than throwing", () => {
    expect(count(0, Number.NaN, 0.1)).toBe(0);
    expect(count(0, Number.POSITIVE_INFINITY, 0.1)).toBe(0);
  });
});

describe("parseValueList", () => {
  it("parses a comma-separated list, trimming whitespace", () => {
    expect(parseValueList(" 0, .1 ,0.2 ")).toEqual({ values: [0, 0.1, 0.2] });
  });

  it("refuses a malformed entry by name instead of dropping it", () => {
    // #1940: "0,.1,.2,.3..4,.5" used to become [0, 0.1, 0.2, 0.5] with no message.
    expect(parseValueList("0,.1,.2,.3..4,.5")).toEqual({ problem: '".3..4" is not a number.' });
  });

  it("refuses an empty entry between commas", () => {
    expect(parseValueList("1,,2")).toEqual({ problem: "Remove the empty entry next to a comma." });
    expect(parseValueList("1,2,")).toEqual({ problem: "Remove the empty entry next to a comma." });
  });

  it("refuses an empty list", () => {
    expect(parseValueList("")).toEqual({ problem: "Enter at least one value." });
    expect(parseValueList("   ")).toEqual({ problem: "Enter at least one value." });
  });
});

describe("rangeProblem", () => {
  it("reports only the editor's refused (empty) value list", () => {
    expect(rangeProblem({ type: "value_list", values: [] })).not.toBeNull();
    expect(rangeProblem({ type: "value_list", values: [1] })).toBeNull();
    expect(rangeProblem({ type: "low_high_step", low: 1, high: 5, step: 1 })).toBeNull();
  });
});
