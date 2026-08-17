import { describe, expect, it } from "vitest";
import { computeGridSize, type StrategyRangeConfig } from "./recency-param-range";

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
