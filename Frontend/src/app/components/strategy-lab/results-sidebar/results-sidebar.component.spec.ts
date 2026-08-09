import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { ResultsSidebarComponent } from "./results-sidebar.component";

function run(): BacktestRunDetail {
  return {
    id: 122,
    engine: "PYTHON",
    source: "engine",
    requestedEngine: "python",
    strategyName: "ema_crossover_signal",
    symbol: "SPY",
    leanRunId: null,
    parameters: "{}",
    startDate: "2026-05-11",
    endDate: "2026-08-07",
    fillMode: "signal_bar_close",
    executedAt: 1,
    durationMs: 2,
    totalTrades: 15,
    winningTrades: 7,
    losingTrades: 8,
    winRate: 7 / 15,
    totalPnL: -1714.92,
    initialCash: 100_000,
    commissionPerOrder: 1,
    finalEquity: 98_285.08,
    totalFees: 30,
    maxDrawdown: 0.0256,
    sharpeRatio: -2.03,
    sortinoRatio: -2.32,
    profitFactor: 0.51,
    leanStatisticsJson: null,
    leanAnalysisJson: null,
    verdictJson: JSON.stringify({
      verdict_version: 1,
      engine: "python",
      generated_at_ms: 1,
      composite: 41,
      grade: "C",
      signal: "Rework",
      headline: "Needs work.",
      red_flags: [],
      dimensions: [{
        key: "return_quality",
        label: "Return quality",
        weight: 0.2,
        score: 8,
        summary: "Mixed.",
        sub_scores: [
          { key: "sharpe", label: "Sharpe", score: 0, raw_value: -2.03, display: "−2.03", note: "Below target." },
          { key: "cagr", label: "CAGR", score: 17, raw_value: 0.12, display: "12.00%", note: "Above target." },
          { key: "calmar", label: "Calmar", score: 8, raw_value: 0.8, display: "0.80", note: "Borderline." },
          { key: "benchmark", label: "Benchmark", score: null, raw_value: null, display: "Unavailable", note: "Not recorded." },
        ],
      }],
      missing_metrics: [],
      normalized_weights: false,
      cleanliness: null,
    }),
    verdictVersion: 1,
    verdictGrade: "C",
    verdictSignal: "Rework",
    equityCurve: null,
    validationAnalytics: null,
    dataPolicy: null,
    insightSummaryJson: null,
    parityGroupId: "pair-1",
    trades: [],
    tradesTruncated: false,
    parityVerdicts: [{
      id: 1,
      status: "diverged",
      createdAt: 10,
      verdictJson: JSON.stringify({ divergences: [{ category: "trade_count", message: "Counts differ." }] }),
    }],
  };
}

describe("ResultsSidebarComponent", () => {
  it("renders an ordered, producer-colored list without repeating headline KPIs", async () => {
    await TestBed.configureTestingModule({
      imports: [ResultsSidebarComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const fixture = TestBed.createComponent(ResultsSidebarComponent);
    fixture.componentRef.setInput("run", run());
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector("ol.results-sidebar__metrics")).not.toBeNull();
    expect(root.textContent).toContain("CAGR");
    expect(root.textContent).toContain("Calmar");
    expect(root.textContent).not.toContain("Sharpe");
    expect(root.textContent).not.toContain("Benchmark");
    expect(root.querySelector("[data-band='positive']")?.textContent).toContain("CAGR");
    expect(root.querySelector("[data-band='warning']")?.textContent).toContain("Calmar");
    expect(root.textContent).toContain("Compatibility");
    expect(root.textContent).toContain("Diverged");
    expect(root.textContent).not.toContain("Counts differ");
  });
});
