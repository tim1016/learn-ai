import { describe, expect, it } from "vitest";
import { computeGridSize, parseValueList, rangeProblem, type StrategyRangeConfig } from "./param-range";

describe("computeGridSize", () => {
  it.each<[string, StrategyRangeConfig[], string[], number]>([
    [
      "multiplies symbol count by the cartesian product of param ranges",
      [
        {
          strategyKey: "ema_crossover_2_bps",
          paramRanges: {
            gap_bps: { type: "value_list", values: [1, 2] },
            rsi_min: { type: "value_list", values: [50] },
          },
        },
      ],
      ["SPY", "AAPL"],
      4, // 2 symbols x (2 x 1)
    ],
    [
      "sums combo counts across multiple strategies",
      [
        { strategyKey: "a", paramRanges: { p: { type: "value_list", values: [1, 2] } } },
        { strategyKey: "b", paramRanges: { p: { type: "value_list", values: [1, 2, 3] } } },
      ],
      ["SPY"],
      5, // 1 x (2 + 3)
    ],
    [
      "counts a low/high/step range inclusively",
      [{ strategyKey: "a", paramRanges: { p: { type: "low_high_step", low: 1, high: 3, step: 1 } } }],
      ["SPY"],
      3, // 1, 2, 3
    ],
    [
      "treats a strategy with no param ranges as one combo",
      [{ strategyKey: "a", paramRanges: {} }],
      ["SPY", "AAPL"],
      2,
    ],
  ])("%s", (_name, strategies, symbols, want) => {
    expect(computeGridSize(strategies, symbols)).toBe(want);
  });

  it("returns 0 for no symbols or no strategies", () => {
    const strategies: StrategyRangeConfig[] = [
      { strategyKey: "a", paramRanges: { p: { type: "value_list", values: [1] } } },
    ];
    expect(computeGridSize(strategies, [])).toBe(0);
    expect(computeGridSize([], ["SPY"])).toBe(0);
  });
});

describe("low/high/step counts match the canonical Python range size", () => {
  const count = (low: number, high: number, step: number): number =>
    computeGridSize([{ strategyKey: "s", paramRanges: { p: { type: "low_high_step", low, high, step } } }], ["SPY"]);

  it("counts exactly like the canonical Python range: no cell past high, clean decimal grids, long spans and exponent inputs, non-finite bounds as empty", () => {
    expect.soft(count(0, 0.29999999999999993, 0.1), "never counts a cell past high, even when high sits just below a step boundary: 0 to 0.29999999999999993 by 0.1").toBe(3);
    expect.soft(count(0, 1, 0.6), "never counts a cell past high, even when high sits just below a step boundary: 0 to 1 by 0.6").toBe(2);
    expect.soft(count(0, 1, 0.3), "never counts a cell past high, even when high sits just below a step boundary: 0 to 1 by 0.3").toBe(4);

    expect.soft(count(0.15, 0.6, 0.15), "counts a clean decimal grid exactly: 0.15 to 0.6 by 0.15").toBe(4);
    expect.soft(count(0.15, 0.25, 0.01), "counts a clean decimal grid exactly: 0.15 to 0.25 by 0.01").toBe(11);
    expect.soft(count(5, 5, 1e-20), "counts a clean decimal grid exactly: 5 to 5 by 1e-20").toBe(1);

    expect.soft(count(1e-30, 0.9, 0.1), "is exact over a long span and with exponent-form inputs: 1e-30 to 0.9 by 0.1").toBe(9);
    expect.soft(count(0, 1e-9, 1e-10), "is exact over a long span and with exponent-form inputs: 0 to 1e-9 by 1e-10").toBe(11);
    expect.soft(count(0, 1.5e21, 5e20), "is exact over a long span and with exponent-form inputs: 0 to 1.5e21 by 5e20").toBe(4);

    expect.soft(count(0, Number.NaN, 0.1), "treats non-finite bounds as an empty grid rather than throwing: NaN high").toBe(0);
    expect.soft(count(0, Number.POSITIVE_INFINITY, 0.1), "treats non-finite bounds as an empty grid rather than throwing: infinite high").toBe(0);
  });
});

describe("parseValueList", () => {
  it("reads a clean list, and refuses a malformed entry, an empty entry and an empty list by name", () => {
    expect.soft(parseValueList(" 0, .1 ,0.2 "), "parses a comma-separated list, trimming whitespace").toEqual({ values: [0, 0.1, 0.2] });

    // #1940: "0,.1,.2,.3..4,.5" used to become [0, 0.1, 0.2, 0.5] with no message.
    expect.soft(parseValueList("0,.1,.2,.3..4,.5"), "refuses a malformed entry by name instead of dropping it (#1940)").toEqual({ problem: '".3..4" is not a number.' });

    expect.soft(parseValueList("1,,2"), "refuses an empty entry between commas: 1,,2").toEqual({ problem: "Remove the empty entry next to a comma." });
    expect.soft(parseValueList("1,2,"), "refuses an empty entry between commas: trailing comma").toEqual({ problem: "Remove the empty entry next to a comma." });

    expect.soft(parseValueList(""), "refuses an empty list: empty string").toEqual({ problem: "Enter at least one value." });
    expect.soft(parseValueList("   "), "refuses an empty list: blanks only").toEqual({ problem: "Enter at least one value." });
  });
});

describe("rangeProblem", () => {
  it("reports only the editor's refused (empty) value list", () => {
    expect(rangeProblem({ type: "value_list", values: [] })).not.toBeNull();
    expect(rangeProblem({ type: "value_list", values: [1] })).toBeNull();
    expect(rangeProblem({ type: "low_high_step", low: 1, high: 5, step: 1 })).toBeNull();
  });
});
