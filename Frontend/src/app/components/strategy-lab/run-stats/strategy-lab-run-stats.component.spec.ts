import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import type { BacktestRunDetail, BacktestRunDetailTrade } from "../../../graphql/backtest-runs.query";
import { StrategyLabRunStatsComponent } from "./strategy-lab-run-stats.component";

// Reuse the makeRun builder shape from strategy-lab-run-report.service.spec.ts.
// Keep it local to this file — test builders are not shared infrastructure here.
function makeTrade(overrides: Partial<BacktestRunDetailTrade> = {}): BacktestRunDetailTrade {
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

function curve(points: { t: number; e: number }[], cadence: string) {
  return { cadence, rawPoints: points.length, keptPoints: points.length, error: null, points };
}

function makeRun(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
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

function makeResult() {
  return {
    success: true, strategy_name: "spy_ema_crossover", fill_mode: "signal_bar_close",
    initial_cash: 100_000, final_equity: 100_048, net_profit: 48, total_fees: 2,
    total_trades: 1, winning_trades: 1, losing_trades: 0, win_rate: 1,
    statistics: { max_drawdown_pct: 0.01, sharpe_ratio: 1.2, sortino_ratio: 1.4, profit_factor: 2.1, expectancy_pct: null },
    lean_statistics: null, lean_analysis: [], trades: [], log_lines: [], validation_analytics: null,
  };
}

describe("StrategyLabRunStatsComponent", () => {
  it("stacks the grade, headline metrics, supplementary statistics and evidence menu", async () => {
    await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun(), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText("Backtest Evidence Grade")).toBeTruthy();
    expect(screen.getByText("Returns")).toBeTruthy();
    expect(screen.getByText("More statistics")).toBeTruthy();
    expect(screen.getByText("Validation atlas")).toBeTruthy();
  });

  it("never renders the retired results-page framing", async () => {
    const { container } = await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun(), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(container.textContent).not.toContain("Back to workbench");
    expect(container.querySelector("app-strategy-lab-chart")).toBeNull();
  });
});
