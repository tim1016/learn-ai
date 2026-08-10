import { Component, computed, input, provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Apollo } from "apollo-angular";
import { from } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail, BacktestRunDetailTrade } from "../../../graphql/backtest-runs.query";
import { StrategyLabChartComponent } from "../../strategy-lab/strategy-lab-chart/strategy-lab-chart.component";
import { parseLeanAnalysis, RunReportComponent, toEngineTrade } from "./run-report.component";

@Component({
  selector: "app-strategy-lab-chart",
  template: `
    <div data-testid="strategy-chart">Shared-time-scale strategy chart</div>
    <output data-testid="strategy-chart-markers">{{ markersJson() }}</output>
    <output data-testid="strategy-chart-equity">{{ equityPointsJson() }}</output>
  `,
})
class StrategyLabChartStubComponent {
  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<unknown[]>([]);
  readonly equityPoints = input<unknown[]>([]);
  readonly markersJson = computed(() => JSON.stringify(this.markers()));
  readonly equityPointsJson = computed(() => JSON.stringify(this.equityPoints()));
}

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

let watchQueryMock: ReturnType<typeof vi.fn>;

async function renderReport(
  run: BacktestRunDetail,
  supplied = false,
  queryResult: unknown = { data: { backtestRun: run }, loading: false },
): Promise<ComponentFixture<RunReportComponent>> {
  watchQueryMock = vi.fn(() => ({
    valueChanges: from([queryResult]),
    stopPolling: vi.fn(),
  }));
  await TestBed.configureTestingModule({
    imports: [RunReportComponent],
    providers: [provideZonelessChangeDetection(), { provide: Apollo, useValue: { watchQuery: watchQueryMock } }],
  }).overrideComponent(RunReportComponent, {
    remove: { imports: [StrategyLabChartComponent] },
    add: { imports: [StrategyLabChartStubComponent] },
  }).compileComponents();

  const fixture = TestBed.createComponent(RunReportComponent);
  fixture.componentRef.setInput("runId", run.id);
  if (supplied) fixture.componentRef.setInput("runDetail", run);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe("RunReportComponent", () => {
  it("renders the grade-led KPI row with its statistics and evidence rail", async () => {
    const fixture = await renderReport(makeRun());
    const root = fixture.nativeElement as HTMLElement;
    const text = root.textContent ?? "";

    expect(text).toContain("Backtest Evidence Grade");
    expect(text).toContain("no grade is inferred");
    expect(text).toContain("Shared-time-scale strategy chart");
    expect(text).not.toContain("Back to workbench");
    expect(text).not.toContain("Run configuration");
    expect(text).toContain("More statistics");
    expect(text).toContain("Validation atlas");
    expect(text).not.toContain("Diagnostic complete");
    expect(root.querySelector("app-strategy-lab-verdict")).toBeNull();
  });

  it("uses the producer-authored realized staircase instead of mark-to-market points", async () => {
    const fixture = await renderReport(makeRun(), true);
    const root = fixture.nativeElement as HTMLElement;

    expect(watchQueryMock).not.toHaveBeenCalled();
    expect(root.querySelector("[data-testid='strategy-chart-equity']")?.textContent).toBe(JSON.stringify([
      { timeMs: Date.UTC(2026, 0, 5, 15, 0), value: 100_000 },
      { timeMs: makeTrade().exitTimestamp, value: 100_048 },
      { timeMs: Date.UTC(2026, 0, 6, 21, 0), value: 100_048 },
    ]));
  });

  it("does not mislabel a report-query failure as a missing run", async () => {
    const fixture = await renderReport(makeRun(), false, {
      data: undefined,
      loading: false,
      error: new Error("Backtest detail query is incompatible with the server."),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text).toContain("Run report could not be loaded.");
    expect(text).not.toContain("Run not found.");
  });

  it("keeps buy and sell markers tied to persisted trade outcomes", async () => {
    const fixture = await renderReport(makeRun({ trades: [makeTrade({ pnL: 0, pnlPts: 0, pnlPct: 0 })] }));
    const markers = (fixture.nativeElement as HTMLElement)
      .querySelector("[data-testid='strategy-chart-markers']")?.textContent;

    expect(markers).toContain('"color":"#90a4ae"');
    expect(markers).toContain("BUY · BREAK EVEN");
    expect(markers).toContain("SELL · $0.00");
  });

  it("makes a missing realized curve explicit instead of relabeling mark-to-market evidence", async () => {
    const fixture = await renderReport(makeRun({
      equityCurve: {
        schemaVersion: 2,
        error: null,
        markToMarket: curve([{ t: 1, e: 100 }], "strategy_bar_close"),
        realized: { cadence: "trade_exit", rawPoints: 0, keptPoints: 0, error: "Realized equity unavailable.", points: [] },
      },
    }));

    expect((fixture.nativeElement as HTMLElement).textContent).toContain("Realized equity unavailable.");
    expect((fixture.nativeElement as HTMLElement)
      .querySelector("[data-testid='strategy-chart-equity']")?.textContent).toBe("[]");
  });

  it("renders a notice for a structurally malformed persisted verdict", async () => {
    const fixture = await renderReport(makeRun({ verdictJson: "{}" }));

    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      "Persisted verdict data is incomplete or malformed.",
    );
    expect(fixture.componentInstance.verdict()).toBeNull();
  });

  it("preserves every valid native LEAN analysis finding and arbitrary sample shape", () => {
    const parsed = parseLeanAnalysis(JSON.stringify([{
      name: "FlatEquityCurveAnalysis",
      issue: "The curve is flat.",
      sample: [{ start: 1_700_000_000_000, end: 1_700_086_400_000 }],
      solutions: ["Check warm-up."],
    }]));

    expect(parsed).toEqual([expect.objectContaining({ name: "FlatEquityCurveAnalysis", solutions: ["Check warm-up."] })]);
  });
});

describe("toEngineTrade", () => {
  it("passes through persisted P&L fields without recomputing them", () => {
    const trade = toEngineTrade(makeTrade({ pnlPts: 7, pnlPct: 0.02, pnL: -1.9 }), 4);
    expect(trade).toEqual(expect.objectContaining({ trade_number: 5, pnl_pts: 7, pnl_pct: 0.02, result: "LOSS" }));
  });
});
