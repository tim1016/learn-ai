import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { Component, input, output, provideZonelessChangeDetection, signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { MarketDataService } from "../../../services/market-data.service";
import { IndicatorCatalogService } from "../../../shared/indicator-catalog/indicator-catalog.service";
import { TradingChartComponent } from "../../../shared/trading-chart";
import { indicatorMatches, normalizeIndicatorResults, timeframeKey } from "./strategy-lab-chart.adapter";
import { StrategyLabChartComponent } from "./strategy-lab-chart.component";

@Component({ selector: "app-trading-chart", template: "" })
class TradingChartStubComponent {
  readonly candles = input<unknown[]>([]);
  readonly overlays = input<unknown[]>([]);
  readonly equity = input<unknown[]>([]);
  readonly subPanes = input<unknown[]>([]);
  readonly markers = input<unknown[]>([]);
  readonly quote = input<unknown>(null);
  readonly timeframeLabel = input("");
  readonly activeIndicators = input<unknown[]>([]);
  readonly indicatorCategories = input<unknown[]>([]);
  readonly indicatorCatalogLoading = input(false);
  readonly indicatorAdded = output<never>();
  readonly indicatorRemoved = output<string>();
}

function makeRun(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  return {
    id: 44,
    engine: "PYTHON",
    source: "engine",
    requestedEngine: "both",
    strategyName: "ema_crossover_signal",
    symbol: "SPY",
    leanRunId: null,
    parameters: JSON.stringify({ symbol: "SPY" }),
    startDate: "2026-01-05",
    endDate: "2026-01-06",
    fillMode: "signal_bar_close",
    executedAt: 1,
    durationMs: 2,
    totalTrades: 0,
    winningTrades: 0,
    losingTrades: 0,
    winRate: 0,
    totalPnL: 0,
    initialCash: 100000,
    commissionPerOrder: 1,
    finalEquity: 100000,
    totalFees: 0,
    maxDrawdown: 0,
    sharpeRatio: null,
    sortinoRatio: null,
    profitFactor: null,
    leanStatisticsJson: null,
    leanAnalysisJson: null,
    verdictJson: null,
    verdictVersion: null,
    verdictGrade: null,
    verdictSignal: null,
    equityCurve: null,
    validationAnalytics: null,
    dataPolicy: {
      source: "polygon",
      symbol: "SPY",
      adjusted: false,
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
    tradesTruncated: false,
    parityVerdicts: [],
    ...overrides,
  };
}

describe("Strategy Lab chart adapter", () => {
  it("compares numeric indicator recipes semantically without recreating server IDs", () => {
    expect(indicatorMatches(
      "supertrend",
      { multiplier: 3.0, length: 10 },
      "supertrend",
      { length: 10, multiplier: 3 },
    )).toBe(true);
  });

  it("preserves exact server timestamps when adapting indicator points", () => {
    const timestamps = [1_767_626_100_000, 1_767_627_000_000];
    const normalized = normalizeIndicatorResults([
      {
        id: "ema_5",
        panel: "main",
        type: "line",
        color: "#ffb300",
        refs: [],
        data: timestamps.map((t, index) => ({ t, value: 500 + index })),
      },
    ]);

    expect(normalized.overlays[0].points.map((point) => point.timeMs)).toEqual(timestamps);
  });

  it("formats the persisted strategy timeframe without inferring a new cadence", () => {
    expect(timeframeKey("minute", 15)).toBe("15m");
    expect(timeframeKey("day", 1)).toBe("1D");
  });
});

describe("StrategyLabChartComponent", () => {
  it("requests one atomic exact-bar chart and preserves server time anchors", async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyLabChartComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: IndicatorCatalogService,
          useValue: { load: vi.fn(), categories: signal([]), loading: signal(false) },
        },
        { provide: MarketDataService, useValue: { getStockSnapshot: vi.fn(() => of(null)) } },
      ],
    })
      .overrideComponent(StrategyLabChartComponent, {
        remove: { imports: [TradingChartComponent] },
        add: { imports: [TradingChartStubComponent] },
      })
      .compileComponents();
    const fixture = TestBed.createComponent(StrategyLabChartComponent);
    fixture.componentRef.setInput("run", makeRun({
      source: "lean-sidecar",
      engine: "LEAN",
      parameters: JSON.stringify({ symbol: "SPY", starting_cash: 100000 }),
    }));
    fixture.detectChanges();
    await Promise.resolve();
    TestBed.tick();

    const http = TestBed.inject(HttpTestingController);
    const calendarRequests = http.match((candidate) =>
      candidate.url.endsWith("/api/lean-sidecar/calendar/next-trading-day-open"));
    expect(calendarRequests.map((request) => request.request.params.get("date")).sort()).toEqual([
      "2026-01-04",
      "2026-01-06",
    ]);
    calendarRequests.find((request) => request.request.params.get("date") === "2026-01-04")?.flush({
      next_trading_date: "2026-01-05",
      session_open_ms_utc: 1_767_624_600_000,
    });
    calendarRequests.find((request) => request.request.params.get("date") === "2026-01-06")?.flush({
      next_trading_date: "2026-01-07",
      session_open_ms_utc: 1_767_797_400_000,
    });
    await Promise.resolve();
    TestBed.tick();
    await Promise.resolve();
    TestBed.tick();
    const request = http.expectOne((candidate) => candidate.url.endsWith("/api/engine/chart"));
    expect(request.request.body).toEqual(expect.objectContaining({
      strategy_name: "ema_crossover_signal",
      parameters: { symbol: "SPY" },
      from_ms_utc: 1_767_624_600_000,
      to_ms_utc: 1_767_797_400_000,
      adjusted: false,
      timespan: "minute",
      multiplier: 15,
    }));
    const timestamps = [1_767_626_100_000, 1_767_627_000_000];
    request.flush({
      policy_key: "polygon-raw",
      symbol: "SPY",
      bars: timestamps.map((t, index) => ({ t, o: 500 + index, h: 501 + index, l: 499 + index, c: 500.5 + index, v: 10 })),
      coverage: { expected_days: 2, available_days: 1, is_complete: false, missing_session_ms_utc: [1_767_711_000_000] },
      indicator_specs: [{ id: "ema-length-5", name: "ema", params: { length: 5 }, label: "EMA 5", strategy_default: true }],
      indicators: [{
        id: "ema_5",
        panel: "main",
        type: "line",
        color: "#ffb300",
        refs: [],
        data: timestamps.map((t, index) => ({ t, value: 500 + index })),
      }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.candles().map((bar) => bar.timeMs)).toEqual(timestamps);
    expect(fixture.componentInstance.overlays()[0].points.map((point) => point.timeMs)).toEqual(timestamps);
    expect(fixture.componentInstance.indicatorNotice()).toContain("covers 1 of 2 sessions");
    http.verify();
  });

  it("shows unavailable evidence when persisted strategy parameters are malformed", async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyLabChartComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: IndicatorCatalogService,
          useValue: { load: vi.fn(), categories: signal([]), loading: signal(false) },
        },
        { provide: MarketDataService, useValue: { getStockSnapshot: vi.fn(() => of(null)) } },
      ],
    })
      .overrideComponent(StrategyLabChartComponent, {
        remove: { imports: [TradingChartComponent] },
        add: { imports: [TradingChartStubComponent] },
      })
      .compileComponents();
    const fixture = TestBed.createComponent(StrategyLabChartComponent);
    fixture.componentRef.setInput("run", makeRun({ parameters: "{" }));
    fixture.detectChanges();
    await Promise.resolve();
    TestBed.tick();

    expect(fixture.componentInstance.indicatorNotice()).toMatch(/parameters are malformed/);
    const http = TestBed.inject(HttpTestingController);
    http.expectNone((candidate) => candidate.url.endsWith("/api/engine/chart"));
    http.verify();
  });
});
