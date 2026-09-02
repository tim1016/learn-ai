/** Test-only builders for `BacktestRunDetail` fixtures; not for production use. */

import type { BacktestRunDetail, BacktestRunDetailTrade } from "../../../graphql/backtest-runs.query";

export function makeTrade(overrides: Partial<BacktestRunDetailTrade> = {}): BacktestRunDetailTrade {
  return {
    id: 1,
    entryTimestamp: Date.UTC(2026, 0, 5, 15, 0),
    exitTimestamp: Date.UTC(2026, 0, 5, 16, 15),
    entryPrice: 500,
    exitPrice: 505,
    quantity: 10,
    pnL: 48,
    pnlPts: 5,
    pnlPct: 0.01,
    signalReason: "crossover",
    isSyntheticExit: false,
    ...overrides,
  };
}

export function curve(points: { t: number; e: number }[], cadence: string) {
  return { cadence, rawPoints: points.length, keptPoints: points.length, error: null, points };
}

export function makeRun(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  const start = Date.UTC(2026, 0, 5, 15, 0);
  const end = Date.UTC(2026, 0, 6, 21, 0);
  return {
    id: 44,
    engine: "PYTHON",
    source: "engine",
    requestedEngine: "both",
    strategyName: "spy_ema_crossover",
    symbol: "SPY",
    leanRunId: null,
    parameters: JSON.stringify({ short: 5, long: 10, symbol: "SPY" }),
    startDate: "2026-01-05",
    endDate: "2026-01-06",
    fillMode: "signal_bar_close",
    executedAt: end,
    durationMs: 1200,
    totalTrades: 1,
    winningTrades: 1,
    losingTrades: 0,
    winRate: 1,
    totalPnL: 48,
    initialCash: 100_000,
    commissionPerOrder: 1,
    finalEquity: 100_048,
    totalFees: 2,
    maxDrawdown: 0.01,
    sharpeRatio: 1.2,
    sortinoRatio: 1.4,
    profitFactor: 2.1,
    leanStatisticsJson: null,
    leanAnalysisJson: null,
    verdictJson: JSON.stringify({
      verdict_version: 1, engine: "python", generated_at_ms: 1, composite: 72,
      grade: "B", signal: "Iterate", headline: "Profitable but thin sample.",
      red_flags: [], dimensions: [], missing_metrics: [], normalized_weights: false, cleanliness: null,
    }),
    verdictVersion: 1,
    verdictGrade: "B",
    verdictSignal: "Iterate",
    equityCurve: {
      schemaVersion: 2,
      error: null,
      markToMarket: curve([{ t: start, e: 100_000 }, { t: end, e: 100_048 }], "strategy_bar_close"),
      realized: curve([{ t: start, e: 100_000 }, { t: makeTrade().exitTimestamp, e: 100_048 }, { t: end, e: 100_048 }], "trade_exit"),
    },
    validationAnalytics: null,
    dataPolicy: {
      source: "polygon", symbol: "SPY", adjusted: true, session: "regular",
      input_bars: { timespan: "minute", multiplier: 1 },
      strategy_bars: { timespan: "minute", multiplier: 15 },
      timestamp_policy: "bar_close_ms_utc", timezone: "America/New_York",
      provider_kind: "live", fixture_id: null, fixture_sha256: null,
    },
    insightSummaryJson: null,
    parityGroupId: null,
    trades: [makeTrade()],
    tradesTruncated: false,
    parityVerdicts: [],
    ...overrides,
  };
}
