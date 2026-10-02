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
 * dashed levels on its pane. The before-start shade, the run's start and end
 * lines and the selected candle's band are one overlay primitive per pane.
 * Clicking a candle reports it; the host owns selection.
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
  readonly gateId = input.required<string>();
  readonly selectedBarCloseMs = input<number | null>(null);

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
  private lineSignature: string | null = null;
  /** The run whose bars were last fitted to the width; a new run fits again. */
  private fittedRunId: string | null = null;
  private fitPending = false;

  constructor() {
    effect(() => this.renderCandles());
    effect(() => this.renderLines());
    effect(() => this.renderOverlays());
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
    });
    this.renderCandles();
    this.renderLines();
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

  /** Fit a new run's bars to the width, once the chart has one. */
  private fitIfPending(): void {
    const timeScale = this.chart?.timeScale();
    if (!this.fitPending || timeScale === undefined || timeScale.width() <= 0) return;
    this.fitPending = false;
    timeScale.fitContent();
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
    untracked(() => this.renderOverlays());
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
