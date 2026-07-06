import { CommonModule, DecimalPipe, PercentPipe } from '@angular/common';
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  OnDestroy,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MessageModule } from 'primeng/message';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import {
  IChartApi,
  ISeriesApi,
  LineSeries,
  createChart,
  type LineData,
  type Time,
} from 'lightweight-charts';

import { MonteCarloService } from '../../../../services/monte-carlo.service';
import type {
  MonteCarloResponse,
  MonteCarloStatus,
} from '../../../../services/monte-carlo.types';
import { TimestampDisplayPipe } from '../../../../shared/timestamp';

const CHART_THEME = {
  bg: '#0f172a',
  text: '#cbd5e1',
  grid: 'rgba(148, 163, 184, 0.12)',
  border: 'rgba(148, 163, 184, 0.25)',
  crosshair: '#94a3b8',
  surface: '#1e293b',
};

const P5_COLOR = 'rgba(248, 113, 113, 0.8)'; // muted red — pessimistic tail
const P50_COLOR = '#60a5fa'; // medium blue — central tendency
const P95_COLOR = 'rgba(74, 222, 128, 0.8)'; // muted green — optimistic tail

/**
 * Full-page detail view for a Monte Carlo analysis at
 * ``/research-lab/monte-carlo/:mc_id``. Loads ``(config, result)``
 * via ``MonteCarloService.getMonteCarlo`` and renders:
 *
 *   * Header: status, parent-run link, method, simulation count.
 *   * Aggregate quantile cards: drawdown / terminal PnL / max losing
 *     streak {p5, p50, p95}.
 *   * Equity-band fan chart — three lightweight-charts ``LineSeries``
 *     (P5 dashed-feel via low opacity, P50 solid blue, P95 muted
 *     green). Trade-index serves as the time axis (we don't have
 *     real bar timestamps for simulated paths, only ordinal trade
 *     positions).
 *   * Breach-probability table — per client-supplied threshold,
 *     the fraction of simulations that hit at least that drawdown.
 *
 * The equity-band time axis uses ``trade_index`` cast as
 * ``UTCTimestamp`` — a deliberate abuse of the Time type to feed
 * lightweight-charts an integer ordinal. It works because the chart
 * doesn't try to interpret the values as wall-clock time when
 * ``timeVisible: false``; the labels render as raw integers.
 */
@Component({
  selector: 'app-monte-carlo-detail-page',
  imports: [
    CommonModule,
    RouterLink,
    MessageModule,
    TableModule,
    TagModule,
    DecimalPipe,
    PercentPipe,
    TimestampDisplayPipe,
  ],
  templateUrl: './monte-carlo-detail-page.component.html',
  styleUrls: ['./monte-carlo-detail-page.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MonteCarloDetailPageComponent implements AfterViewInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly service = inject(MonteCarloService);
  private readonly destroyRef = inject(DestroyRef);

  readonly chartEl = viewChild<ElementRef<HTMLDivElement>>('bandChart');

  readonly monteCarlo = signal<MonteCarloResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly mcId = signal<string | null>(null);

  private chart: IChartApi | null = null;
  private p5Series: ISeriesApi<'Line'> | null = null;
  private p50Series: ISeriesApi<'Line'> | null = null;
  private p95Series: ISeriesApi<'Line'> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  // Monotonic load token. Each call to ``load()`` increments it and
  // captures the new value; after the awaited fetch resolves, the
  // call only applies its response if its captured token still
  // matches the current one. This prevents an out-of-order earlier
  // response from overwriting a later one when the user navigates
  // between mc_ids in quick succession.
  private loadToken = 0;

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const id = params.get('mc_id');
      this.mcId.set(id);
      if (id) {
        void this.load(id);
      }
    });

    effect(() => {
      const data = this.monteCarlo();
      if (!data) return;
      this.renderBands(data);
    });
  }

  ngAfterViewInit(): void {
    this.createChart();
    const data = this.monteCarlo();
    if (data) this.renderBands(data);
  }

  ngOnDestroy(): void {
    this.chart?.remove();
    this.resizeObserver?.disconnect();
  }

  async load(mcId: string): Promise<void> {
    const token = ++this.loadToken;
    // Drop any prior payload before fetching so a failed load (404,
    // network error) doesn't leave the previous mc_id's quantiles
    // and bands rendered alongside the new error message.
    this.monteCarlo.set(null);
    this.loading.set(true);
    this.error.set(null);
    try {
      const data = await this.service.getMonteCarlo(mcId);
      // Stale-response guard: another ``load()`` started while this
      // one was in flight. The newer call owns ``monteCarlo``; this
      // call's response would be for the previous mc_id.
      if (token !== this.loadToken) return;
      this.monteCarlo.set(data);
    } catch (err) {
      if (token !== this.loadToken) return;
      this.error.set(this.formatError(err));
    } finally {
      if (token === this.loadToken) {
        this.loading.set(false);
      }
    }
  }

  statusSeverity(status: MonteCarloStatus): 'success' | 'danger' {
    return status === 'completed' ? 'success' : 'danger';
  }

  shortHash(value: string | null | undefined, len = 16): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  // ────────────────────────────────────────────────────────────────
  // Chart
  // ────────────────────────────────────────────────────────────────
  private createChart(): void {
    const el = this.chartEl()?.nativeElement;
    if (!el) return;

    this.chart = createChart(el, {
      width: el.clientWidth,
      height: 320,
      layout: { background: { color: CHART_THEME.bg }, textColor: CHART_THEME.text },
      grid: {
        vertLines: { color: CHART_THEME.grid },
        horzLines: { color: CHART_THEME.grid },
      },
      timeScale: {
        // Trade-index axis — render the integers without trying to
        // format them as dates.
        timeVisible: false,
        secondsVisible: false,
        borderColor: CHART_THEME.border,
      },
      crosshair: {
        mode: 0,
        vertLine: {
          color: CHART_THEME.crosshair,
          labelBackgroundColor: CHART_THEME.surface,
        },
        horzLine: {
          color: CHART_THEME.crosshair,
          labelBackgroundColor: CHART_THEME.surface,
        },
      },
      rightPriceScale: { borderColor: CHART_THEME.border },
    });

    this.p5Series = this.chart.addSeries(LineSeries, {
      color: P5_COLOR,
      lineWidth: 1,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) =>
          '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
      },
    });
    this.p50Series = this.chart.addSeries(LineSeries, {
      color: P50_COLOR,
      lineWidth: 2,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) =>
          '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
      },
    });
    this.p95Series = this.chart.addSeries(LineSeries, {
      color: P95_COLOR,
      lineWidth: 1,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) =>
          '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
      },
    });

    this.resizeObserver = new ResizeObserver(() => {
      this.chart?.applyOptions({ width: el.clientWidth });
    });
    this.resizeObserver.observe(el);
  }

  private renderBands(data: MonteCarloResponse): void {
    if (!this.p5Series || !this.p50Series || !this.p95Series) return;
    const p5: LineData[] = [];
    const p50: LineData[] = [];
    const p95: LineData[] = [];
    for (const point of data.result.equity_bands) {
      // ``trade_index`` doubles as the time axis. Cast through ``Time``
      // because lightweight-charts' typings expect a UTCTimestamp here;
      // with timeVisible:false the renderer treats it as an integer
      // ordinal and labels it as raw int.
      const t = point.trade_index as unknown as Time;
      p5.push({ time: t, value: point.p5 });
      p50.push({ time: t, value: point.p50 });
      p95.push({ time: t, value: point.p95 });
    }
    this.p5Series.setData(p5);
    this.p50Series.setData(p50);
    this.p95Series.setData(p95);
    this.chart?.timeScale().fitContent();
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}
