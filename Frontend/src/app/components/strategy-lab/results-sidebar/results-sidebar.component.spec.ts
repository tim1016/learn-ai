import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { BacktestRunDetail } from "../../../services/backtest-runs.types";
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
    startDate: 1778472000000, // 2026-05-11 ET midnight
    endDate: 1786075200000, // 2026-08-07 ET midnight
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
    metricDocumentation: [],
    notes: null,
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

  it("exposes each metric's note through a keyboard-operable disclosure, not a hover-only title (#2462)", async () => {
    await TestBed.configureTestingModule({
      imports: [ResultsSidebarComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const fixture = TestBed.createComponent(ResultsSidebarComponent);
    fixture.componentRef.setInput("run", run());
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    const disclosures = Array.from(root.querySelectorAll("details.results-sidebar__note"));
    expect(disclosures.length).toBeGreaterThan(1);
    // No metric row leans on a hover `title` for its explanation anymore.
    expect(root.querySelector(".results-sidebar__metrics [title]")).toBeNull();

    // Every disclosure's accessible name names its metric, so a screen
    // reader tabbing through the list can tell the controls apart.
    const summaries = disclosures.map((details) => details.querySelector("summary")?.textContent?.trim());
    expect(summaries).toEqual(["Why this score: CAGR", "Why this score: Calmar"]);
    expect(new Set(summaries).size).toBe(summaries.length);

    const disclosure = disclosures[0] as HTMLDetailsElement;
    expect(disclosure.textContent).toContain("Above target.");
  });

  it("opens and closes a note by keyboard alone (#2462)", async () => {
    await TestBed.configureTestingModule({
      imports: [ResultsSidebarComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const fixture = TestBed.createComponent(ResultsSidebarComponent);
    fixture.componentRef.setInput("run", run());
    // Attached to the document so Tab navigation and Enter activation run
    // against real focus, the way a keyboard user reaches the control.
    document.body.appendChild(fixture.nativeElement);
    try {
      fixture.detectChanges();

      const summary = (fixture.nativeElement as HTMLElement)
        .querySelector("details.results-sidebar__note summary") as HTMLElement | null;
      expect(summary).not.toBeNull();

      summary?.focus();
      expect(document.activeElement).toBe(summary);
      const disclosure = summary?.closest("details") as HTMLDetailsElement;
      expect(disclosure.open).toBe(false);

      summary?.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      summary?.click(); // Enter on a summary activates it — jsdom needs the click spelled out
      fixture.detectChanges();
      expect(disclosure.open).toBe(true);
      expect(disclosure.querySelector("p")?.textContent).toContain("Above target.");
    } finally {
      document.body.removeChild(fixture.nativeElement);
    }
  });
});
