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
