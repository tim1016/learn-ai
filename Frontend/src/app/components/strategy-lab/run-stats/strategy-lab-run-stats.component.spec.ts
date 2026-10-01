import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import { makeRun } from "../testing/run-fixtures";
import { StrategyLabRunStatsComponent } from "./strategy-lab-run-stats.component";

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

  it("never renders the retired results-page framing", async () => {
    const { container } = await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun(), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(container.textContent).not.toContain("Back to workbench");
    expect(container.querySelector("app-strategy-lab-chart")).toBeNull();
  });

  it("says a $0-fee Python run's fees were not charged (#2601)", async () => {
    await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun({ requestedEngine: "python", commissionPerOrder: 0, totalFees: 0 }), result: { ...makeResult(), total_fees: 0 }, verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText("Not charged")).toBeTruthy();
  });

  it("prices a LEAN run's own per-fill fees although it records no flat fee", async () => {
    await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun({ source: "lean-sidecar", engine: "LEAN", commissionPerOrder: 0 }), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.queryByText("Not charged")).toBeNull();
    expect(screen.getByText("$2.00")).toBeTruthy();
  });

  it("prices a paired run saved before #2465 that recorded the rail's $0 but paid IBKR fees", async () => {
    await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun({ requestedEngine: "both", commissionPerOrder: 0, totalFees: 2 }), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.queryByText("Not charged")).toBeNull();
    expect(screen.getByText("$2.00")).toBeTruthy();
  });

  it("never calls a paired run's fees not charged, even when it traded nothing", async () => {
    await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun({ requestedEngine: "both", commissionPerOrder: 0, totalFees: 0, totalTrades: 0 }), result: { ...makeResult(), total_fees: 0, total_trades: 0 }, verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.queryByText("Not charged")).toBeNull();
    expect(screen.getByText("$0.00")).toBeTruthy();
  });
});
