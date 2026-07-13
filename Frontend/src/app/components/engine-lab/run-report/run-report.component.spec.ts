import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail, BacktestRunDetailTrade } from "../../../graphql/backtest-runs.query";
import { RunReportComponent, toEngineTrade } from "./run-report.component";

function makeTrade(overrides: Partial<BacktestRunDetailTrade> = {}): BacktestRunDetailTrade {
  return {
    id: 1,
    entryTimestamp: Date.UTC(2026, 0, 5, 15, 0),
    exitTimestamp: Date.UTC(2026, 0, 5, 16, 15),
    entryPrice: 500,
    exitPrice: 505,
    quantity: 10,
    pnL: 48,
    signalReason: "crossover",
    isSyntheticExit: false,
    ...overrides,
  };
}

function makeRun(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  return {
    id: 44,
    engine: "PYTHON",
    source: "engine",
    strategyName: "spy_ema_crossover",
    symbol: "SPY",
    leanRunId: null,
    startDate: "2026-01-05",
    endDate: "2026-01-06",
    fillMode: "signal_bar_close",
    executedAt: Date.UTC(2026, 0, 6, 21, 0),
    durationMs: 1200,
    totalTrades: 1,
    winningTrades: 1,
    losingTrades: 0,
    winRate: 1,
    totalPnL: 48,
    initialCash: 100_000,
    finalEquity: 100_048,
    totalFees: 2,
    maxDrawdown: 0.01,
    sharpeRatio: 1.2,
    sortinoRatio: 1.4,
    profitFactor: 2.1,
    leanStatisticsJson: null,
    verdictJson: JSON.stringify({
      verdict_version: 1,
      engine: "python",
      generated_at_ms: 1,
      composite: 72,
      grade: "B",
      signal: "Iterate",
      headline: "Profitable but thin sample.",
      red_flags: [],
      dimensions: [],
      missing_metrics: [],
      normalized_weights: false,
      cleanliness: null,
    }),
    verdictVersion: 1,
    verdictGrade: "B",
    verdictSignal: "Iterate",
    equityCurve: {
      cadence: "strategy_bar_close",
      rawPoints: 2,
      keptPoints: 2,
      error: null,
      points: [
        { t: Date.UTC(2026, 0, 5, 15, 0), e: 100_000 },
        { t: Date.UTC(2026, 0, 6, 21, 0), e: 100_048 },
      ],
    },
    validationAnalytics: null,
    dataPolicy: {
      source: "polygon",
      symbol: "SPY",
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
    trades: [makeTrade()],
    parityVerdicts: [],
    ...overrides,
  };
}

async function renderReport(run: BacktestRunDetail | null): Promise<{
  fixture: ComponentFixture<RunReportComponent>;
  httpMock: HttpTestingController;
}> {
  const apolloMock = {
    watchQuery: vi.fn(() => ({
      valueChanges: of({ data: { backtestRun: run } }),
      stopPolling: vi.fn(),
    })),
  };
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [RunReportComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: Apollo, useValue: apolloMock },
    ],
  }).compileComponents();

  const fixture = TestBed.createComponent(RunReportComponent);
  fixture.componentRef.setInput("runId", 44);
  fixture.detectChanges();
  // Flush effects/resources without awaiting app stability — a pending
  // bars request (deliberately unflushed until the test inspects it)
  // would otherwise keep the app unstable forever.
  await Promise.resolve();
  TestBed.tick();
  fixture.detectChanges();
  return { fixture, httpMock: TestBed.inject(HttpTestingController) };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RunReportComponent", () => {
  it("renders the persisted run's headline and verdict from GraphQL", async () => {
    const { fixture, httpMock } = await renderReport(makeRun());
    httpMock.expectOne((req) => req.url.includes("/api/engine/bars")).flush({
      policy_key: "polygon-adjusted",
      symbol: "SPY",
      count: 0,
      bars: [],
      coverage: { expected_days: 2, available_days: 2, is_complete: true, missing_days: [] },
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("spy_ema_crossover / SPY");
    expect(text).toContain("run #44");
    expect(text).toContain("Profitable but thin sample.");
    expect(text).toContain("Grade B");
  });

  it("requests bars using the persisted DataPolicy dimensions", async () => {
    const { httpMock } = await renderReport(makeRun());

    const req = httpMock.expectOne((r) => r.url.includes("/api/engine/bars"));
    expect(req.request.params.get("symbol")).toBe("SPY");
    expect(req.request.params.get("from_date")).toBe("2026-01-05");
    expect(req.request.params.get("to_date")).toBe("2026-01-06");
    expect(req.request.params.get("adjusted")).toBe("true");
    expect(req.request.params.get("session")).toBe("regular");
    expect(req.request.params.get("timespan")).toBe("minute");
    expect(req.request.params.get("multiplier")).toBe("15");
    req.flush({
      policy_key: "polygon-adjusted",
      symbol: "SPY",
      count: 0,
      bars: [],
      coverage: { expected_days: 2, available_days: 2, is_complete: true, missing_days: [] },
    });
  });

  it("surfaces partial bar-store coverage as an honest notice", async () => {
    const { fixture, httpMock } = await renderReport(makeRun());
    httpMock.expectOne((req) => req.url.includes("/api/engine/bars")).flush({
      policy_key: "polygon-adjusted",
      symbol: "SPY",
      count: 26,
      bars: [],
      coverage: { expected_days: 2, available_days: 1, is_complete: false, missing_days: ["2026-01-06"] },
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Bar store covers 1 of 2 weekdays");
  });

  it("skips the bars fetch and explains why when the run has no data policy", async () => {
    const { fixture, httpMock } = await renderReport(makeRun({ dataPolicy: null }));
    httpMock.expectNone((req) => req.url.includes("/api/engine/bars"));
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("no recorded data policy");
  });

  it("reports missing validation analytics honestly for legacy rows", async () => {
    const { fixture, httpMock } = await renderReport(makeRun({ validationAnalytics: null }));
    httpMock.expectOne((req) => req.url.includes("/api/engine/bars")).flush({
      policy_key: "polygon-adjusted",
      symbol: "SPY",
      count: 0,
      bars: [],
      coverage: { expected_days: 2, available_days: 2, is_complete: true, missing_days: [] },
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Validation analytics not recorded for this run.");
  });

  it("renders 'Run not found' when the id does not resolve", async () => {
    const { fixture } = await renderReport(null);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Run not found.");
  });

  async function renderWithParity(verdict: {
    status: string;
    verdictJson: string;
  }): Promise<ComponentFixture<RunReportComponent>> {
    const { fixture, httpMock } = await renderReport(
      makeRun({ parityVerdicts: [{ id: 1, createdAt: Date.UTC(2026, 0, 6, 21, 5), ...verdict }] }),
    );
    httpMock.expectOne((req) => req.url.includes("/api/engine/bars")).flush({
      policy_key: "polygon-adjusted",
      symbol: "SPY",
      count: 0,
      bars: [],
      coverage: { expected_days: 2, available_days: 2, is_complete: true, missing_days: [] },
    });
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it("shows the pending parity state while the LEAN companion runs", async () => {
    const fixture = await renderWithParity({
      status: "pending",
      verdictJson: JSON.stringify({ schema_version: 1, status: "pending", reason: null }),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("LEAN validating companion is running");
  });

  it("shows divergence categories when the engines disagree", async () => {
    const fixture = await renderWithParity({
      status: "diverged",
      verdictJson: JSON.stringify({
        schema_version: 1,
        status: "diverged",
        counts_by_category: { FILL_PRICE_DRIFT: 2 },
        divergences: [
          { category: "FILL_PRICE_DRIFT", trade_number: 1, ms_utc: 1, message: "fill differs by $0.03" },
          { category: "FILL_PRICE_DRIFT", trade_number: 2, ms_utc: 2, message: "fill differs by $0.02" },
        ],
      }),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("The engines disagree");
    expect(text).toContain("2");
    expect(text).toContain("Show 2 divergence details");
  });

  it("explains honest unavailability with trader copy", async () => {
    const fixture = await renderWithParity({
      status: "unavailable",
      verdictJson: JSON.stringify({ schema_version: 1, status: "unavailable", reason: "no_lean_counterpart" }),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("No LEAN counterpart is registered for this strategy.");
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Persisted-trade adapter — regression coverage moved from the deleted
// lean-engine mapStudyTradeToEngineTrade helper. pnl_pct must be derived
// as pnl_pts / entryPrice (the engine's convention); the .NET
// BacktestTrade entity does not persist a percent column.
// ─────────────────────────────────────────────────────────────────────────
describe("toEngineTrade", () => {
  it("derives pnl_pct from price delta over entry price", () => {
    const trade = toEngineTrade(makeTrade({ entryPrice: 500, exitPrice: 505 }), 0);
    expect(trade.pnl_pts).toBe(5);
    expect(trade.pnl_pct).toBeCloseTo(0.01, 10);
  });

  it("classifies WIN/LOSS from the persisted dollar PnL, not the price delta", () => {
    // Fees can turn a positive price delta into a losing trade.
    const trade = toEngineTrade(makeTrade({ entryPrice: 500, exitPrice: 500.01, pnL: -1.9 }), 0);
    expect(trade.result).toBe("LOSS");
  });

  it("falls back to 0 pct when entry price is zero to avoid NaN/Infinity", () => {
    const trade = toEngineTrade(makeTrade({ entryPrice: 0, exitPrice: 5 }), 0);
    expect(trade.pnl_pct).toBe(0);
  });

  it("assigns a 1-based trade number and passes fields through", () => {
    const trade = toEngineTrade(makeTrade({ quantity: 3, signalReason: "rsi_oversold" }), 4);
    expect(trade.trade_number).toBe(5);
    expect(trade.quantity).toBe(3);
    expect(trade.signal_reason).toBe("rsi_oversold");
    expect(trade.indicators).toEqual({});
  });
});
