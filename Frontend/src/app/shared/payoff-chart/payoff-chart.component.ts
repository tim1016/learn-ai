import {
  Component, input, signal, computed, effect, viewChild, ElementRef,
  ChangeDetectionStrategy, OnDestroy, afterNextRender, inject, Injector,
} from '@angular/core';
import { TitleCasePipe } from '@angular/common';
import {
  createChart, LineSeries, BaselineSeries, LineStyle, CrosshairMode,
  type IChartApi, type ISeriesApi, type UTCTimestamp, type MouseEventParams,
} from 'lightweight-charts';
import {
  PayoffPoint, ChartCurveData, GreekCurvePoint, GreekType,
} from '../../graphql/types';

@Component({
  selector: 'app-payoff-chart',
  standalone: true,
  imports: [TitleCasePipe],
  templateUrl: './payoff-chart.component.html',
  styleUrls: ['./payoff-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PayoffChartComponent implements OnDestroy {
  // ── Inputs (same public API — unused inputs kept for future phases) ─
  expirationCurve = input<PayoffPoint[]>([]);
  currentPnlCurve = input<PayoffPoint[]>([]);
  whatIfCurves = input<ChartCurveData[]>([]);
  greekCurve = input<GreekCurvePoint[]>([]);
  selectedGreek = input<GreekType>('delta');
  breakevens = input<number[]>([]);
  spotPrice = input(0);
  weightedIv = input(0.2);
  timeToExpiry = input(0);
  riskFreeRate = input(0.043);
  height = input(450);

  // ── Template refs ──────────────────────────────────────────────────
  private chartEl = viewChild<ElementRef<HTMLDivElement>>('chartContainer');
  private tooltipEl = viewChild<ElementRef<HTMLDivElement>>('tooltip');

  // ── Chart instances ────────────────────────────────────────────────
  private chart: IChartApi | null = null;
  private container: HTMLDivElement | null = null;
  private expSeries: ISeriesApi<'Baseline'> | null = null;
  private currSeries: ISeriesApi<'Line'> | null = null;
  private greekSeries: ISeriesApi<'Line'> | null = null;

  // True once the user has wheeled or pinched the chart. While set,
  // syncData() stops calling fitContent() so leg edits / what-if
  // toggles don't snap the visible range back. Cleared by resetZoom().
  // Detected via DOM wheel/touchstart on the container — more
  // reliable than disambiguating user vs programmatic range changes
  // through timeScale().subscribeVisibleLogicalRangeChange().
  readonly userZoomed = signal(false);
  /**
   * Scenario-id → dashed line series. Lazily created when a scenario
   * is enabled; removed when disabled. Keying by label keeps the chart
   * in sync with whatever the parent's `whatIfScenarios` signal emits.
   */
  private whatIfSeries = new Map<string, ISeriesApi<'Line'>>();
  private injector = inject(Injector);

  /** Greek display name for tooltip */
  private readonly greekLabels: Record<string, string> = {
    delta: 'Delta (Δ)',
    gamma: 'Gamma (Γ)',
    theta: 'Theta (Θ)',
    vega: 'Vega (V)',
    rho: 'Rho (ρ)',
  };

  hasData = computed(() => this.expirationCurve().length > 0);

  constructor() {
    afterNextRender(() => {
      this.bootstrap();
      // Data-sync effect — created after chart exists
      effect(() => this.syncData(), { injector: this.injector });
    });
  }

  // ── Chart creation ─────────────────────────────────────────────────

  private bootstrap(): void {
    const container = this.chartEl()?.nativeElement;
    if (!container) return;
    this.container = container;
    container.addEventListener('wheel', this.onUserZoom, { passive: true });
    container.addEventListener('touchstart', this.onUserZoom, { passive: true });

    this.chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { color: 'transparent' },
        textColor: '#9ca3af',
        fontFamily: "'Inter', system-ui, sans-serif",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.06)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.06)' },
      },
      rightPriceScale: {
        borderVisible: false,
      },
      leftPriceScale: {
        borderVisible: false,
        visible: false, // toggled on when greek data arrives
      },
      timeScale: {
        borderVisible: false,
        fixLeftEdge: true,
        fixRightEdge: true,
        tickMarkFormatter: (time: number) => `$${Number(time).toFixed(0)}`,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      localization: {
        priceFormatter: (price: number) => {
          const pfx = price >= 0 ? '+' : '';
          return `${pfx}$${price.toFixed(2)}`;
        },
        timeFormatter: (time: number) => `$${Number(time).toFixed(2)}`,
      },
    });

    // 1. Expiration P&L — baseline series (green fill above / red below zero)
    this.expSeries = this.chart.addSeries(BaselineSeries, {
      baseValue: { type: 'price', price: 0 },
      topLineColor: 'rgba(34, 197, 94, 0.9)',
      topFillColor1: 'rgba(34, 197, 94, 0.28)',
      topFillColor2: 'rgba(34, 197, 94, 0.05)',
      bottomLineColor: 'rgba(239, 68, 68, 0.9)',
      bottomFillColor1: 'rgba(239, 68, 68, 0.05)',
      bottomFillColor2: 'rgba(239, 68, 68, 0.28)',
      lineWidth: 2,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
    });

    // Zero reference line
    this.expSeries.createPriceLine({
      price: 0,
      color: 'rgba(120, 120, 120, 0.4)',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: false,
    });

    // 2. Current (BS-priced) P&L — blue dashed line
    this.currSeries = this.chart.addSeries(LineSeries, {
      color: 'rgba(59, 130, 246, 0.9)',
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
    });

    // 3. Greek curve — orange line on left price scale
    this.greekSeries = this.chart.addSeries(LineSeries, {
      color: 'rgba(251, 146, 60, 0.9)',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 3,
      priceScaleId: 'left',
    });

    // Tooltip via crosshair
    this.chart.subscribeCrosshairMove(p => this.onCrosshair(p));
  }

  // ── Reactive data sync ─────────────────────────────────────────────

  private syncData(): void {
    // Read signals to track dependencies
    const pts = this.expirationCurve();
    const curr = this.currentPnlCurve();
    const greekPts = this.greekCurve();
    const whatIfs = this.whatIfCurves();
    const spot = this.spotPrice();

    if (!this.chart || !this.expSeries || !this.currSeries || !this.greekSeries) return;

    if (pts.length === 0) {
      this.expSeries.setData([]);
      this.currSeries.setData([]);
      this.greekSeries.setData([]);
      this.removeAllWhatIfSeries();
      this.chart.applyOptions({ leftPriceScale: { visible: false } });
      return;
    }

    // Expiration P&L — price on x-axis, pnl on y-axis
    this.expSeries.setData(
      pts.map(p => ({ time: p.price as UTCTimestamp, value: p.pnl })),
    );

    // Current P&L
    this.currSeries.setData(
      curr.length > 0
        ? curr.map(p => ({ time: p.price as UTCTimestamp, value: p.pnl }))
        : [],
    );

    // Greek curve — left price scale
    if (greekPts.length > 0) {
      this.greekSeries.setData(
        greekPts.map(p => ({ time: p.price as UTCTimestamp, value: p.value })),
      );
      this.chart.applyOptions({ leftPriceScale: { visible: true } });
    } else {
      this.greekSeries.setData([]);
      this.chart.applyOptions({ leftPriceScale: { visible: false } });
    }

    // What-if scenarios — one dashed series per enabled scenario
    this.syncWhatIfSeries(whatIfs);

    // Preserve the user's zoom across data updates: leg edits and
    // what-if toggles fire syncData(), but we only auto-fit when the
    // user hasn't taken control of the visible range yet.
    if (!this.userZoomed()) {
      this.chart.timeScale().fitContent();
    }
  }

  /**
   * Reset the visible range back to fit-all-data and clear the
   * user-zoom flag so subsequent data updates resume auto-fitting.
   * Called by the in-chart "Reset zoom" button.
   */
  resetZoom(): void {
    this.chart?.timeScale().fitContent();
    this.userZoomed.set(false);
  }

  private onUserZoom = (): void => {
    this.userZoomed.set(true);
  };

  /**
   * Reconcile the on-chart what-if series with the input list:
   * - Add a series for any enabled scenario not yet on the chart
   * - Update data on series whose scenario is still enabled
   * - Remove series for scenarios that have been disabled
   */
  private syncWhatIfSeries(scenarios: ChartCurveData[]): void {
    if (!this.chart) return;

    const incomingLabels = new Set(scenarios.map(s => s.label));

    // Drop series for scenarios that are no longer enabled.
    for (const [label, series] of this.whatIfSeries) {
      if (!incomingLabels.has(label)) {
        this.chart.removeSeries(series);
        this.whatIfSeries.delete(label);
      }
    }

    // Add or update series for enabled scenarios.
    for (const scenario of scenarios) {
      // ChartCurveData.borderDash defaults to [6,3] from the parent;
      // lightweight-charts only exposes preset LineStyle values, so map
      // the presence of a dash array to LineStyle.Dashed.
      const lineStyle = (scenario.borderDash && scenario.borderDash.length > 0)
        ? LineStyle.Dashed
        : LineStyle.Solid;

      let series = this.whatIfSeries.get(scenario.label);
      if (!series) {
        series = this.chart.addSeries(LineSeries, {
          color: scenario.color,
          lineWidth: 2,
          lineStyle,
          crosshairMarkerVisible: true,
          crosshairMarkerRadius: 3,
        });
        this.whatIfSeries.set(scenario.label, series);
      } else {
        // Reapply style on every sync so the line picks up changes when
        // the parent emits a new scenario object that keeps the same
        // label but flips color/borderDash (e.g. theme toggle, user
        // recolor). Without this, the series keeps its first-render
        // style indefinitely.
        series.applyOptions({ color: scenario.color, lineStyle });
      }
      series.setData(
        scenario.points.map(p => ({ time: p.price as UTCTimestamp, value: p.pnl })),
      );
    }
  }

  private removeAllWhatIfSeries(): void {
    if (!this.chart) return;
    for (const series of this.whatIfSeries.values()) {
      this.chart.removeSeries(series);
    }
    this.whatIfSeries.clear();
  }

  // ── Crosshair tooltip ──────────────────────────────────────────────

  private onCrosshair(param: MouseEventParams): void {
    const tip = this.tooltipEl()?.nativeElement;
    if (!tip) return;

    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
      tip.style.display = 'none';
      return;
    }

    const price = Number(param.time);
    const expRaw = param.seriesData.get(this.expSeries!) as any;
    const currRaw = param.seriesData.get(this.currSeries!) as any;
    const greekRaw = param.seriesData.get(this.greekSeries!) as any;
    const expVal: number | null = expRaw?.value ?? null;
    const currVal: number | null = currRaw?.value ?? null;
    const greekVal: number | null = greekRaw?.value ?? null;

    if (expVal == null && currVal == null && greekVal == null) {
      tip.style.display = 'none';
      return;
    }

    const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;

    // Build the tooltip via DOM nodes rather than innerHTML to avoid
    // XSS — what-if `label` and `color` originate from
    // `whatIfCurves` (caller-supplied) and would otherwise flow into
    // the parsed HTML string.
    tip.replaceChildren();
    tip.appendChild(this.buildTooltipTitle(`Underlying: $${price.toFixed(2)}`));

    if (expVal != null) {
      tip.appendChild(this.buildTooltipRow(['tt-dot', 'tt-exp'], `Exp P&L: ${fmt(expVal)}`));
    }
    if (currVal != null) {
      tip.appendChild(this.buildTooltipRow(['tt-dot', 'tt-curr'], `Cur P&L: ${fmt(currVal)}`));
    }
    if (greekVal != null) {
      const greekLabel = this.greekLabels[this.selectedGreek()] ?? this.selectedGreek();
      tip.appendChild(
        this.buildTooltipRow(['tt-dot', 'tt-greek'], `${greekLabel}: ${greekVal.toFixed(4)}`),
      );
    }

    // What-if scenario rows — one per enabled series
    for (const [label, series] of this.whatIfSeries) {
      const raw = param.seriesData.get(series) as { value?: number } | undefined;
      const v = raw?.value;
      if (v == null) continue;
      const scenario = this.whatIfCurves().find(s => s.label === label);
      tip.appendChild(
        this.buildTooltipRow(['tt-dot'], `${label}: ${fmt(v)}`, scenario?.color),
      );
    }

    tip.style.display = 'block';

    const box = this.chartEl()?.nativeElement;
    if (!box) return;
    const tw = tip.offsetWidth;
    tip.style.left = (param.point.x + tw + 20 > box.clientWidth)
      ? `${param.point.x - tw - 12}px`
      : `${param.point.x + 12}px`;
    tip.style.top = `${Math.max(param.point.y - 20, 0)}px`;
  }

  private buildTooltipTitle(text: string): HTMLDivElement {
    const div = document.createElement('div');
    div.className = 'tt-title';
    div.textContent = text;
    return div;
  }

  private buildTooltipRow(
    dotClasses: string[],
    text: string,
    dotColor?: string,
  ): HTMLDivElement {
    const row = document.createElement('div');
    row.className = 'tt-row';
    const dot = document.createElement('span');
    dot.classList.add(...dotClasses);
    if (dotColor) {
      // setProperty (vs string concat into a style attribute) treats
      // the value as a CSS token, not a parsed style declaration —
      // an attacker-supplied "red; background:url(...)" can't escape
      // the `background-color` property.
      dot.style.setProperty('background-color', dotColor);
    }
    row.appendChild(dot);
    row.appendChild(document.createTextNode(text));
    return row;
  }

  // ── Cleanup ────────────────────────────────────────────────────────

  ngOnDestroy(): void {
    if (this.container) {
      this.container.removeEventListener('wheel', this.onUserZoom);
      this.container.removeEventListener('touchstart', this.onUserZoom);
    }
    this.chart?.remove();
    this.chart = null;
    this.container = null;
    this.expSeries = null;
    this.currSeries = null;
    this.greekSeries = null;
    this.whatIfSeries.clear();
  }
}
