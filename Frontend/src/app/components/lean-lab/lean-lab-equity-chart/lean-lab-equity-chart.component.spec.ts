import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LeanLabEquityChartComponent } from "./lean-lab-equity-chart.component";
import type { NormalizedEquityPoint } from "../../../services/lean-sidecar.types";

// Module-level mock for lightweight-charts so the test runs in jsdom
// without dragging in canvas + WebGL. The shape mirrors the existing
// chart-component spec mocks already in the project.
vi.mock("lightweight-charts", () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const setData = vi.fn();
  const applyOptions = vi.fn();
  const remove = vi.fn();
  const series = { setData, applyOptions };
  const chart = {
    addSeries: vi.fn().mockReturnValue(series),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions,
    remove,
  };
  return {
    createChart: vi.fn().mockReturnValue(chart),
    CandlestickSeries: "CandlestickSeries",
    __mocks__: { chart, series, mockTimeScale },
  };
});

import * as lwc from "lightweight-charts";

const lwcMocks = (lwc as unknown as { __mocks__: { chart: { addSeries: ReturnType<typeof vi.fn>; remove: ReturnType<typeof vi.fn> }; series: { setData: ReturnType<typeof vi.fn> }; mockTimeScale: { fitContent: ReturnType<typeof vi.fn> } } }).__mocks__;

function makePoint(ms_utc: number, value: number): NormalizedEquityPoint {
  return { ms_utc, value, open: value, high: value, low: value };
}

describe("LeanLabEquityChartComponent", () => {
  let fixture: ComponentFixture<LeanLabEquityChartComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    await TestBed.configureTestingModule({
      imports: [LeanLabEquityChartComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabEquityChartComponent);
  });

  it("creates the chart in AfterViewInit and seeds it with the initial points", async () => {
    fixture.componentRef.setInput("equityPoints", [
      makePoint(1_736_121_600_000, 100_000),
      makePoint(1_736_207_900_000, 100_100),
    ]);
    fixture.detectChanges();

    // createChart called once; addSeries(CandlestickSeries, ...) called once.
    expect(lwc.createChart).toHaveBeenCalledTimes(1);
    expect(lwcMocks.chart.addSeries).toHaveBeenCalledWith("CandlestickSeries", expect.any(Object));

    // Initial data set converts ms→seconds and preserves OHLC.
    expect(lwcMocks.series.setData).toHaveBeenCalledWith([
      { time: 1_736_121_600, open: 100_000, high: 100_000, low: 100_000, close: 100_000 },
      { time: 1_736_207_900, open: 100_100, high: 100_100, low: 100_100, close: 100_100 },
    ]);
    expect(lwcMocks.mockTimeScale.fitContent).toHaveBeenCalled();
  });

  it("updates the series when the input changes after creation", async () => {
    fixture.componentRef.setInput("equityPoints", [makePoint(1_736_121_600_000, 100_000)]);
    fixture.detectChanges();
    lwcMocks.series.setData.mockClear();

    fixture.componentRef.setInput("equityPoints", [
      makePoint(1_736_121_600_000, 100_000),
      makePoint(1_736_207_900_000, 100_500),
    ]);
    fixture.detectChanges();

    expect(lwcMocks.series.setData).toHaveBeenCalledTimes(1);
    const setDataArg = lwcMocks.series.setData.mock.calls[0][0];
    expect(setDataArg).toHaveLength(2);
    expect(setDataArg[1].close).toBe(100_500);
  });

  it("sorts unsorted equity points before drawing", () => {
    fixture.componentRef.setInput("equityPoints", [
      makePoint(1_736_207_900_000, 100_100),
      makePoint(1_736_121_600_000, 100_000),
    ]);
    fixture.detectChanges();

    const sent = lwcMocks.series.setData.mock.calls[0][0];
    expect(sent[0].time).toBeLessThan(sent[1].time);
  });

  it("draws an empty series when given no points (does not crash)", () => {
    fixture.componentRef.setInput("equityPoints", []);
    fixture.detectChanges();
    expect(lwcMocks.series.setData).toHaveBeenCalledWith([]);
  });

  it("disposes the chart in ngOnDestroy", () => {
    fixture.componentRef.setInput("equityPoints", [makePoint(1_736_121_600_000, 100_000)]);
    fixture.detectChanges();
    fixture.destroy();
    expect(lwcMocks.chart.remove).toHaveBeenCalledTimes(1);
  });
});
