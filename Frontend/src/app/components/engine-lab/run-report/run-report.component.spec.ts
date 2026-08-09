import { provideZonelessChangeDetection } from "@angular/core";
import { Component, input } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Apollo } from "apollo-angular";
import { concat, from, NEVER, type Observable, of, throwError } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail, BacktestRunDetailTrade } from "../../../graphql/backtest-runs.query";
import { StrategyLabChartComponent } from "../../strategy-lab/strategy-lab-chart/strategy-lab-chart.component";
import { parseLeanAnalysis, RunReportComponent, toEngineTrade } from "./run-report.component";

@Component({
  selector: "app-strategy-lab-chart",
  template: "<div data-testid=\"strategy-chart\">Synced strategy chart</div>",
})
class StrategyLabChartStubComponent {
  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<unknown[]>([]);
  readonly equityPoints = input<unknown[]>([]);
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

function makeRun(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
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
    executedAt: Date.UTC(2026, 0, 6, 21, 0),
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
    tradesTruncated: false,
    parityVerdicts: [],
    ...overrides,
  };
}

let watchQueryMock: ReturnType<typeof vi.fn>;

interface WatchResult {
  data?: { backtestRun?: BacktestRunDetail | null };
  loading?: boolean;
}

async function renderReport(
  run: BacktestRunDetail | null,
  supplyRunDetail = false,
  watchedRuns: (BacktestRunDetail | null)[] = [run],
  valueChanges?: Observable<WatchResult>,
): Promise<ComponentFixture<RunReportComponent>> {
  watchQueryMock = vi.fn(() => ({
    valueChanges: valueChanges ?? from(
      watchedRuns.map((backtestRun) => ({ data: { backtestRun } })),
    ),
    stopPolling: vi.fn(),
  }));
  const apolloMock = {
    watchQuery: watchQueryMock,
  };
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [RunReportComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: Apollo, useValue: apolloMock },
    ],
  })
    .overrideComponent(RunReportComponent, {
      remove: { imports: [StrategyLabChartComponent] },
      add: { imports: [StrategyLabChartStubComponent] },
    })
    .compileComponents();

  const fixture = TestBed.createComponent(RunReportComponent);
  fixture.componentRef.setInput("runId", 44);
  if (supplyRunDetail && run) fixture.componentRef.setInput("runDetail", run);
  fixture.detectChanges();
  await Promise.resolve();
  TestBed.tick();
  fixture.detectChanges();
  return fixture;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RunReportComponent", () => {
  it("preserves every valid native LEAN analysis finding and arbitrary sample shape", () => {
    const parsed = parseLeanAnalysis(JSON.stringify([
      {
        name: "FlatEquityCurveAnalysis",
        issue: "The curve is flat.",
        sample: [{ start: 1_700_000_000_000, end: 1_700_086_400_000, trading_days: 2 }],
        solutions: ["Check warm-up.", "Check subscriptions."],
      },
      {
        name: "StatisticalSignificanceOfDailyReturnsAnalysis",
        issue: "The p-value is above 0.05.",
        sample: { pValue: "0.0684907141208504" },
        solutions: ["Review the trading rules."],
      },
    ]));

    expect(parsed).toHaveLength(2);
    expect(parsed[0].solutions).toEqual(["Check warm-up.", "Check subscriptions."]);
    expect(parsed[1].sample).toEqual({ pValue: "0.0684907141208504" });
  });

  it("renders the persisted verdict and condensed metrics without restating configuration", async () => {
    const fixture = await renderReport(makeRun());
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Diagnostic complete");
    expect(text).toContain("B · 72");
    expect(text).toContain("Net P&L");
    expect(text).toContain("Synced strategy chart");
    expect(text).not.toContain("spy_ema_crossover / SPY");
    expect(text).not.toContain("run #44");
    expect([...((fixture.nativeElement as HTMLElement).querySelectorAll("details"))]
      .every((detail) => !detail.open)).toBe(true);
    expect(fixture.componentInstance.markers()).toEqual([
      expect.objectContaining({ color: "#26a69a", text: "BUY · WIN" }),
      expect.objectContaining({ color: "#26a69a", text: "SELL · +$48.00" }),
    ]);
  });

  it("renders a zero-PnL trade as neutral break-even chart evidence", async () => {
    const fixture = await renderReport(makeRun({
      winningTrades: 0,
      losingTrades: 0,
      trades: [makeTrade({ pnL: 0, pnlPts: 0, pnlPct: 0 })],
    }));

    expect(fixture.componentInstance.markers()).toEqual([
      expect.objectContaining({ color: "#90a4ae", text: "BUY · BREAK EVEN" }),
      expect.objectContaining({ color: "#90a4ae", text: "SELL · $0.00" }),
    ]);
  });

  it("uses a history-selected run directly instead of querying the same row twice", async () => {
    const fixture = await renderReport(makeRun(), true);

    expect(watchQueryMock).not.toHaveBeenCalled();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain("Diagnostic complete");
  });

  it("keeps polling a supplied pending run and renders the terminal parity update", async () => {
    const pending = makeRun({
      parityVerdicts: [{ id: 1, status: "pending", verdictJson: "{}", createdAt: 1 }],
    });
    const terminal = makeRun({
      parityVerdicts: [{ id: 1, status: "matched", verdictJson: JSON.stringify({ schema_version: 1, status: "matched" }), createdAt: 2 }],
    });

    const fixture = await renderReport(pending, true, [pending, terminal]);
    fixture.detectChanges();

    expect(watchQueryMock).toHaveBeenCalledOnce();
    expect(fixture.componentInstance.run()?.parityVerdicts[0].status).toBe("matched");
  });

  it("keeps the loading state while Apollo has no network result", async () => {
    const fixture = await renderReport(
      null,
      false,
      [],
      concat(of({ loading: true, data: undefined }), NEVER),
    );
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Loading run…");
    expect(text).not.toContain("Run not found.");
    expect(watchQueryMock).toHaveBeenCalledWith(expect.objectContaining({
      fetchPolicy: "network-only",
    }));
  });

  it("labels large ledgers as bounded evidence while retaining full-run metrics", async () => {
    const fixture = await renderReport(makeRun({
      totalTrades: 19_861,
      tradesTruncated: true,
    }));

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("most recent 1 of 19,861 trades");
    expect(text).toContain("Headline metrics use the complete persisted run.");
    expect(text).toContain("1 recent of 19861 completed trades");
    expect(text).toContain("Loaded 1");
    expect(text).not.toContain("All 1");
  });

  it("does not mislabel transport failures as missing runs", async () => {
    const fixture = await renderReport(
      null,
      false,
      [],
      throwError(() => new Error("gateway timeout")),
    );
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Run report could not be loaded.");
    expect(text).not.toContain("Run not found.");
  });

  it("reports missing validation analytics honestly for legacy rows", async () => {
    const fixture = await renderReport(makeRun({ validationAnalytics: null }));
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Validation analytics were not recorded for this run.");
  });

  it("explains the native LEAN chart cadence without treating it as a KPI source", async () => {
    const fixture = await renderReport(makeRun({ source: "lean-sidecar", engine: "LEAN" }));
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("native Strategy Equity chart");
    expect(text).toContain("can be sparse or end before the terminal portfolio value");
    expect(text).toContain("never from interpolating this chart");
  });

  it("keeps native LEAN findings available in a collapsed deep dive", async () => {
    const fixture = await renderReport(makeRun({
      leanAnalysisJson: JSON.stringify([{
        name: "FlatEquityCurveAnalysis",
        issue: "The curve is flat.",
        sample: { trading_days: 2 },
        solutions: ["Check subscriptions."],
      }]),
    }));
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("LEAN native analysis");
    expect(root.textContent).toContain("Flat Equity Curve");
    expect(root.textContent).toContain("Check subscriptions.");
    expect([...root.querySelectorAll("details")].every((detail) => !detail.open)).toBe(true);
  });

  it("renders 'Run not found' when the id does not resolve", async () => {
    const fixture = await renderReport(null);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Run not found.");
  });

  async function renderWithParity(verdict: {
    status: string;
    verdictJson: string;
  }): Promise<ComponentFixture<RunReportComponent>> {
    const fixture = await renderReport(
      makeRun({ parityVerdicts: [{ id: 1, createdAt: Date.UTC(2026, 0, 6, 21, 5), ...verdict }] }),
    );
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
    expect(text).toContain("parity: Pending");
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
    const root = fixture.nativeElement as HTMLElement;
    const parityButton = root.querySelector<HTMLButtonElement>(".verdict-line__parity");
    expect(parityButton?.textContent).toContain("Diverged");
    parityButton?.click();
    fixture.detectChanges();
    expect(root.textContent).toContain("Fill Price Drift");
    expect(root.textContent).toContain("fill differs by $0.03");
  });

  it("shows native and readiness receipts and explains a readiness mismatch", async () => {
    const fixture = await renderWithParity({
      status: "diverged",
      verdictJson: JSON.stringify({
        schema_version: 2,
        status: "diverged",
        reason: "production_readiness_mismatch",
        counts_by_category: {},
        divergences: [],
        native_metric_parity: {
          status: "match",
          native_metric_count: 66,
          formatted_metric_count: 25,
        },
        readiness_parity: {
          status: "mismatch",
          compared_field_count: 17,
          mismatched_fields: ["parity_signature"],
        },
        input_parity: {
          status: "match",
          compared_field_count: 15,
          fixture_id: "bar-store-v1-exact",
          fixture_sha256: "a".repeat(64),
          mismatched_fields: [],
        },
      }),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("different production-readiness evidence");
    expect(text).toContain("LEAN-native values:");
    expect(text).toContain("66 checked");
    expect(text).toContain("Readiness fields:");
    expect(text).toContain("17 checked");
    expect(text).toContain("Shared fixture:");
    expect(text).toContain("bar-store-v1-exact");
  });

  it("explains honest unavailability with trader copy", async () => {
    const fixture = await renderWithParity({
      status: "unavailable",
      verdictJson: JSON.stringify({ schema_version: 1, status: "unavailable", reason: "no_lean_counterpart" }),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("parity: Unavailable");
  });

  it("explains that an ordinary raw run did not opt into the compatibility contract", async () => {
    const fixture = await renderWithParity({
      status: "unavailable",
      verdictJson: JSON.stringify({
        schema_version: 1,
        status: "unavailable",
        reason: "execution_profile_unsupported",
      }),
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Start a Compatibility pair");
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Persisted-trade adapter — regression coverage moved from the deleted
// lean-engine mapStudyTradeToEngineTrade helper. Numerical fields are
// passed through from the persisted run contract; the UI must not
// independently recompute P&L.
// ─────────────────────────────────────────────────────────────────────────
describe("toEngineTrade", () => {
  it("passes through persisted pnl points and percent", () => {
    const trade = toEngineTrade(makeTrade({ pnlPts: 7, pnlPct: 0.02 }), 0);
    expect(trade.pnl_pts).toBe(7);
    expect(trade.pnl_pct).toBe(0.02);
  });

  it("classifies WIN/LOSS from the persisted dollar PnL, not the price delta", () => {
    // Fees can turn a positive price delta into a losing trade.
    const trade = toEngineTrade(makeTrade({ entryPrice: 500, exitPrice: 500.01, pnL: -1.9 }), 0);
    expect(trade.result).toBe("LOSS");
  });

  it("passes through a persisted zero percent", () => {
    const trade = toEngineTrade(makeTrade({ entryPrice: 0, exitPrice: 5, pnlPct: 0 }), 0);
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
