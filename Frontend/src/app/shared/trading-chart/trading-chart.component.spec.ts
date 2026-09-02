import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TradingChartComponent, TRADING_CHART_FACTORY } from "./trading-chart.component";

const EQUITY = [{
  id: "equity", name: "Realized equity", type: "area", color: "#fff", lineType: "steps",
  points: [{ timeMs: 1_700_000_000_000, value: 100_100 }],
}];

const chartHarness: {
  options: Record<string, unknown> | null;
  addSeries: ReturnType<typeof vi.fn>;
  paneHeights: ReturnType<typeof vi.fn>[];
  timeScale: { fitContent: ReturnType<typeof vi.fn> };
  seriesData: ReturnType<typeof vi.fn>[];
  resize: ReturnType<typeof vi.fn>;
} = {
  options: null,
  addSeries: vi.fn(),
  paneHeights: [],
  timeScale: { fitContent: vi.fn() },
  seriesData: [],
  resize: vi.fn(),
};

const createChart = vi.fn((_element: HTMLElement, options: Record<string, unknown>) => {
  chartHarness.options = options;
  chartHarness.addSeries = vi.fn(() => {
    const setData = vi.fn();
    chartHarness.seriesData.push(setData);
    return { setData, createPriceLine: vi.fn() };
  });
  chartHarness.paneHeights = [vi.fn(), vi.fn(), vi.fn()];
  chartHarness.timeScale = { fitContent: vi.fn() };
  chartHarness.resize = vi.fn();
  return {
    addSeries: chartHarness.addSeries,
    panes: vi.fn(() => chartHarness.paneHeights.map((setHeight) => ({ setHeight }))),
    timeScale: vi.fn(() => chartHarness.timeScale),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    resize: chartHarness.resize,
    remove: vi.fn(),
  };
});

describe("TradingChartComponent", () => {
  beforeEach(() => {
    createChart.mockClear();
    chartHarness.options = null;
    chartHarness.seriesData = [];
  });

  afterEach(() => TestBed.resetTestingModule());

  async function createComponent() {
    await TestBed.configureTestingModule({
      imports: [TradingChartComponent],
      providers: [provideZonelessChangeDetection(), { provide: TRADING_CHART_FACTORY, useValue: createChart }],
    }).compileComponents();
    return TestBed.createComponent(TradingChartComponent);
  }

  it("uses one native chart and assigns price, realized equity, and indicators to panes", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("candles", [
      { timeMs: 1_700_000_000_000, open: 100, high: 102, low: 99, close: 101, volume: 20 },
    ]);
    fixture.componentRef.setInput("equity", [{
      id: "equity", name: "Realized equity", type: "area", color: "#fff", lineType: "steps",
      points: [{ timeMs: 1_700_000_000_000, value: 100_100 }],
    }]);
    fixture.componentRef.setInput("subPanes", [{
      id: "rsi", label: "RSI", referenceLevels: [30, 70],
      series: [{ id: "rsi", name: "RSI", type: "line", color: "#abc", points: [{ timeMs: 1_700_000_000_000, value: 55 }] }],
    }]);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(createChart).toHaveBeenCalledOnce();
    expect(chartHarness.addSeries.mock.calls.map((call) => call[2])).toEqual([0, 0, 1, 2]);
    expect(chartHarness.timeScale.fitContent).toHaveBeenCalledOnce();
    expect(chartHarness.paneHeights[0]).toHaveBeenCalledWith(470);
    expect(chartHarness.paneHeights[1]).toHaveBeenCalledWith(205);
    expect(chartHarness.paneHeights[2]).toHaveBeenCalledWith(185);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelectorAll(".trading-chart__canvas")).toHaveLength(1);
    expect(root.querySelector(".trading-chart__attribution")?.textContent).toContain("TradingView");
    expect(root.querySelector(".trading-chart__pane-legend")?.textContent).toContain("Candlesticks, volume, trades");
    expect(root.querySelector(".trading-chart__pane-legend")?.textContent).toContain("Realized equity");
    expect(root.querySelector(".trading-chart__pane-legend")?.textContent).toContain("RSI");
  });

  it("converts milliseconds only at the chart boundary without collapsing sub-second points", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("candles", [
      { timeMs: 1_700_000_000_111, open: 100, high: 102, low: 99, close: 101, volume: 20 },
      { timeMs: 1_700_000_000_999, open: 101, high: 103, low: 100, close: 102, volume: 21 },
    ]);
    fixture.detectChanges();
    await fixture.whenStable();

    const priceData = chartHarness.seriesData[0].mock.calls[0][0] as { time: number }[];
    expect(priceData.map((point) => point.time)).toEqual([1_700_000_000.111, 1_700_000_000.999]);
  });

  it("creates the indicator picker rail only after expansion", async () => {
    const fixture = await createComponent();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector(".trading-chart__rail")).toBeNull();
    root.querySelector<HTMLButtonElement>("[aria-label='Expand chart']")?.click();
    fixture.detectChanges();
    expect(root.querySelector(".trading-chart__rail")).not.toBeNull();
  });

  it("distributes measured height across panes proportionally to their weights", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("equity", EQUITY);
    fixture.detectChanges();
    await fixture.whenStable();

    fixture.componentInstance.availableHeight.set(1350);
    fixture.detectChanges();
    await fixture.whenStable();

    // Weights 470 (price) : 205 (equity) = 675 total; a 1350px viewport scales both 2x.
    expect(fixture.componentInstance.paneHeights()).toEqual([940, 410]);
    expect(fixture.componentInstance.chartHeight()).toBe(1350);
    // A resize must not tear down and rebuild the whole chart — the size
    // effect re-applies heights to the chart that is already mounted.
    expect(createChart).toHaveBeenCalledOnce();
  });

  it("re-applies the distributed pane heights to the live chart on every measurement", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("equity", EQUITY);
    fixture.detectChanges();
    await fixture.whenStable();

    // The first render happens before the observer has measured anything, so
    // the fixed weights are what reach the chart.
    expect(chartHarness.paneHeights[0]).toHaveBeenCalledWith(470);
    expect(chartHarness.paneHeights[1]).toHaveBeenCalledWith(205);

    fixture.componentInstance.availableHeight.set(1350);
    fixture.detectChanges();
    await fixture.whenStable();

    // Before the build/size split, setHeight ran only inside rebuildChart, so a
    // measurement moved the canvas and the CSS variable while leaving every
    // pane at its pre-measurement height.
    expect(chartHarness.paneHeights[0]).toHaveBeenLastCalledWith(940);
    expect(chartHarness.paneHeights[1]).toHaveBeenLastCalledWith(410);
    expect(chartHarness.resize).toHaveBeenLastCalledWith(expect.any(Number), 1350);
  });

  it("falls back to fixed pane heights when the viewport is below the floor", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("equity", EQUITY);
    fixture.detectChanges();
    await fixture.whenStable();

    fixture.componentInstance.availableHeight.set(180);
    fixture.detectChanges();

    // Two panes at a 120px floor cannot fit 180px, so the wrap scrolls instead
    // of squashing the price pane into unreadability.
    expect(fixture.componentInstance.chartHeight()).toBe(675);
  });

  it("uses the fixed heights until a measurement arrives", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("equity", EQUITY);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(fixture.componentInstance.chartHeight()).toBe(675);
  });

  it("rebuilds the chart when candle data changes after mount", async () => {
    const fixture = await createComponent();
    fixture.componentRef.setInput("candles", [
      { timeMs: 1_700_000_000_000, open: 100, high: 102, low: 99, close: 101, volume: 20 },
    ]);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(createChart).toHaveBeenCalledOnce();

    fixture.componentRef.setInput("candles", [
      { timeMs: 1_700_000_000_000, open: 100, high: 102, low: 99, close: 101, volume: 20 },
      { timeMs: 1_700_000_060_000, open: 101, high: 104, low: 100, close: 103, volume: 15 },
    ]);
    fixture.detectChanges();
    await fixture.whenStable();

    // Data changes must still rebuild the chart — only availableHeight()
    // changes (resizes) are excluded from the mount effect's dependencies.
    expect(createChart).toHaveBeenCalledTimes(2);
  });
});
