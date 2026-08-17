import { describe, expect, it } from "vitest";
import { filterToHeroAndExpanded, groupKey, isHeroTrade } from "./recency-hero-fold";

const heroes = [{ symbol: "SPY", strategyKey: "ema_crossover_2_bps", paramsHash: "hero-hash" }];

function trade(symbol: string, strategyKey: string, paramsHash: string) {
  return { symbol, strategyKey, paramsHash };
}

describe("isHeroTrade", () => {
  it("is true for a trade matching the hero's symbol, strategy, and paramsHash", () => {
    expect(isHeroTrade(trade("SPY", "ema_crossover_2_bps", "hero-hash"), heroes)).toBe(true);
  });

  it("is false for a different combo of the same strategy and symbol", () => {
    expect(isHeroTrade(trade("SPY", "ema_crossover_2_bps", "other-hash"), heroes)).toBe(false);
  });

  it("is false for a different symbol entirely", () => {
    expect(isHeroTrade(trade("AAPL", "ema_crossover_2_bps", "hero-hash"), heroes)).toBe(false);
  });
});

describe("groupKey", () => {
  it("is stable and distinguishes symbol from strategy", () => {
    expect(groupKey("SPY", "ema_crossover_2_bps")).toBe(groupKey("SPY", "ema_crossover_2_bps"));
    expect(groupKey("SPY", "a")).not.toBe(groupKey("SPY", "b"));
  });
});

describe("filterToHeroAndExpanded", () => {
  it("keeps hero trades by default even when nothing is expanded", () => {
    const trades = [trade("SPY", "ema_crossover_2_bps", "hero-hash")];
    expect(filterToHeroAndExpanded(trades, heroes, new Set())).toEqual(trades);
  });

  it("hides a non-hero combo when its group is not expanded", () => {
    const trades = [trade("SPY", "ema_crossover_2_bps", "other-hash")];
    expect(filterToHeroAndExpanded(trades, heroes, new Set())).toEqual([]);
  });

  it("shows a non-hero combo once its (symbol, strategy) group is expanded", () => {
    const trades = [trade("SPY", "ema_crossover_2_bps", "other-hash")];
    const expanded = new Set([groupKey("SPY", "ema_crossover_2_bps")]);
    expect(filterToHeroAndExpanded(trades, heroes, expanded)).toEqual(trades);
  });

  it("expanding one group does not reveal a different group's non-hero combos", () => {
    const trades = [trade("AAPL", "ema_crossover_2_bps", "other-hash")];
    const expanded = new Set([groupKey("SPY", "ema_crossover_2_bps")]);
    expect(filterToHeroAndExpanded(trades, heroes, expanded)).toEqual([]);
  });
});
