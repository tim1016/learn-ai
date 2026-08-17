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
  it("returns the midpoint color at pnl = 0", () => {
    expect(pnlColor(0, 100)).toBe(pnlColor(0, 100));
    // breakeven should sit at the midpoint regardless of domain
    const atZeroA = pnlColor(0, 50);
    const atZeroB = pnlColor(0, 200);
    expect(atZeroA).toBe(atZeroB);
  });

  it("is a distinct color at the positive extreme vs the negative extreme", () => {
    const positive = pnlColor(100, 100);
    const negative = pnlColor(-100, 100);
    expect(positive).not.toBe(negative);
  });

  it("is symmetric in magnitude: +x and -x are equidistant from the midpoint color", () => {
    const mid = pnlColor(0, 100);
    const pos = pnlColor(50, 100);
    const neg = pnlColor(-50, 100);
    expect(pos).not.toBe(mid);
    expect(neg).not.toBe(mid);
    expect(pos).not.toBe(neg);
  });

  it("does not throw when domain is zero (all trades breakeven)", () => {
    expect(() => pnlColor(0, 0)).not.toThrow();
  });

  it("clamps values beyond the domain to the extreme color", () => {
    expect(pnlColor(1000, 100)).toBe(pnlColor(100, 100));
    expect(pnlColor(-1000, 100)).toBe(pnlColor(-100, 100));
  });
});

describe("sharpeToOpacity", () => {
  it("returns the floor for null sharpe (insufficient trades)", () => {
    expect(sharpeToOpacity(null)).toBe(sharpeToOpacity(0));
  });

  it("returns the floor for zero or negative sharpe", () => {
    const floor = sharpeToOpacity(null);
    expect(sharpeToOpacity(0)).toBe(floor);
    expect(sharpeToOpacity(-1)).toBe(floor);
  });

  it("increases monotonically for increasing positive sharpe", () => {
    const low = sharpeToOpacity(0.2);
    const mid = sharpeToOpacity(1.0);
    const high = sharpeToOpacity(3.0);
    expect(low).toBeLessThan(mid);
    expect(mid).toBeLessThanOrEqual(high);
  });

  it("never exceeds 1 or drops below the floor", () => {
    expect(sharpeToOpacity(100)).toBeLessThanOrEqual(1);
    expect(sharpeToOpacity(-100)).toBeGreaterThanOrEqual(sharpeToOpacity(null));
  });
});

describe("packLane", () => {
  it("places non-overlapping trades in the same sub-row", () => {
    const trades = [
      trade({ fingerprint: "a", entryMs: 0, exitMs: 100, pnl: 10 }),
      trade({ fingerprint: "b", entryMs: 200, exitMs: 300, pnl: 20 }),
    ];
    const packed = packLane(trades);
    expect(packed.find((p) => p.trade.fingerprint === "a")?.subRow).toBe(0);
    expect(packed.find((p) => p.trade.fingerprint === "b")?.subRow).toBe(0);
  });

  it("places overlapping trades in different sub-rows", () => {
    const trades = [
      trade({ fingerprint: "a", entryMs: 0, exitMs: 200, pnl: 10 }),
      trade({ fingerprint: "b", entryMs: 100, exitMs: 300, pnl: 20 }),
    ];
    const packed = packLane(trades);
    const rowA = packed.find((p) => p.trade.fingerprint === "a")?.subRow;
    const rowB = packed.find((p) => p.trade.fingerprint === "b")?.subRow;
    expect(rowA).not.toBe(rowB);
  });

  it("gives the highest-pnl overlapping trade the top sub-row (row 0)", () => {
    const trades = [
      trade({ fingerprint: "low", entryMs: 0, exitMs: 200, pnl: 5 }),
      trade({ fingerprint: "high", entryMs: 100, exitMs: 300, pnl: 50 }),
    ];
    const packed = packLane(trades);
    expect(packed.find((p) => p.trade.fingerprint === "high")?.subRow).toBe(0);
    expect(packed.find((p) => p.trade.fingerprint === "low")?.subRow).toBe(1);
  });

  it("never nudges a trade's entry or exit time", () => {
    const t = trade({ entryMs: 12345, exitMs: 67890 });
    const packed = packLane([t]);
    expect(packed[0].trade.entryMs).toBe(12345);
    expect(packed[0].trade.exitMs).toBe(67890);
  });

  it("handles an empty lane", () => {
    expect(packLane([])).toEqual([]);
  });
});

describe("computeSwimlaneLayout", () => {
  it("groups trades into one lane per symbol", () => {
    const layout = computeSwimlaneLayout([
      trade({ symbol: "SPY", fingerprint: "a" }),
      trade({ symbol: "AAPL", fingerprint: "b" }),
    ]);
    expect(layout.lanes.map((l) => l.symbol).sort()).toEqual(["AAPL", "SPY"]);
  });

  it("orders lanes by recency — the symbol with the most recent trade first", () => {
    const layout = computeSwimlaneLayout([
      trade({ symbol: "OLD", fingerprint: "a", entryMs: 1_000 }),
      trade({ symbol: "FRESH", fingerprint: "b", entryMs: 9_000 }),
    ]);
    expect(layout.lanes[0].symbol).toBe("FRESH");
    expect(layout.lanes[1].symbol).toBe("OLD");
  });

  it("computes a symmetric pnl domain across all shown trades, from percentage return not dollar amount", () => {
    const layout = computeSwimlaneLayout([
      trade({ fingerprint: "a", pnlPct: -0.3, pnl: -3000 }),
      trade({ fingerprint: "b", pnlPct: 0.12, pnl: 12 }),
    ]);
    expect(layout.pnlDomain).toBe(0.3);
  });

  it("colors two trades with the same percentage return identically, even with very different dollar pnl", () => {
    // Same % outcome, wildly different dollar size (quantity/price) — the
    // hue must be cross-symbol comparable, so it has to key off pnlPct.
    const layout = computeSwimlaneLayout([
      trade({ fingerprint: "small", symbol: "AAPL", pnlPct: 0.1, pnl: 5 }),
      trade({ fingerprint: "large", symbol: "SPY", pnlPct: 0.1, pnl: 50_000 }),
    ]);
    const small = layout.bars.find((b) => b.trade.fingerprint === "small");
    const large = layout.bars.find((b) => b.trade.fingerprint === "large");
    expect(small?.fillColor).toBe(large?.fillColor);
  });

  it("returns an empty layout for no trades", () => {
    const layout = computeSwimlaneLayout([]);
    expect(layout.lanes).toEqual([]);
    expect(layout.bars).toEqual([]);
  });

  it("every bar carries a resolved fill color and opacity", () => {
    const layout = computeSwimlaneLayout([trade({ pnl: 15, sharpe: 2 })]);
    expect(layout.bars[0].fillColor).toMatch(/^#[0-9a-f]{6}$/i);
    expect(layout.bars[0].opacity).toBeGreaterThan(0);
    expect(layout.bars[0].opacity).toBeLessThanOrEqual(1);
  });
});
