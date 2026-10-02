import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  InjectionToken,
  computed,
  effect,
  inject,
  input,
  output,
  untracked,
  viewChild,
} from '@angular/core';
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type TickMarkType,
} from 'lightweight-charts';

import { createAppChart, formatChartAxisTick } from '../../../../shared/charts/chart-utils';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { formatChartCrosshairTime } from '../dual-pane-chart/dual-pane-chart.component';
import type { IndicatorSeriesPlan } from '../dual-pane-chart/dual-pane-chart-indicators';
import type { StrategyViewResponse } from '../lib/broker-v2-panel.types';
import {
  StrategyChartOverlay,
  type OverlayLine,
  type StrategyChartOverlayState,
} from './strategy-chart-overlay';
import {
  BAND_LINE_COLOR,
  decisionMarkers,
  lineColorHex,
  openingRange,
  rangeRevealing,
  strategyLinePlans,
  toChartTime,
  toGateShadedCandles,
  type StrategyLinePlan,
} from './strategy-view-model';

export const STRATEGY_CHART_FACTORY = new InjectionToken<typeof createAppChart>(
  'STRATEGY_CHART_FACTORY',
  { providedIn: 'root', factory: () => createAppChart },
);

/** A click on one decision candle, with where it happened on screen. */
export interface StrategyCandleClick {
  readonly barCloseMs: number;
  readonly clientX: number;
  readonly clientY: number;
}

/** The price pane keeps most of the height; every value pane shares the rest. */
const PRICE_PANE_STRETCH = 3;

function minuteOf(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'local', granularity: 'minute' });
}

/**
 * The bot's own decision candles on a lightweight-charts canvas (#2639).
 *
 * Candles are the strategy view's bars, placed at their close and shaded by
 * the active gate's result as the backend recorded it. Each declared value
 * with a pane is a line from the bot's recorded values; a band draws two
 * dashed levels on its pane. Catalogue indicators the viewer adds are drawn
 * thinner, from the chart's own computation on these candles, on the price
 * pane or on panes after the strategy's. The before-start shade, the run's
 * start and end lines and the selected candle's band are one overlay
 * primitive per pane. Clicking a candle reports it; the host owns selection.
 */
@Component({
  selector: 'app-strategy-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div #chartContainer class="strategy-chart" role="img" [attr.aria-label]="ariaLabel()"></div>`,
  styles: `
    :host { display: block; min-height: 0; }
    .strategy-chart { width: 100%; height: 100%; min-height: 22rem; cursor: pointer; }
  `,
})
export class StrategyChartComponent implements AfterViewInit {
  readonly view = input.required<StrategyViewResponse>();
  /** The active gate; `null` shades every candle dark. */
  readonly gateId = input.required<string | null>();
  readonly selectedBarCloseMs = input<number | null>(null);
  /** Chart-computed catalogue lines, placed at each candle's close. */
  readonly indicatorPlans = input<readonly IndicatorSeriesPlan[]>([]);

  readonly candleClicked = output<StrategyCandleClick>();

  private readonly chartContainer = viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');
  private readonly destroyRef = inject(DestroyRef);
  private readonly createChart = inject(STRATEGY_CHART_FACTORY);

  protected readonly ariaLabel = computed(() => {
    const view = this.view();
    return `${view.strategy_name} decision candles for ${view.symbol}. Click a candle for its checks.`;
  });

  private readonly linePlans = computed(() => strategyLinePlans(this.view().declaration, this.view().candles));

  private readonly overlayState = computed((): StrategyChartOverlayState => {
    const view = this.view();
    const lines: OverlayLine[] = [];
    const startedAtMs = view.run_started_at_ms ?? null;
    const stoppedAtMs = view.run_stopped_at_ms ?? null;
    if (startedAtMs !== null) {
      lines.push({ atMs: startedAtMs, label: `Bot started ${minuteOf(startedAtMs)}`, emphasis: 'start' });
    }
    if (stoppedAtMs !== null) {
      lines.push({ atMs: stoppedAtMs, label: `Ended ${minuteOf(stoppedAtMs)}`, emphasis: 'end' });
    }
    const beforeStart = view.candles.filter((candle) => candle.phase === 'before_start');
    return {
      bars: view.candles.map((candle) => ({ startMs: candle.bar_start_ms, closeMs: candle.bar_close_ms })),
      shadeBeforeMs: beforeStart.length > 0 ? startedAtMs : null,
      shadeLabel: beforeStart.at(-1)?.phase_text ?? null,
      lines,
      highlightCloseMs: this.selectedBarCloseMs(),
    };
  });

  private chart: IChartApi | null = null;
  private candles: ISeriesApi<'Candlestick'> | null = null;
  private markers: ISeriesMarkersPluginApi<Time> | null = null;
  private priceOverlay: StrategyChartOverlay | null = null;
  private paneOverlays: StrategyChartOverlay[] = [];
  private lineSeries = new Map<string, ISeriesApi<SeriesType>>();
  private computedSeries: ISeriesApi<SeriesType>[] = [];
  private lineSignature: string | null = null;
  /** The run whose bars were last fitted to the width; a new run fits again. */
  private fittedRunId: string | null = null;
  private fitPending = false;

  constructor() {
    effect(() => this.renderCandles());
    effect(() => this.renderLines());
    effect(() => {
      const plans = this.indicatorPlans();
      untracked(() => this.renderComputed(plans));
    });
    effect(() => this.renderOverlays());
    effect(() => {
      const selected = this.selectedBarCloseMs();
      untracked(() => this.revealSelected(selected));
    });
  }

  ngAfterViewInit(): void {
    const chart = this.createChart(this.chartContainer().nativeElement, {
      layout: {
        background: { color: 'transparent' },
        textColor: '#9598a1',
        fontFamily: 'JetBrains Mono, SFMono-Regular, Consolas, monospace',
      },
      grid: {
        vertLines: { color: 'rgba(42, 46, 57, 0.55)' },
        horzLines: { color: 'rgba(42, 46, 57, 0.55)' },
      },
      rightPriceScale: { borderColor: '#2a2e39' },
      timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false },
      localization: { timeFormatter: (time: Time) => formatChartCrosshairTime(time, 'local') },
      autoSize: true,
    });
    chart.applyOptions({
      timeScale: {
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => formatChartAxisTick(time, undefined, tickMarkType),
      },
    });
    this.chart = chart;
    this.candles = chart.addSeries(CandlestickSeries, { priceLineVisible: false });
    this.markers = createSeriesMarkers(this.candles, []);
    this.priceOverlay = new StrategyChartOverlay(true);
    this.candles.attachPrimitive(this.priceOverlay);
    const onClick = (param: MouseEventParams<Time>) => this.onChartClick(param);
    chart.subscribeClick(onClick);
    // An auto-sized chart learns its width a frame after it is created; a fit
    // before then packs every bar into the right edge.
    const onSizeChange = () => this.fitIfPending();
    chart.timeScale().subscribeSizeChange(onSizeChange);
    this.destroyRef.onDestroy(() => {
      chart.timeScale().unsubscribeSizeChange(onSizeChange);
      chart.unsubscribeClick(onClick);
      chart.remove();
      this.chart = null;
      this.candles = null;
      this.markers = null;
      this.priceOverlay = null;
      this.paneOverlays = [];
      this.lineSeries.clear();
      this.computedSeries = [];
    });
    this.renderCandles();
    this.renderLines();
    this.renderComputed(this.indicatorPlans());
    this.renderOverlays();
  }

  private renderCandles(): void {
    const view = this.view();
    const gateId = this.gateId();
    if (this.candles === null) return;
    this.candles.setData(toGateShadedCandles(view.candles, gateId));
    this.markers?.setMarkers(decisionMarkers(view.candles));
    if (view.candles.length > 0 && this.fittedRunId !== view.run_id) {
      this.fittedRunId = view.run_id;
      this.fitPending = true;
    }
    this.fitIfPending();
  }

  /** Open a new run on its own bars (plus a session before them), once the chart has a width. */
  private fitIfPending(): void {
    const timeScale = this.chart?.timeScale();
    if (!this.fitPending || timeScale === undefined || timeScale.width() <= 0) return;
    this.fitPending = false;
    const view = this.view();
    const range = openingRange(view.candles, view.run_started_at_ms ?? null);
    if (range === null) timeScale.fitContent();
    else timeScale.setVisibleLogicalRange(range);
  }

  /** A row selected beside the chart brings its candle on screen. */
  private revealSelected(barCloseMs: number | null): void {
    const timeScale = this.chart?.timeScale();
    if (barCloseMs === null || timeScale === undefined) return;
    const index = this.view().candles.findIndex((candle) => candle.bar_close_ms === barCloseMs);
    const visible = timeScale.getVisibleLogicalRange();
    if (index < 0 || visible === null) return;
    const next = rangeRevealing(visible, index);
    if (next !== null) timeScale.setVisibleLogicalRange(next);
  }

  /** A refresh with the same lines on the same panes only re-reads their
   * points, so a new decision never rebuilds panes under the viewer. */
  private renderLines(): void {
    const plans = this.linePlans();
    const chart = this.chart;
    if (chart === null) return;
    const signature = plans.map((plan) => `${plan.key}@${plan.paneIndex}:${plan.band?.join('-') ?? ''}`).join('|');
    if (signature !== this.lineSignature) {
      this.rebuildLines(chart, plans);
      this.lineSignature = signature;
    }
    for (const plan of plans) this.lineSeries.get(plan.key)?.setData([...plan.points]);
  }

  private rebuildLines(chart: IChartApi, plans: readonly StrategyLinePlan[]): void {
    // The catalogue's panes come after the strategy's, so they go first and return last.
    this.clearComputed(chart);
    for (const series of this.lineSeries.values()) chart.removeSeries(series);
    this.lineSeries.clear();
    this.paneOverlays = [];
    const overlaidPanes = new Set<number>([0]);
    for (const plan of plans) {
      const series = chart.addSeries(LineSeries, {
        color: lineColorHex(plan.color),
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      }, plan.paneIndex);
      for (const price of plan.band ?? []) {
        series.createPriceLine({ price, color: BAND_LINE_COLOR, lineWidth: 1, lineStyle: 2, axisLabelVisible: true });
      }
      if (!overlaidPanes.has(plan.paneIndex)) {
        const overlay = new StrategyChartOverlay(false);
        series.attachPrimitive(overlay);
        this.paneOverlays.push(overlay);
        overlaidPanes.add(plan.paneIndex);
      }
      this.lineSeries.set(plan.key, series);
    }
    const panes = chart.panes();
    panes[0]?.setStretchFactor(PRICE_PANE_STRETCH);
    for (const pane of panes.slice(1)) pane.setStretchFactor(1);
    untracked(() => {
      this.renderComputed(this.indicatorPlans());
      this.renderOverlays();
    });
  }

  /** Redraws the catalogue lines: overlays on the price pane, the rest on
   * panes of their own after the strategy's (one pane per indicator panel). */
  private renderComputed(plans: readonly IndicatorSeriesPlan[]): void {
    const chart = this.chart;
    if (chart === null) return;
    this.clearComputed(chart);
    const firstFreePane = Math.max(0, ...this.linePlans().map((plan) => plan.paneIndex)) + 1;
    const paneIndices = new Map<string, number>();
    for (const plan of plans) {
      let paneIndex = 0;
      if (plan.pane !== 'main') {
        paneIndex = paneIndices.get(plan.pane) ?? firstFreePane + paneIndices.size;
        paneIndices.set(plan.pane, paneIndex);
      }
      const options = { color: plan.color, priceLineVisible: false, lastValueVisible: false };
      const series = plan.type === 'histogram'
        ? chart.addSeries(HistogramSeries, options, paneIndex)
        : chart.addSeries(LineSeries, { ...options, lineWidth: 1, crosshairMarkerVisible: false }, paneIndex);
      series.setData(plan.points.map((point) => ({ time: point.time, value: point.value })));
      for (const price of plan.referenceLevels) {
        series.createPriceLine({ price, color: BAND_LINE_COLOR, lineWidth: 1, lineStyle: 2, axisLabelVisible: true });
      }
      this.computedSeries.push(series);
    }
  }

  private clearComputed(chart: IChartApi): void {
    for (const series of this.computedSeries) chart.removeSeries(series);
    this.computedSeries = [];
  }

  private renderOverlays(): void {
    const state = this.overlayState();
    this.priceOverlay?.update(state);
    for (const overlay of this.paneOverlays) overlay.update(state);
  }

  private onChartClick(param: MouseEventParams<Time>): void {
    if (typeof param.time !== 'number' || param.point === undefined) return;
    const candle = this.view().candles.find((each) => toChartTime(each.bar_close_ms) === param.time);
    if (candle === undefined) return;
    const bounds = this.chartContainer().nativeElement.getBoundingClientRect();
    this.candleClicked.emit({
      barCloseMs: candle.bar_close_ms,
      clientX: param.sourceEvent?.clientX ?? bounds.left + param.point.x,
      clientY: param.sourceEvent?.clientY ?? bounds.top + param.point.y,
    });
  }
}
