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
  viewChildren,
} from "@angular/core";
import {
  AreaSeries,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  type AreaData,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";

import type { IndicatorCategory } from "../indicator-catalog/indicator-catalog.service";
import {
  IndicatorPickerComponent,
  type IndicatorPickerAdd,
} from "../indicator-picker/indicator-picker.component";
import { TickerQuoteComponent, type TickerQuoteView } from "../ticker-quote/ticker-quote.component";
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
  kind: "price" | "equity" | "indicator";
  series: TradingSeries[];
  referenceLevels: number[];
}

interface PaneChart {
  id: string;
  chart: IChartApi;
  series: ISeriesApi<SeriesType>[];
}

const AXIS_COLUMN_WIDTH = 68;
export const TRADING_CHART_FACTORY = new InjectionToken<typeof createChart>(
  "TRADING_CHART_FACTORY",
  { providedIn: "root", factory: () => createChart },
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

@Component({
  selector: "app-trading-chart",
  imports: [IndicatorPickerComponent, TickerQuoteComponent],
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

  private readonly paneElements = viewChildren<ElementRef<HTMLDivElement>>("paneCanvas");
  private readonly paneCharts: PaneChart[] = [];
  private readonly resizeObservers: ResizeObserver[] = [];
  private syncing = false;
  private syncFrame: number | null = null;

  readonly logicalRange = signal<LogicalRange | null>(null);
  readonly logicalRangeLabel = computed(() => {
    const range = this.logicalRange();
    return range ? `${range.from.toFixed(3)}:${range.to.toFixed(3)}` : "fit";
  });

  readonly panes = computed<PaneModel[]>(() => {
    const result: PaneModel[] = [{
      id: "price",
      label: "Price",
      kind: "price",
      series: this.overlays().map((series) => ({ ...series })),
      referenceLevels: [],
    }];
    if (this.equity().some((series) => series.points.length > 0)) {
      result.push({
        id: "equity",
        label: "Equity",
        kind: "equity",
        series: this.equity().map((series) => ({ ...series })),
        referenceLevels: [],
      });
    }
    for (const pane of this.subPanes()) {
      if (pane.series.some((series) => series.points.length > 0)) {
        result.push({
          id: pane.id,
          label: pane.label,
          kind: "indicator",
          series: pane.series.map((series) => ({ ...series })),
          referenceLevels: [...(pane.referenceLevels ?? [])],
        });
      }
    }
    return result;
  });

  constructor() {
    effect(() => {
      const panes = this.panes();
      const elements = this.paneElements();
      this.candles();
      this.markers();
      if (elements.length !== panes.length) return;
      this.rebuildCharts(panes, elements);
    });
  }

  ngOnDestroy(): void {
    this.destroyCharts();
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

  private rebuildCharts(panes: readonly PaneModel[], elements: readonly ElementRef<HTMLDivElement>[]): void {
    this.destroyCharts();
    panes.forEach((pane, index) => {
      const element = elements[index].nativeElement;
      const isBottom = index === panes.length - 1;
      const height = pane.kind === "price" ? 430 : 170;
      const chart = this.createChart(element, {
        width: element.clientWidth,
        height,
        layout: { background: { color: THEME.bg }, textColor: THEME.text },
        grid: {
          vertLines: { color: THEME.grid },
          horzLines: { color: THEME.grid },
        },
        timeScale: {
          visible: isBottom,
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

      const instance: PaneChart = { id: pane.id, chart, series: [] };
      if (pane.kind === "price") this.renderPrice(instance);
      else this.renderTimeAnchors(instance);
      this.renderNormalizedSeries(instance, pane.series, pane.referenceLevels);
      this.paneCharts.push(instance);
      this.subscribeRange(instance);
      this.observeResize(instance, element);
    });

    const main = this.paneCharts[0]?.chart;
    main?.timeScale().fitContent();
    this.scheduleRangeBroadcast(main?.timeScale().getVisibleLogicalRange() ?? null, "price");
  }

  /**
   * Give every non-price chart the candle pane's complete timestamp set.
   * Logical-range synchronization is only pixel-accurate when each chart has
   * the same logical index map; sparse equity/indicator points alone would
   * otherwise compress time and put a drawdown under the wrong candle.
   */
  private renderTimeAnchors(instance: PaneChart): void {
    if (this.candles().length === 0) return;
    const anchors = instance.chart.addSeries(LineSeries, {
      color: "rgba(0, 0, 0, 0)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    anchors.setData(
      this.candles()
        .map((candle): WhitespaceData => ({ time: toChartTime(candle.timeMs) }))
        .sort(sortByTime),
    );
    instance.series.push(anchors);
  }

  private renderPrice(instance: PaneChart): void {
    const candleSeries = instance.chart.addSeries(CandlestickSeries, {
      upColor: THEME.bull,
      downColor: THEME.bear,
      borderUpColor: THEME.bull,
      borderDownColor: THEME.bear,
      wickUpColor: THEME.bull,
      wickDownColor: THEME.bear,
    });
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
    instance.series.push(candleSeries);

    if (this.candles().some((bar) => bar.volume !== undefined)) {
      const volume = instance.chart.addSeries(HistogramSeries, {
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
      });
      volume.setData(this.candles().map((bar): HistogramData => ({
        time: toChartTime(bar.timeMs),
        value: bar.volume ?? 0,
        color: bar.close >= bar.open ? THEME.bullVolume : THEME.bearVolume,
      })).sort(sortByTime));
      instance.chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      instance.series.push(volume);
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
    instance: PaneChart,
    seriesList: readonly TradingSeries[],
    referenceLevels: readonly number[],
  ): void {
    let firstNormalized = true;
    for (const series of seriesList) {
      const data = series.points
        .map((point) => ({ time: toChartTime(point.timeMs), value: point.value }))
        .sort(sortByTime);
      let rendered: ISeriesApi<SeriesType>;
      if (series.type === "histogram") {
        const histogram = instance.chart.addSeries(HistogramSeries, {
          color: series.color,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        histogram.setData(data as HistogramData[]);
        rendered = histogram;
      } else if (series.type === "area") {
        const area = instance.chart.addSeries(AreaSeries, {
          lineColor: series.color || THEME.equity,
          topColor: "rgba(122, 169, 255, 0.28)",
          bottomColor: "rgba(122, 169, 255, 0.02)",
          lineWidth: series.lineWidth ?? 2,
          priceLineVisible: false,
        });
        area.setData(data as AreaData[]);
        rendered = area;
      } else {
        const line = instance.chart.addSeries(LineSeries, {
          color: series.color,
          lineWidth: series.lineWidth ?? 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        line.setData(data);
        rendered = line;
      }
      if (referenceLevels.length > 0 && firstNormalized) {
        for (const price of referenceLevels) {
          rendered.createPriceLine({
            price,
            color: "#4a5068",
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
          });
        }
      }
      instance.series.push(rendered);
      firstNormalized = false;
    }
  }

  private subscribeRange(source: PaneChart): void {
    source.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      this.scheduleRangeBroadcast(range, source.id);
    });
  }

  private scheduleRangeBroadcast(range: LogicalRange | null, sourceId: string): void {
    if (!range || this.syncing) return;
    this.syncing = true;
    if (this.syncFrame !== null) cancelAnimationFrame(this.syncFrame);
    this.syncFrame = requestAnimationFrame(() => {
      for (const pane of this.paneCharts) {
        if (pane.id !== sourceId) pane.chart.timeScale().setVisibleLogicalRange(range);
      }
      this.logicalRange.set(range);
      this.syncing = false;
      this.syncFrame = null;
    });
  }

  private observeResize(instance: PaneChart, element: HTMLDivElement): void {
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? element.clientWidth;
      if (width > 0) instance.chart.applyOptions({ width });
    });
    observer.observe(element);
    this.resizeObservers.push(observer);
  }

  private destroyCharts(): void {
    if (this.syncFrame !== null) {
      cancelAnimationFrame(this.syncFrame);
      this.syncFrame = null;
    }
    this.resizeObservers.splice(0).forEach((observer) => observer.disconnect());
    this.paneCharts.splice(0).forEach((pane) => pane.chart.remove());
    this.syncing = false;
  }
}

function toChartTime(timeMs: number): UTCTimestamp {
  return Math.floor(timeMs / 1000) as UTCTimestamp;
}

function sortByTime<T extends { time: Time }>(left: T, right: T): number {
  return Number(left.time) - Number(right.time);
}
