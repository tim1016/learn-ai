import { afterNextRender, ChangeDetectionStrategy, Component, computed, DestroyRef, effect, ElementRef, inject, InjectionToken, input, signal, untracked, viewChild } from '@angular/core';
import {
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesMarker,
  type TickMarkType,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';

import { createAppChart, formatChartAxisTick } from '../../../../shared/charts/chart-utils';
import { themeColor } from '../../../../shared/charts/theme-color';
import { CHART_SERIES_COLOR_TOKENS, type ChartSeriesColorToken } from '../../../../shared/trading-chart/chart-series-color-tokens';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { PlanLineStyle, PlanMarker, SeriesPlan, TimelinePlan } from '../timeline-plan';
import { labelReachPx, rangeWithRoom, slotCount } from './timeline-room';

/** How the chart is made. A spec hands in a fake, so no canvas is needed. */
export const TIMELINE_CHART_FACTORY = new InjectionToken<typeof createAppChart>('TIMELINE_CHART_FACTORY', { providedIn: 'root', factory: () => createAppChart });

/** How a theme token becomes a colour at the chart's boundary: the one place a colour is read. */
export const TIMELINE_COLOURS = new InjectionToken<(host: HTMLElement, cssVar: string) => string>('TIMELINE_COLOURS', { providedIn: 'root', factory: () => themeColor });

const PANE_HEIGHT_PX = 190;
const AXIS_HEIGHT_PX = 34;
/** A minute that traded alone is a line of one point, a stroke a few pixels long; it is drawn as a dot of this radius instead. */
const LONE_POINT_RADIUS_PX = 3;
/** Session times are New York times: this is a session view, not the viewer's own clock. */
const NEW_YORK = 'America/New_York';

const LINE_STYLES: Readonly<Record<PlanLineStyle, LineStyle>> = {
  solid: LineStyle.Solid,
  dotted: LineStyle.Dotted,
  dashed: LineStyle.Dashed,
  'large-dashed': LineStyle.LargeDashed,
};

/** The chart wants seconds as a branded number; every instant of the plan is already whole seconds, so the brand is all that is added. */
const chartTime = (seconds: number): UTCTimestamp => seconds as UTCTimestamp;

interface Tip {
  /** The minute under the cursor, named by its close: a fill in the tooltip filled in that minute, not at that instant. */
  readonly heading: string;
  readonly lines: readonly string[];
  readonly left: number;
  readonly top: number;
  readonly flip: boolean;
}

/**
 * The one-day chart: a thin wrapper that hands a {@link TimelinePlan} to `lightweight-charts` and draws nothing of its
 * own. It is made once the view has rendered, redraws when the plan changes, and is taken down with the view.
 *
 * The day is always shown whole, with room at both ends for a marker's label (see `timeline-room`): the library's own edge
 * fixing would put the first and last minute against the edges and cut the label off, so it is off, and the chart is not
 * scrolled or zoomed, so that the day cannot be moved out of view. The room is worked out again when the width changes.
 *
 * The time axis and the crosshair read in New York time through the shared timestamp helpers. Colours are read from the
 * theme's chart tokens here and nowhere else. The crosshair's own price label is switched off: a price a person reads is
 * the server's text, and the tooltip gives it for the minute under the cursor. The axis labels are the library's own
 * positions on the price scale and carry no data point.
 */
@Component({
  selector: 'app-options-backtest-timeline-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './timeline-chart.component.html',
  styleUrl: './timeline-chart.component.scss',
})
export class TimelineChartComponent {
  private readonly createChart = inject(TIMELINE_CHART_FACTORY);
  private readonly colour = inject(TIMELINE_COLOURS);
  private readonly destroyRef = inject(DestroyRef);

  readonly plan = input.required<TimelinePlan>();

  private readonly canvas = viewChild.required<ElementRef<HTMLDivElement>>('canvas');
  protected readonly tip = signal<Tip | null>(null);
  protected readonly height = computed(() => this.plan().panes.length * PANE_HEIGHT_PX + AXIS_HEIGHT_PX);

  private chart: IChartApi | null = null;
  private drawn: ISeriesApi<'Line'>[] = [];
  /** What the day on the chart takes: how many places on the time scale, and how far its longest label reaches. */
  private slots = 0;
  private reachPx = 0;
  private readonly onCrosshair = (param: MouseEventParams<Time>): void => this.showTip(param);
  private readonly onWidth = (): void => this.fit();

  constructor() {
    afterNextRender(() => this.start());
    effect(() => {
      const plan = this.plan();
      untracked(() => this.draw(plan));
    });
    this.destroyRef.onDestroy(() => this.stop());
  }

  private start(): void {
    const host = this.canvas().nativeElement;
    const border = this.colour(host, '--border-light');
    const chart = this.createChart(host, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: this.colour(host, '--text-secondary'), panes: { separatorColor: border, separatorHoverColor: border, enableResize: false } },
      grid: { vertLines: { color: border }, horzLines: { color: border } },
      rightPriceScale: { borderVisible: false },
      crosshair: { horzLine: { labelVisible: false } },
      handleScroll: false,
      handleScale: false,
      localization: { timeFormatter: (time: Time) => crosshairTime(time) },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => formatChartAxisTick(time, NEW_YORK, tickMarkType),
      },
    });
    chart.subscribeCrosshairMove(this.onCrosshair);
    chart.timeScale().subscribeSizeChange(this.onWidth);
    this.chart = chart;
    this.draw(this.plan());
  }

  private stop(): void {
    const chart = this.chart;
    if (chart === null) return;
    chart.unsubscribeCrosshairMove(this.onCrosshair);
    chart.timeScale().unsubscribeSizeChange(this.onWidth);
    chart.remove();
    this.chart = null;
    this.drawn = [];
  }

  /** Takes down what was drawn and draws the plan: every series, with its data and its markers, on its pane. */
  private draw(plan: TimelinePlan): void {
    const chart = this.chart;
    if (chart === null) return;
    for (const series of this.drawn) chart.removeSeries(series);
    this.drawn = [];
    this.tip.set(null);
    this.slots = slotCount(plan);
    this.reachPx = labelReachPx(plan);
    const host = this.canvas().nativeElement;
    for (const each of plan.series) {
      const series = chart.addSeries(
        LineSeries,
        {
          color: this.colourOf(host, each.token),
          lineWidth: each.lineWidth,
          lineStyle: LINE_STYLES[each.lineStyle],
          lineVisible: each.lineVisible,
          pointMarkersVisible: each.pointMarkers,
          pointMarkersRadius: LONE_POINT_RADIUS_PX,
          crosshairMarkerVisible: false,
          lastValueVisible: false,
          priceLineVisible: false,
        },
        each.pane,
      );
      series.setData(dataOf(each));
      if (each.markers.length > 0) createSeriesMarkers(series, each.markers.map((marker) => this.markerOf(host, marker)));
      this.drawn.push(series);
    }
    this.fit();
  }

  /** Shows the whole day with room for its labels at the width the chart has now; the whole day, as it is, while the width is not known. */
  private fit(): void {
    const scale = this.chart?.timeScale();
    if (scale === undefined) return;
    const range = rangeWithRoom(this.slots, scale.width(), this.reachPx);
    if (range === null) scale.fitContent();
    else scale.setVisibleLogicalRange(range);
  }

  private markerOf(host: HTMLElement, marker: PlanMarker): SeriesMarker<Time> {
    return { id: marker.id, time: chartTime(marker.time), position: 'atPriceMiddle', price: marker.price, shape: marker.shape, color: this.colourOf(host, marker.token), text: marker.text };
  }

  private colourOf(host: HTMLElement, token: ChartSeriesColorToken): string {
    const definition = CHART_SERIES_COLOR_TOKENS.get(token);
    if (definition === undefined) throw new Error(`The chart has no colour token ${token}.`);
    return this.colour(host, definition.cssVar);
  }

  /** What the cursor is over: the lines the plan has for that minute, in the server's own text. */
  private showTip(param: MouseEventParams<Time>): void {
    if (typeof param.time !== 'number' || param.point === undefined) {
      this.tip.set(null);
      return;
    }
    const lines = this.plan().tips.get(param.time);
    if (lines === undefined) {
      this.tip.set(null);
      return;
    }
    const width = this.canvas().nativeElement.clientWidth;
    this.tip.set({
      heading: `Minute ending ${formatTimestampDisplay(param.time * 1000, { mode: 'et', granularity: 'minute' })}`,
      lines,
      left: param.point.x,
      top: param.point.y,
      flip: param.point.x > width / 2,
    });
  }
}

function crosshairTime(time: Time): string {
  return typeof time === 'number' ? formatTimestampDisplay(time * 1000, { mode: 'et', granularity: 'chart' }) : String(time);
}

function dataOf(series: SeriesPlan): ({ time: UTCTimestamp; value: number } | { time: UTCTimestamp })[] {
  return series.data.map((datum) => ('value' in datum ? { time: chartTime(datum.time), value: datum.value } : { time: chartTime(datum.time) }));
}
