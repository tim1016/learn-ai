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
  private chart: IChartApi | null = null;
  private resizeObserver: ResizeObserver | null = null;

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
  readonly chartHeight = computed(() => this.panes().reduce(
    (height, pane) => height + pane.height,
    0,
  ));

  constructor() {
    effect(() => {
      const canvas = this.chartCanvas();
      const panes = this.panes();
      this.candles();
      this.markers();
      if (!canvas) return;
      this.rebuildChart(panes, canvas.nativeElement);
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
    const chart = this.createChart(element, {
      width: element.clientWidth,
      height: this.chartHeight(),
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
    chart.panes().forEach((pane, index) => pane.setHeight(panes[index]?.height ?? PANE_HEIGHTS.indicator));
    chart.timeScale().fitContent();
    this.chart = chart;
    this.observeResize(element);
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

  private observeResize(element: HTMLDivElement): void {
    if (typeof ResizeObserver === "undefined" || this.chart === null) return;
    this.resizeObserver = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? element.clientWidth;
      if (width > 0) this.chart?.resize(width, this.chartHeight());
    });
    this.resizeObserver.observe(element);
  }

  private destroyChart(): void {
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.chart?.remove();
    this.chart = null;
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
