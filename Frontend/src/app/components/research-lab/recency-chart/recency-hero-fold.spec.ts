import { describe, expect, it } from "vitest";
import { filterToHeroAndExpanded, groupKey, isHeroTrade } from "./recency-hero-fold";

const heroes = [{ symbol: "SPY", strategyKey: "ema_crossover_2_bps", paramsHash: "hero-hash" }];

function trade(symbol: string, strategyKey: string, paramsHash: string) {
  return { symbol, strategyKey, paramsHash };
}

describe("isHeroTrade", () => {
  it.each([
    [true, "a trade matching the hero's symbol, strategy, and paramsHash", "SPY", "hero-hash"],
    [false, "a different combo of the same strategy and symbol", "SPY", "other-hash"],
    [false, "a different symbol entirely", "AAPL", "hero-hash"],
  ])("is %s for %s", (want, _kind, symbol, paramsHash) => {
    expect(isHeroTrade(trade(symbol, "ema_crossover_2_bps", paramsHash), heroes)).toBe(want);
  });
});

describe("groupKey", () => {
  it("is stable and distinguishes symbol from strategy", () => {
    expect(groupKey("SPY", "ema_crossover_2_bps")).toBe(groupKey("SPY", "ema_crossover_2_bps"));
    expect(groupKey("SPY", "a")).not.toBe(groupKey("SPY", "b"));
  });
});

describe("filterToHeroAndExpanded", () => {
  const heroTrades = [trade("SPY", "ema_crossover_2_bps", "hero-hash")];
  const otherSpyTrades = [trade("SPY", "ema_crossover_2_bps", "other-hash")];
  const otherAaplTrades = [trade("AAPL", "ema_crossover_2_bps", "other-hash")];

  it.each([
    ["keeps hero trades by default even when nothing is expanded", heroTrades, new Set<string>(), heroTrades],
    ["hides a non-hero combo when its group is not expanded", otherSpyTrades, new Set<string>(), []],
    ["shows a non-hero combo once its (symbol, strategy) group is expanded", otherSpyTrades, new Set([groupKey("SPY", "ema_crossover_2_bps")]), otherSpyTrades],
    ["expanding one group does not reveal a different group's non-hero combos", otherAaplTrades, new Set([groupKey("SPY", "ema_crossover_2_bps")]), []],
  ])("%s", (_name, trades, expanded, want) => {
    expect(filterToHeroAndExpanded(trades, heroes, expanded)).toEqual(want);
  });
});
