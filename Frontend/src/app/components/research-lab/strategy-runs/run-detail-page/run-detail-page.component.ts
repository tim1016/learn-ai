import { CommonModule, DatePipe, DecimalPipe, PercentPipe } from '@angular/common';
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
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import {
  AreaSeries,
  createChart,
  IChartApi,
  ISeriesApi,
  type AreaData,
  type Time,
} from 'lightweight-charts';

import { StrategyRunsService } from '../../../../services/strategy-runs.service';
import type {
  RunLedger,
  StrategyRunResponse,
} from '../../../../services/strategy-runs.types';
import { WalkForwardSectionComponent } from './walk-forward-section/walk-forward-section.component';

const CHART_THEME = {
  bg: '#0f172a',
  text: '#cbd5e1',
  grid: 'rgba(148, 163, 184, 0.12)',
  border: 'rgba(148, 163, 184, 0.25)',
  crosshair: '#94a3b8',
  surface: '#1e293b',
};

const EQUITY_COLOR = '#22d3ee';
const DRAWDOWN_COLOR = '#f87171';

/**
 * Full-page detail view for a single strategy run, mounted at
 * ``/research-lab/strategy-runs/:run_id``. Loads the persisted
 * ``(ledger, result)`` pair via ``StrategyRunsService.getRun`` and
 * renders four sections:
 *
 *   * Header with status, spec id, time window, full run_id.
 *   * Provenance card — every identity column from the ledger.
 *   * Metrics card — Sharpe, Sortino, profit factor, exposure, etc.
 *     All values come from the server's ``RunMetrics``; nothing is
 *     computed in Angular.
 *   * Equity + drawdown charts (lightweight-charts v5 ``AreaSeries``).
 *   * Trade table — every round-trip with entry/exit, PnL, indicators.
 *
 * The charts use lightweight-charts' ``UTCTimestamp`` (integer seconds
 * since epoch UTC) — converted from the server's ``int64 ms UTC`` at
 * the render boundary. Per the project's timestamp-rigor rule, the
 * stored format never changes; only the display-format does.
 */
@Component({
  selector: 'app-run-detail-page',
  imports: [
    CommonModule,
    RouterLink,
    ButtonModule,
    MessageModule,
    TableModule,
    TagModule,
    DatePipe,
    DecimalPipe,
    PercentPipe,
    WalkForwardSectionComponent,
  ],
  templateUrl: './run-detail-page.component.html',
  styleUrls: ['./run-detail-page.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RunDetailPageComponent implements AfterViewInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly service = inject(StrategyRunsService);
  private readonly destroyRef = inject(DestroyRef);

  readonly equityChartEl = viewChild<ElementRef<HTMLDivElement>>('equityChart');
  readonly drawdownChartEl = viewChild<ElementRef<HTMLDivElement>>('drawdownChart');

  readonly run = signal<StrategyRunResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly runId = signal<string | null>(null);

  private equityChart: IChartApi | null = null;
  private equitySeries: ISeriesApi<'Area'> | null = null;
  private drawdownChart: IChartApi | null = null;
  private drawdownSeries: ISeriesApi<'Area'> | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const id = params.get('run_id');
      this.runId.set(id);
      if (id) {
        void this.load(id);
      }
    });

    // Re-render charts whenever the run signal updates AND the chart
    // elements have been instantiated. ``effect`` fires on signal
    // changes; the chart DOM is created in ``ngAfterViewInit``, so the
    // first run-load may arrive before charts exist — the effect just
    // skips that pass and re-fires once charts come up.
    effect(() => {
      const data = this.run();
      if (!data) return;
      this.renderEquity(data);
      this.renderDrawdown(data);
    });
  }

  ngAfterViewInit(): void {
    this.createCharts();
    const data = this.run();
    if (data) {
      this.renderEquity(data);
      this.renderDrawdown(data);
    }
  }

  ngOnDestroy(): void {
    this.equityChart?.remove();
    this.drawdownChart?.remove();
    this.resizeObserver?.disconnect();
  }

  async load(runId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const data = await this.service.getRun(runId);
      this.run.set(data);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  statusSeverity(status: RunLedger['status']): 'success' | 'warn' | 'danger' | 'info' {
    switch (status) {
      case 'completed':
        return 'success';
      case 'running':
        return 'info';
      case 'failed':
        return 'danger';
      default:
        return 'warn';
    }
  }

  shortHash(value: string | null | undefined, len = 16): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  // ────────────────────────────────────────────────────────────────
  // Charts
  // ────────────────────────────────────────────────────────────────
  private createCharts(): void {
    const equityEl = this.equityChartEl()?.nativeElement;
    const drawdownEl = this.drawdownChartEl()?.nativeElement;
    if (!equityEl || !drawdownEl) return;

    this.equityChart = createChart(equityEl, this.chartOptions(equityEl));
    this.equitySeries = this.equityChart.addSeries(AreaSeries, {
      lineColor: EQUITY_COLOR,
      topColor: 'rgba(34, 211, 238, 0.35)',
      bottomColor: 'rgba(34, 211, 238, 0.02)',
      lineWidth: 2,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
      },
    });

    this.drawdownChart = createChart(drawdownEl, this.chartOptions(drawdownEl));
    this.drawdownSeries = this.drawdownChart.addSeries(AreaSeries, {
      lineColor: DRAWDOWN_COLOR,
      topColor: 'rgba(248, 113, 113, 0.04)',
      bottomColor: 'rgba(248, 113, 113, 0.4)',
      lineWidth: 2,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) => (v * 100).toFixed(2) + '%',
      },
    });

    // Resize charts when their container changes width.
    this.resizeObserver = new ResizeObserver(() => {
      this.equityChart?.applyOptions({ width: equityEl.clientWidth });
      this.drawdownChart?.applyOptions({ width: drawdownEl.clientWidth });
    });
    this.resizeObserver.observe(equityEl);
    this.resizeObserver.observe(drawdownEl);
  }

  private chartOptions(el: HTMLElement) {
    return {
      width: el.clientWidth,
      height: 240,
      layout: { background: { color: CHART_THEME.bg }, textColor: CHART_THEME.text },
      grid: {
        vertLines: { color: CHART_THEME.grid },
        horzLines: { color: CHART_THEME.grid },
      },
      timeScale: {
        timeVisible: true,
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
    };
  }

  private renderEquity(data: StrategyRunResponse): void {
    if (!this.equitySeries) return;
    const points: AreaData[] = data.result.equity_curve.map((p) => ({
      time: msToUtcSeconds(p.timestamp_ms),
      value: p.equity,
    }));
    this.equitySeries.setData(deduplicateByTime(points));
    this.equityChart?.timeScale().fitContent();
  }

  private renderDrawdown(data: StrategyRunResponse): void {
    if (!this.drawdownSeries) return;
    // Draw drawdown as a *negative* fraction so the area fills downward
    // and the visual matches "we lost X% from peak". The server emits
    // positive fractions in [0, 1]; flip the sign at the render boundary.
    const points: AreaData[] = data.result.drawdown_curve.map((p) => ({
      time: msToUtcSeconds(p.timestamp_ms),
      value: -p.drawdown_pct,
    }));
    this.drawdownSeries.setData(deduplicateByTime(points));
    this.drawdownChart?.timeScale().fitContent();
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}

/**
 * Convert ``int64 ms UTC`` (the wire format) to lightweight-charts'
 * ``UTCTimestamp`` (integer seconds since epoch UTC). Truncates rather
 * than rounds — sub-second precision is meaningless for the bar
 * resolutions this UI handles.
 */
function msToUtcSeconds(ms: number): Time {
  return Math.floor(ms / 1000) as Time;
}

/**
 * lightweight-charts requires strictly-increasing timestamps. The
 * server's equity/drawdown curves are emitted at every minute bar, so
 * after the ms→s truncation in ``msToUtcSeconds`` we may have
 * consecutive points at the same second (only possible if the server
 * emitted two snapshots within the same second; rare but defensive).
 * Deduplicate by keeping the last value at each timestamp.
 */
function deduplicateByTime(points: AreaData[]): AreaData[] {
  const byTime = new Map<number, AreaData>();
  for (const p of points) {
    byTime.set(p.time as number, p);
  }
  return [...byTime.values()].sort((a, b) => (a.time as number) - (b.time as number));
}
