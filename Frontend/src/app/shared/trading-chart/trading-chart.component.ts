import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  InjectionToken,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  model,
  output,
  signal,
  viewChild,
} from "@angular/core";
import {
  AreaSeries,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineType,
  type AreaData,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  createSeriesMarkers,
} from "lightweight-charts";

import { createAppChart } from "../charts/chart-utils";
import type { IndicatorCategory } from "../indicator-catalog/indicator-catalog.service";
import type { IndicatorPickerAdd } from "../indicator-picker/indicator-picker.component";
import { TickerQuoteComponent, type TickerQuoteView } from "../ticker-quote/ticker-quote.component";
import { ChartIndicatorRailComponent } from "./chart-indicator-rail.component";
import type {
  TradingCandle,
  TradingIndicatorChip,
  TradingMarker,
  TradingSeries,
  TradingSubPane,
} from "./trading-chart.types";

interface PaneModel {
  id: string;
  label: string;
  description: string;
  kind: "price" | "equity" | "indicator";
  height: number;
  series: TradingSeries[];
  referenceLevels: number[];
}

const AXIS_COLUMN_WIDTH = 68;
const PANE_HEIGHTS = {
  price: 470,
  equity: 205,
  indicator: 185,
} as const;
const MIN_PANE_HEIGHT = 120;
export const TRADING_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  "TRADING_CHART_FACTORY",
  { providedIn: "root", factory: () => createAppChart },
);
const THEME = {
  bg: "#131722",
  grid: "rgba(42, 46, 57, 0.5)",
  text: "#9598a1",
  border: "#2a2e39",
  crosshair: "#4a5068",
  bull: "#26a69a",
  bear: "#ef5350",
  bullVolume: "rgba(38, 166, 154, 0.20)",
  bearVolume: "rgba(239, 83, 80, 0.20)",
  equity: "#7aa9ff",
};

/** One native lightweight-charts pane tree for a shared horizontal clock. */
@Component({
  selector: "app-trading-chart",
  imports: [ChartIndicatorRailComponent, TickerQuoteComponent],
  templateUrl: "./trading-chart.component.html",
  styleUrl: "./trading-chart.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    "[class.trading-chart-host--expanded]": "expanded()",
    "(keydown.escape)": "collapse()",
  },
})
export class TradingChartComponent implements OnDestroy {
  private readonly createChart = inject(TRADING_CHART_FACTORY);

  readonly candles = input<readonly TradingCandle[]>([]);
  readonly overlays = input<readonly TradingSeries[]>([]);
  readonly equity = input<readonly TradingSeries[]>([]);
  readonly subPanes = input<readonly TradingSubPane[]>([]);
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly quote = input<TickerQuoteView | null>(null);
  readonly timeframeLabel = input("1m");
  readonly activeIndicators = input<readonly TradingIndicatorChip[]>([]);
  readonly indicatorCategories = input<readonly IndicatorCategory[]>([]);
  readonly indicatorCatalogLoading = input(false);

  readonly expanded = model(false);
  readonly indicatorAdded = output<IndicatorPickerAdd>();
  readonly indicatorRemoved = output<string>();

  private readonly chartCanvas = viewChild<ElementRef<HTMLDivElement>>("chartCanvas");
  private readonly canvasWrap = viewChild<ElementRef<HTMLDivElement>>("canvasWrap");
  private readonly chart = signal<IChartApi | null>(null);

  readonly panes = computed<PaneModel[]>(() => {
    const result: PaneModel[] = [{
      id: "price",
      label: "Price",
      description: "Candlesticks, volume, trades, and price overlays",
      kind: "price",
      height: PANE_HEIGHTS.price,
      series: this.overlays().map((series) => ({ ...series })),
      referenceLevels: [],
    }];
    if (this.equity().some((series) => series.points.length > 0)) {
      result.push({
        id: "equity",
        label: "Realized equity",
        description: seriesNames(this.equity()),
        kind: "equity",
        height: PANE_HEIGHTS.equity,
        series: this.equity().map((series) => ({ ...series })),
        referenceLevels: [],
      });
    }
    for (const pane of this.subPanes()) {
      if (pane.series.some((series) => series.points.length > 0)) {
        result.push({
          id: pane.id,
          label: pane.label,
          description: seriesNames(pane.series),
          kind: "indicator",
          height: PANE_HEIGHTS.indicator,
          series: pane.series.map((series) => ({ ...series })),
          referenceLevels: [...(pane.referenceLevels ?? [])],
        });
      }
    }
    return result;
  });
  /** Measured height of the scroll wrap. 0 until the observer first fires. */
  readonly availableHeight = signal(0);

  /**
   * Pane heights are proportional to the available height, using the fixed
   * PANE_HEIGHTS as weights so relative proportions are unchanged. Below the
   * floor the fixed heights return and `.trading-chart__canvas-wrap` scrolls —
   * squashing a price pane to nothing is worse than a scrollbar.
   */
  readonly paneHeights = computed<number[]>(() => {
    const weights = this.panes().map((pane) => pane.height);
    const weightTotal = weights.reduce((total, weight) => total + weight, 0);
    const available = this.availableHeight();
    if (available <= 0 || weightTotal === 0) return weights;
    const scaled = weights.map((weight) => Math.round((weight / weightTotal) * available));
    return scaled.some((height) => height < MIN_PANE_HEIGHT) ? weights : scaled;
  });

  readonly chartHeight = computed(() =>
    this.paneHeights().reduce((total, height) => total + height, 0),
  );

  constructor() {
    // Build: structural and data inputs only. `rebuildChart` reads candles and
    // markers through `renderPrice`, which is what makes a data change rebuild.
    // It reads nothing height-derived, so a resize cannot tear the chart down.
    effect(() => {
      const canvas = this.chartCanvas();
      if (!canvas) return;
      this.rebuildChart(this.panes(), canvas.nativeElement);
    });

    // Size: the single place a measured height reaches the chart. It re-runs on
    // a rebuild too (`chart()` is a dependency), so the freshly built chart and
    // a later measurement take the same path.
    effect(() => {
      const chart = this.chart();
      const canvas = this.chartCanvas();
      const heights = this.paneHeights();
      if (chart === null || !canvas) return;
      chart.panes().forEach((pane, index) => pane.setHeight(heights[index] ?? MIN_PANE_HEIGHT));
      chart.resize(canvas.nativeElement.clientWidth, this.chartHeight());
    });

    // Measure: independent of the chart's lifetime, so a data change no longer
    // disconnects and re-creates the observer on its way through.
    effect((onCleanup) => {
      const wrap = this.canvasWrap();
      if (!wrap || typeof ResizeObserver === "undefined") return;
      const observer = new ResizeObserver((entries) => {
        // The wrap's height is layout-driven; the canvas's is driven by
        // chartHeight, so observing the canvas would be a feedback loop.
        const height = entries[0]?.contentRect.height ?? wrap.nativeElement.clientHeight;
        if (height > 0) this.availableHeight.set(Math.round(height));
      });
      observer.observe(wrap.nativeElement);
      onCleanup(() => observer.disconnect());
    });
  }

  ngOnDestroy(): void {
    this.destroyChart();
  }

  toggleExpanded(): void {
    this.expanded.update((value) => !value);
  }

  collapse(): void {
    this.expanded.set(false);
  }

  onIndicatorAdded(event: IndicatorPickerAdd): void {
    this.indicatorAdded.emit(event);
  }

  private rebuildChart(panes: readonly PaneModel[], element: HTMLDivElement): void {
    this.destroyChart();
    // The panes' own weights, not the measured height: reading anything derived
    // from availableHeight() here would make every resize rebuild the chart.
    // The size effect applies the real heights as soon as `chart` is set.
    const chart = this.createChart(element, {
      width: element.clientWidth,
      height: panes.reduce((total, pane) => total + pane.height, 0),
      layout: {
        background: { color: THEME.bg },
        textColor: THEME.text,
        panes: { enableResize: true, separatorColor: THEME.border, separatorHoverColor: THEME.crosshair },
      },
      grid: {
        vertLines: { color: THEME.grid },
        horzLines: { color: THEME.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: THEME.border,
        minBarSpacing: 0.5,
        shiftVisibleRangeOnNewBar: false,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: THEME.crosshair, labelBackgroundColor: THEME.bg },
        horzLine: { color: THEME.crosshair, labelBackgroundColor: THEME.bg },
      },
      rightPriceScale: {
        borderColor: THEME.border,
        minimumWidth: AXIS_COLUMN_WIDTH,
      },
    });

    this.renderPrice(chart);
    panes.slice(1).forEach((pane, index) => {
      this.renderNormalizedSeries(chart, pane.series, pane.referenceLevels, index + 1);
    });
    this.renderNormalizedSeries(chart, panes[0]?.series ?? [], [], 0);
    chart.timeScale().fitContent();
    this.chart.set(chart);
  }

  private renderPrice(chart: IChartApi): void {
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: THEME.bull,
      downColor: THEME.bear,
      borderUpColor: THEME.bull,
      borderDownColor: THEME.bear,
      wickUpColor: THEME.bull,
      wickDownColor: THEME.bear,
    }, 0);
    const candles: CandlestickData[] = this.candles()
      .map((bar) => ({
        time: toChartTime(bar.timeMs),
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      }))
      .sort(sortByTime);
    candleSeries.setData(candles);

    if (this.candles().some((bar) => bar.volume !== undefined)) {
      const volume = chart.addSeries(HistogramSeries, {
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
      }, 0);
      volume.setData(this.candles().map((bar) => ({
        time: toChartTime(bar.timeMs),
        value: bar.volume ?? 0,
        color: bar.close >= bar.open ? THEME.bullVolume : THEME.bearVolume,
      })).sort(sortByTime));
      chart.priceScale("volume", 0).applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    }

    const markers: SeriesMarker<Time>[] = this.markers()
      .map((marker) => ({
        time: toChartTime(marker.timeMs),
        position: marker.position,
        color: marker.color,
        shape: marker.shape,
        text: marker.text,
      }))
      .sort(sortByTime);
    if (markers.length > 0) createSeriesMarkers(candleSeries, markers);
  }

  private renderNormalizedSeries(
    chart: IChartApi,
    seriesList: readonly TradingSeries[],
    referenceLevels: readonly number[],
    paneIndex: number,
  ): void {
    let firstNormalized = true;
    for (const series of seriesList) {
      const data = series.points
        .map((point) => ({ time: toChartTime(point.timeMs), value: point.value }))
        .sort(sortByTime);
      let rendered: ISeriesApi<SeriesType>;
      if (series.type === "histogram") {
        const histogram = chart.addSeries(HistogramSeries, {
          color: series.color,
          priceLineVisible: false,
          lastValueVisible: false,
        }, paneIndex);
        histogram.setData(data);
        rendered = histogram;
      } else if (series.type === "area") {
        const area = chart.addSeries(AreaSeries, {
          lineColor: series.color || THEME.equity,
          topColor: "rgba(122, 169, 255, 0.28)",
          bottomColor: "rgba(122, 169, 255, 0.02)",
          lineWidth: series.lineWidth ?? 2,
          lineType: series.lineType === "steps" ? LineType.WithSteps : LineType.Simple,
          priceLineVisible: false,
        }, paneIndex);
        area.setData(data as AreaData[]);
        rendered = area;
      } else {
        const line = chart.addSeries(LineSeries, {
          color: series.color,
          lineWidth: series.lineWidth ?? 2,
          lineType: series.lineType === "steps" ? LineType.WithSteps : LineType.Simple,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        }, paneIndex);
        line.setData(data);
        rendered = line;
      }
      if (referenceLevels.length > 0 && firstNormalized) {
        for (const price of referenceLevels) {
          rendered.createPriceLine({
            price,
            color: THEME.crosshair,
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
          });
        }
      }
      firstNormalized = false;
    }
  }

  private destroyChart(): void {
    // `update` reads the held value without registering a dependency, so the
    // build effect that calls this cannot end up depending on `chart` and
    // retriggering itself when the replacement is set.
    this.chart.update((chart) => {
      chart?.remove();
      return null;
    });
  }
}

function toChartTime(timeMs: number): UTCTimestamp {
  // The chart library is the sole seconds boundary. All application contracts
  // remain int64 ms UTC and there is no parsing or re-bucketing in Angular.
  return timeMs / 1000 as UTCTimestamp;
}

function sortByTime<T extends { time: Time }>(left: T, right: T): number {
  return Number(left.time) - Number(right.time);
}

function seriesNames(series: readonly TradingSeries[]): string {
  return series.map((item) => item.name).join(" · ");
}
