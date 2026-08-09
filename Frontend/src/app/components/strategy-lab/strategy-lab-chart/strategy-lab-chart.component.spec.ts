import { describe, expect, it } from "vitest";

import {
  normalizeIndicatorResults,
  strategyIndicatorRequests,
} from "./strategy-lab-chart.adapter";

describe("strategyIndicatorRequests", () => {
  it("derives the strategy's configured signal indicators", () => {
    expect(strategyIndicatorRequests("custom", JSON.stringify({
      short_window: 12,
      long_window: 30,
      rsi_period: 9,
    }))).toEqual([
      { id: "sma-length-12", name: "sma", params: { length: 12 }, label: "SMA 12" },
      { id: "sma-length-30", name: "sma", params: { length: 30 }, label: "SMA 30" },
      { id: "rsi-length-9", name: "rsi", params: { length: 9 }, label: "RSI 9" },
    ]);
  });

  it("seeds the canonical EMA crossover indicators when legacy runs omitted parameters", () => {
    expect(strategyIndicatorRequests("spy_ema_crossover", null)).toEqual([
      { id: "ema-length-5", name: "ema", params: { length: 5 }, label: "EMA 5" },
      { id: "ema-length-10", name: "ema", params: { length: 10 }, label: "EMA 10" },
      { id: "rsi-length-14", name: "rsi", params: { length: 14 }, label: "RSI 14" },
    ]);
  });

  it("does not invent indicators for an unrelated strategy with unreadable parameters", () => {
    expect(strategyIndicatorRequests("custom", "not-json")).toEqual([]);
  });

  it("groups sub-pane series and drops warmup nulls without changing timestamps", () => {
    const normalized = normalizeIndicatorResults([{
      id: "rsi_14",
      panel: "rsi",
      type: "line",
      color: "#fff",
      refs: [30, 70],
      data: [{ t: 1_000, value: null }, { t: 2_000, value: 55 }],
    }]);

    expect(normalized.overlays).toEqual([]);
    expect(normalized.subPanes).toEqual([expect.objectContaining({
      id: "rsi",
      referenceLevels: [30, 70],
      series: [expect.objectContaining({ points: [{ timeMs: 2_000, value: 55 }] })],
    })]);
  });
});
