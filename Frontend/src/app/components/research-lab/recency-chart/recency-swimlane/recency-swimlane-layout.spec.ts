import { describe, expect, it } from "vitest";
import {
  computeSwimlaneLayout,
  packLane,
  pnlColor,
  sharpeToOpacity,
  type RecencySwimlaneTrade,
} from "./recency-swimlane-layout";

function trade(overrides: Partial<RecencySwimlaneTrade> = {}): RecencySwimlaneTrade {
  return {
    symbol: "SPY",
    strategyKey: "ema_crossover_2_bps",
    paramsHash: "hash1",
    fingerprint: "fp1",
    entryMs: 1_000,
    exitMs: 2_000,
    pnlPts: 2,
    pnlPct: 0.02,
    quantity: 10,
    pnl: 20,
    holdingSessions: 1,
    sharpe: 1.0,
    studyId: null,
    recencyRunId: 1,
    ...overrides,
  };
}

describe("pnlColor", () => {
  it("maps pnl onto a diverging color scale: midpoint at zero, distinct extremes, symmetric, clamped, safe on a zero domain", () => {
    expect.soft(pnlColor(0, 100), "returns the midpoint color at pnl = 0").toBe(pnlColor(0, 100));
    // breakeven should sit at the midpoint regardless of domain
    const atZeroA = pnlColor(0, 50);
    const atZeroB = pnlColor(0, 200);
    expect.soft(atZeroA, "breakeven sits at the midpoint regardless of domain").toBe(atZeroB);

    const positive = pnlColor(100, 100);
    const negative = pnlColor(-100, 100);
    expect.soft(positive, "is a distinct color at the positive extreme vs the negative extreme").not.toBe(negative);

    const mid = pnlColor(0, 100);
    const pos = pnlColor(50, 100);
    const neg = pnlColor(-50, 100);
    expect.soft(pos, "is symmetric in magnitude: +x differs from the midpoint color").not.toBe(mid);
    expect.soft(neg, "is symmetric in magnitude: -x differs from the midpoint color").not.toBe(mid);
    expect.soft(pos, "is symmetric in magnitude: +x differs from -x").not.toBe(neg);

    expect.soft(() => pnlColor(0, 0), "does not throw when domain is zero (all trades breakeven)").not.toThrow();

    expect.soft(pnlColor(1000, 100), "clamps values beyond the domain to the extreme color: positive").toBe(pnlColor(100, 100));
    expect.soft(pnlColor(-1000, 100), "clamps values beyond the domain to the extreme color: negative").toBe(pnlColor(-100, 100));
  });
});

describe("sharpeToOpacity", () => {
  it("maps sharpe to an opacity: floor for zero or negative, monotone for positive, bounded by 1 and the floor", () => {
    const floor = sharpeToOpacity(null);
    expect.soft(sharpeToOpacity(0), "returns the floor for zero sharpe").toBe(floor);
    expect.soft(sharpeToOpacity(-1), "returns the floor for negative sharpe").toBe(floor);

    const low = sharpeToOpacity(0.2);
    const mid = sharpeToOpacity(1.0);
    const high = sharpeToOpacity(3.0);
    expect.soft(low, "increases monotonically for increasing positive sharpe: low < mid").toBeLessThan(mid);
    expect.soft(mid, "increases monotonically for increasing positive sharpe: mid <= high").toBeLessThanOrEqual(high);

    expect.soft(sharpeToOpacity(100), "never exceeds 1").toBeLessThanOrEqual(1);
    expect.soft(sharpeToOpacity(-100), "never drops below the floor").toBeGreaterThanOrEqual(sharpeToOpacity(null));
  });
});

describe("packLane", () => {
  it("packs trades into sub-rows: non-overlapping share row 0, the highest pnl of an overlap takes row 0, times are never nudged, an empty lane packs to nothing", () => {
    const apart = packLane([
      trade({ fingerprint: "a", entryMs: 0, exitMs: 100, pnl: 10 }),
      trade({ fingerprint: "b", entryMs: 200, exitMs: 300, pnl: 20 }),
    ]);
    expect.soft(apart.find((p) => p.trade.fingerprint === "a")?.subRow, "places non-overlapping trades in the same sub-row: a").toBe(0);
    expect.soft(apart.find((p) => p.trade.fingerprint === "b")?.subRow, "places non-overlapping trades in the same sub-row: b").toBe(0);

    const overlapping = packLane([
      trade({ fingerprint: "low", entryMs: 0, exitMs: 200, pnl: 5 }),
      trade({ fingerprint: "high", entryMs: 100, exitMs: 300, pnl: 50 }),
    ]);
    expect.soft(overlapping.find((p) => p.trade.fingerprint === "high")?.subRow, "gives the highest-pnl overlapping trade the top sub-row (row 0)").toBe(0);
    expect.soft(overlapping.find((p) => p.trade.fingerprint === "low")?.subRow, "gives the lower-pnl overlapping trade the next sub-row (row 1)").toBe(1);

    const t = trade({ entryMs: 12345, exitMs: 67890 });
    const single = packLane([t]);
    expect.soft(single[0].trade.entryMs, "never nudges a trade's entry time").toBe(12345);
    expect.soft(single[0].trade.exitMs, "never nudges a trade's exit time").toBe(67890);

    expect.soft(packLane([]), "handles an empty lane").toEqual([]);
  });
});

describe("computeSwimlaneLayout", () => {
  it("lays trades out: a lane per symbol ordered by recency, a symmetric pnl-percent domain, same-percent trades share a color, empty in empty out, every bar resolved", () => {
    const bySymbol = computeSwimlaneLayout([
      trade({ symbol: "SPY", fingerprint: "a" }),
      trade({ symbol: "AAPL", fingerprint: "b" }),
    ]);
    expect.soft(bySymbol.lanes.map((l) => l.symbol).sort(), "groups trades into one lane per symbol").toEqual(["AAPL", "SPY"]);

    const byRecency = computeSwimlaneLayout([
      trade({ symbol: "OLD", fingerprint: "a", entryMs: 1_000 }),
      trade({ symbol: "FRESH", fingerprint: "b", entryMs: 9_000 }),
    ]);
    expect.soft(byRecency.lanes[0].symbol, "orders lanes by recency: the symbol with the most recent trade first").toBe("FRESH");
    expect.soft(byRecency.lanes[1].symbol, "orders lanes by recency: the older symbol second").toBe("OLD");

    const domain = computeSwimlaneLayout([
      trade({ fingerprint: "a", pnlPct: -0.3, pnl: -3000 }),
      trade({ fingerprint: "b", pnlPct: 0.12, pnl: 12 }),
    ]);
    expect.soft(domain.pnlDomain, "computes a symmetric pnl domain across all shown trades, from percentage return not dollar amount").toBe(0.3);

    // Same % outcome, wildly different dollar size (quantity/price) — the
    // hue must be cross-symbol comparable, so it has to key off pnlPct.
    const samePct = computeSwimlaneLayout([
      trade({ fingerprint: "small", symbol: "AAPL", pnlPct: 0.1, pnl: 5 }),
      trade({ fingerprint: "large", symbol: "SPY", pnlPct: 0.1, pnl: 50_000 }),
    ]);
    const small = samePct.bars.find((b) => b.trade.fingerprint === "small");
    const large = samePct.bars.find((b) => b.trade.fingerprint === "large");
    expect.soft(small?.fillColor, "colors two trades with the same percentage return identically, even with very different dollar pnl").toBe(large?.fillColor);

    const empty = computeSwimlaneLayout([]);
    expect.soft(empty.lanes, "returns an empty layout for no trades: no lanes").toEqual([]);
    expect.soft(empty.bars, "returns an empty layout for no trades: no bars").toEqual([]);

    const resolved = computeSwimlaneLayout([trade({ pnl: 15, sharpe: 2 })]);
    expect.soft(resolved.bars[0].fillColor, "every bar carries a resolved fill color").toMatch(/^#[0-9a-f]{6}$/i);
    expect.soft(resolved.bars[0].opacity, "every bar carries a resolved opacity above 0").toBeGreaterThan(0);
    expect.soft(resolved.bars[0].opacity, "every bar carries a resolved opacity at most 1").toBeLessThanOrEqual(1);
  });
});
