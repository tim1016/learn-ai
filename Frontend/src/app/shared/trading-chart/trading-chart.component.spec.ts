import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TradingChartComponent, TRADING_CHART_FACTORY } from "./trading-chart.component";

const chartHarness: {
  charts: {
    options: Record<string, unknown>;
    rangeCallback: ((range: { from: number; to: number } | null) => void) | null;
    setVisibleLogicalRange: ReturnType<typeof vi.fn>;
  }[];
} = { charts: [] };

const createChart = vi.fn((_element: HTMLElement, options: Record<string, unknown>) => {
  const timeScale = {
    fitContent: vi.fn(),
    getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: 10 })),
    setVisibleLogicalRange: vi.fn(),
    subscribeVisibleLogicalRangeChange: vi.fn((callback) => {
      entry.rangeCallback = callback;
    }),
  };
  const series = {
    setData: vi.fn(),
    createPriceLine: vi.fn(),
  };
  const entry = {
    options,
    rangeCallback: null as ((range: { from: number; to: number } | null) => void) | null,
    setVisibleLogicalRange: timeScale.setVisibleLogicalRange,
  };
  chartHarness.charts.push(entry);
  return {
    addSeries: vi.fn(() => series),
    timeScale: vi.fn(() => timeScale),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
});

describe("TradingChartComponent", () => {
  beforeEach(() => {
    chartHarness.charts.splice(0);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
  });

  afterEach(() => vi.unstubAllGlobals());

  it("uses one axis column, one bottom time axis, and broadcasts one logical range to every pane", async () => {
    await TestBed.configureTestingModule({
      imports: [TradingChartComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: TRADING_CHART_FACTORY, useValue: createChart },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(TradingChartComponent);
    fixture.componentRef.setInput("candles", [
      { timeMs: 1_700_000_000_000, open: 100, high: 102, low: 99, close: 101, volume: 20 },
    ]);
    fixture.componentRef.setInput("equity", [{ id: "equity", label: "Equity", type: "area", color: "#fff", points: [{ timeMs: 1_700_000_000_000, value: 100_100 }] }]);
    fixture.componentRef.setInput("subPanes", [{ id: "rsi", label: "RSI", referenceLevels: [30, 70], series: [{ id: "rsi", label: "RSI", type: "line", color: "#abc", points: [{ timeMs: 1_700_000_000_000, value: 55 }] }] }]);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const panes = (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLElement>(".trading-chart__pane");
    expect(panes).toHaveLength(3);
    expect([...panes].map((pane) => pane.dataset["gridColumnWidth"])).toEqual(["68", "68", "68"]);
    expect([...panes].map((pane) => pane.dataset["timeAxis"])).toEqual(["hidden", "hidden", "visible"]);
    expect([...panes].map((pane) => pane.dataset["timelineSize"])).toEqual(["1", "1", "1"]);
    expect(chartHarness.charts).toHaveLength(3);
    for (const chart of chartHarness.charts) {
      expect((chart.options["rightPriceScale"] as { minimumWidth: number }).minimumWidth).toBe(68);
    }

    chartHarness.charts[0].rangeCallback?.({ from: 3, to: 9 });
    expect(chartHarness.charts[1].setVisibleLogicalRange).toHaveBeenCalledWith({ from: 3, to: 9 });
    expect(chartHarness.charts[2].setVisibleLogicalRange).toHaveBeenCalledWith({ from: 3, to: 9 });
  });

  it("creates the indicator picker rail only after expansion", async () => {
    await TestBed.configureTestingModule({
      imports: [TradingChartComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: TRADING_CHART_FACTORY, useValue: createChart },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(TradingChartComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector(".trading-chart__rail")).toBeNull();
    root.querySelector<HTMLButtonElement>("[aria-label='Expand chart']")?.click();
    fixture.detectChanges();
    expect(root.querySelector(".trading-chart__rail")).not.toBeNull();
  });
});
