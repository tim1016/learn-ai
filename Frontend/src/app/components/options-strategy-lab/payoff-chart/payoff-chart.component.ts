import {
  Component, input, computed, effect, viewChild, ElementRef,
  ChangeDetectionStrategy, OnDestroy, afterNextRender, inject, Injector,
} from '@angular/core';
import {
  createChart, LineSeries, BaselineSeries, LineStyle, CrosshairMode,
  type IChartApi, type ISeriesApi, type UTCTimestamp, type MouseEventParams,
} from 'lightweight-charts';
import {
  PayoffPoint, ChartCurveData, GreekCurvePoint, GreekType,
} from '../../../graphql/types';

@Component({
  selector: 'app-payoff-chart',
  standalone: true,
  imports: [],
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
  private expSeries: ISeriesApi<'Baseline'> | null = null;
  private currSeries: ISeriesApi<'Line'> | null = null;
  private injector = inject(Injector);

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

    // Tooltip via crosshair
    this.chart.subscribeCrosshairMove(p => this.onCrosshair(p));
  }

  // ── Reactive data sync ─────────────────────────────────────────────

  private syncData(): void {
    // Read signals to track dependencies
    const pts = this.expirationCurve();
    const curr = this.currentPnlCurve();
    const spot = this.spotPrice();

    if (!this.chart || !this.expSeries || !this.currSeries) return;

    if (pts.length === 0) {
      this.expSeries.setData([]);
      this.currSeries.setData([]);
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

    this.chart.timeScale().fitContent();
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
    const expVal: number | null = expRaw?.value ?? null;
    const currVal: number | null = currRaw?.value ?? null;

    if (expVal == null && currVal == null) {
      tip.style.display = 'none';
      return;
    }

    const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;
    let html = `<div class="tt-title">Underlying: $${price.toFixed(2)}</div>`;
    if (expVal != null) {
      html += `<div class="tt-row"><span class="tt-dot tt-exp"></span>Exp P&L: ${fmt(expVal)}</div>`;
    }
    if (currVal != null) {
      html += `<div class="tt-row"><span class="tt-dot tt-curr"></span>Cur P&L: ${fmt(currVal)}</div>`;
    }

    tip.innerHTML = html;
    tip.style.display = 'block';

    const box = this.chartEl()?.nativeElement;
    if (!box) return;
    const tw = tip.offsetWidth;
    tip.style.left = (param.point.x + tw + 20 > box.clientWidth)
      ? `${param.point.x - tw - 12}px`
      : `${param.point.x + 12}px`;
    tip.style.top = `${Math.max(param.point.y - 20, 0)}px`;
  }

  // ── Cleanup ────────────────────────────────────────────────────────

  ngOnDestroy(): void {
    this.chart?.remove();
    this.chart = null;
    this.expSeries = null;
    this.currSeries = null;
  }
}
