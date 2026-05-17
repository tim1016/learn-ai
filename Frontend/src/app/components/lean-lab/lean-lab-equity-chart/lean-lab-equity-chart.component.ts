import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  effect,
  ElementRef,
  inject,
  input,
  OnDestroy,
  viewChild,
} from "@angular/core";
import {
  CandlestickData,
  CandlestickSeries,
  createChart,
  IChartApi,
  ISeriesApi,
  UTCTimestamp,
} from "lightweight-charts";
import type { NormalizedEquityPoint } from "../../../services/lean-sidecar.types";

/**
 * Renders the LEAN-sidecar normalized equity curve as a candlestick
 * chart. The Phase 3a parser already shaped `NormalizedEquityPoint`
 * as OHLC + ms-since-epoch UTC; this component is a thin adapter to
 * TradingView lightweight-charts. Nothing here transforms the
 * underlying data — values pass through unchanged.
 *
 * lightweight-charts owns its own DOM rendering; Angular only owns
 * the container <div>. The component creates the chart in
 * AfterViewInit (when the container is in the DOM), disposes it in
 * OnDestroy, and reacts to input changes via `effect()`.
 */

// Match the dark-theme palette engine-chart already uses so the LEAN
// Lab chart looks like a sibling card, not a different theme.
const DARK = {
  bg: "#131722",
  grid: "rgba(42, 46, 57, 0.5)",
  text: "#9598a1",
  border: "#2a2e39",
  crosshair: "#4a5068",
  bull: "#26a69a",
  bear: "#ef5350",
};

@Component({
  selector: "app-lean-lab-equity-chart",
  standalone: true,
  template: `<div #container class="lean-lab-equity-chart"></div>`,
  styles: [
    `
      .lean-lab-equity-chart {
        width: 100%;
        height: 320px;
        background: ${DARK.bg};
        border-radius: 0.5rem;
        border: 1px solid ${DARK.border};
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanLabEquityChartComponent implements AfterViewInit, OnDestroy {
  /**
   * Equity points from the normalized parser. Each carries
   * `ms_utc` (int64 ms UTC), `value` (close), and OHLC components
   * the candlestick renderer consumes directly.
   */
  readonly equityPoints = input<NormalizedEquityPoint[]>([]);

  private readonly container = viewChild.required<ElementRef<HTMLDivElement>>("container");
  private chart: IChartApi | null = null;
  private series: ISeriesApi<"Candlestick"> | null = null;

  constructor() {
    // Wire the input → series update path with an effect. The
    // effect runs whenever `equityPoints()` changes; the no-op
    // branch when the chart isn't created yet is intentional —
    // AfterViewInit fills the series in for the initial paint.
    inject(ElementRef); // ensure DI is wired before effect registration
    effect(() => {
      const points = this.equityPoints();
      if (this.series) {
        this.series.setData(this.toCandlestickData(points));
        this.chart?.timeScale().fitContent();
      }
    });
  }

  ngAfterViewInit(): void {
    const el = this.container().nativeElement;
    this.chart = createChart(el, {
      // ``autoSize: true`` makes lightweight-charts attach its own
      // ResizeObserver to the container so the canvas tracks layout
      // changes (window resize, responsive breakpoint, parent
      // collapse/expand). Without it, the one-time width/height pin
      // at mount silently goes stale and the chart renders clipped.
      autoSize: true,
      layout: {
        background: { color: DARK.bg },
        textColor: DARK.text,
      },
      grid: {
        vertLines: { color: DARK.grid },
        horzLines: { color: DARK.grid },
      },
      timeScale: { borderColor: DARK.border, timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: DARK.border },
      crosshair: { vertLine: { color: DARK.crosshair }, horzLine: { color: DARK.crosshair } },
    });
    this.series = this.chart.addSeries(CandlestickSeries, {
      upColor: DARK.bull,
      downColor: DARK.bear,
      borderUpColor: DARK.bull,
      borderDownColor: DARK.bear,
      wickUpColor: DARK.bull,
      wickDownColor: DARK.bear,
    });
    // Initial paint with whatever points were already in the signal
    // at view-init time (the effect above only fires on *changes*,
    // not on initial value).
    this.series.setData(this.toCandlestickData(this.equityPoints()));
    this.chart.timeScale().fitContent();
  }

  ngOnDestroy(): void {
    this.chart?.remove();
    this.chart = null;
    this.series = null;
  }

  /**
   * Convert NormalizedEquityPoint[] to lightweight-charts'
   * CandlestickData. The library expects `time` as UTCTimestamp
   * (seconds since epoch, integer), so we divide ms by 1000.
   * Sorting is defensive: lightweight-charts rejects non-monotonic
   * series with an exception, and the LEAN summary's value array is
   * supposed to be sorted but a regression in the parser would
   * surface as a chart crash without this guard.
   */
  private toCandlestickData(points: NormalizedEquityPoint[]): CandlestickData[] {
    return [...points]
      .sort((a, b) => a.ms_utc - b.ms_utc)
      .map((p) => ({
        time: Math.floor(p.ms_utc / 1000) as UTCTimestamp,
        open: p.open,
        high: p.high,
        low: p.low,
        close: p.value,
      }));
  }
}
