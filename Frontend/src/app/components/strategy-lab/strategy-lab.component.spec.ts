import { describe, expect, it } from "vitest";

import type { BacktestRunDetail } from "../../graphql/backtest-runs.query";
import { toStrategyLabConfiguration } from "./strategy-lab.component";

function run(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  return {
    id: 91,
    engine: "PYTHON",
    source: "engine",
    requestedEngine: "both",
    strategyName: "ema_crossover_signal",
    symbol: "QQQ",
    leanRunId: null,
    parameters: JSON.stringify({ fast: 8, slow: 21, symbol: "QQQ" }),
    startDate: "2026-03-02",
    endDate: "2026-04-02",
    fillMode: "next_bar_open",
    executedAt: 1,
    durationMs: 2,
    totalTrades: 3,
    winningTrades: 2,
    losingTrades: 1,
    winRate: 2 / 3,
    totalPnL: 120,
    initialCash: 75_000,
    commissionPerOrder: 0.35,
    finalEquity: 75_120,
    totalFees: 2.1,
    maxDrawdown: 0.02,
    sharpeRatio: 1.1,
    sortinoRatio: 1.4,
    profitFactor: 2,
    leanStatisticsJson: null,
    verdictJson: null,
    verdictVersion: null,
    verdictGrade: null,
    verdictSignal: null,
    equityCurve: null,
    validationAnalytics: null,
    dataPolicy: {
      source: "polygon",
      symbol: "QQQ",
      adjusted: true,
      session: "regular",
      input_bars: { timespan: "minute", multiplier: 1 },
      strategy_bars: { timespan: "minute", multiplier: 15 },
      timestamp_policy: "bar_close_ms_utc",
      timezone: "America/New_York",
      provider_kind: "live",
      fixture_id: null,
      fixture_sha256: null,
    },
    insightSummaryJson: null,
    parityGroupId: null,
    trades: [],
    parityVerdicts: [],
    ...overrides,
  };
}

describe("Strategy Lab History rehydration", () => {
  it("restores every persisted control without inferring away the Both selection", () => {
    const configuration = toStrategyLabConfiguration(run(), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "daily",
      autoFetch: false,
    });

    expect(configuration).toEqual({
      engine: "both",
      range: {
        symbol: "QQQ",
        from: "2026-03-02",
        to: "2026-04-02",
        resolution: "minute",
        autoFetch: true,
      },
      parameters: { fast: 8, slow: 21, symbol: "QQQ" },
      fillMode: "next_bar_open",
      initialCash: 75_000,
      commissionPerOrder: 0.35,
    });
  });

  it("uses producing engine only as a legacy-row fallback", () => {
    expect(toStrategyLabConfiguration(run({ requestedEngine: null, engine: "LEAN", source: "lean-sidecar" }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "minute",
    }).engine).toBe("lean");
  });
});
