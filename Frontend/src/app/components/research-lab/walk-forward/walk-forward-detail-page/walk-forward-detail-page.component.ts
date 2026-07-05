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
  AreaSeries,
  createChart,
  IChartApi,
  ISeriesApi,
  type AreaData,
  type Time,
} from 'lightweight-charts';

import { WalkForwardService } from '../../../../services/walk-forward.service';
import type {
  FoldResult,
  WalkForwardResponse,
  WalkForwardStatus,
} from '../../../../services/walk-forward.types';
import { TimestampDisplayPipe } from '../../../../shared/timestamp';

const CHART_THEME = {
  bg: '#0f172a',
  text: '#cbd5e1',
  grid: 'rgba(148, 163, 184, 0.12)',
  border: 'rgba(148, 163, 184, 0.25)',
  crosshair: '#94a3b8',
  surface: '#1e293b',
};

const COMBINED_CURVE_COLOR = '#a78bfa';

/**
 * Full-page detail view for a walk-forward analysis, mounted at
 * ``/research-lab/walk-forward/:wf_id``. Loads the persisted
 * ``(config, result)`` pair via ``WalkForwardService.getWalkForward``
 * and renders:
 *
 *   * Header: status, parent-run link (if present), split-policy
 *     summary, full ``walk_forward_id``.
 *   * Aggregate metrics card: mean/median OOS Sharpe,
 *     pct_profitable_folds, alpha_decay, OOS retention.
 *   * Combined OOS equity curve (lightweight-charts AreaSeries).
 *   * Fold table — one row per fold with status, train+test windows,
 *     test metrics; click → navigates to that fold's individual run
 *     detail (``/research-lab/strategy-runs/<test_run_id>``).
 *
 * Same wire-format invariants as Phase B: timestamps stay
 * ``int64 ms UTC`` end-to-end, converted to lightweight-charts'
 * ``UTCTimestamp`` (seconds) only at the chart-render boundary.
 */
@Component({
  selector: 'app-walk-forward-detail-page',
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
  templateUrl: './walk-forward-detail-page.component.html',
  styleUrls: ['./walk-forward-detail-page.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class WalkForwardDetailPageComponent implements AfterViewInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly service = inject(WalkForwardService);
  private readonly destroyRef = inject(DestroyRef);

  readonly chartEl = viewChild<ElementRef<HTMLDivElement>>('combinedChart');

  readonly walkForward = signal<WalkForwardResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly wfId = signal<string | null>(null);

  private chart: IChartApi | null = null;
  private series: ISeriesApi<'Area'> | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const id = params.get('wf_id');
      this.wfId.set(id);
      if (id) {
        void this.load(id);
      }
    });

    effect(() => {
      const data = this.walkForward();
      if (!data) return;
      this.renderCurve(data);
    });
  }

  ngAfterViewInit(): void {
    this.createChart();
    const data = this.walkForward();
    if (data) this.renderCurve(data);
  }

  ngOnDestroy(): void {
    this.chart?.remove();
    this.resizeObserver?.disconnect();
  }

  async load(wfId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const data = await this.service.getWalkForward(wfId);
      this.walkForward.set(data);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  statusSeverity(status: WalkForwardStatus | FoldResult['status']): 'success' | 'danger' {
    return status === 'completed' ? 'success' : 'danger';
  }

  splitSummary(policy: WalkForwardResponse['result']['split_policy']): string {
    switch (policy.kind) {
      case 'chronological':
        return `chronological · train ${formatPct(policy['train_pct'])}`;
      case 'rolling':
        return `rolling · ${policy['train_days']}d train / ${policy['test_days']}d test / ${policy['step_days']}d step`;
      case 'anchored':
        return `anchored · ${policy['initial_train_days']}d initial train / ${policy['test_days']}d test / ${policy['step_days']}d step`;
      default:
        return policy.kind;
    }
  }

  shortHash(value: string | null | undefined, len = 16): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  trackByFoldIndex(_i: number, fold: FoldResult): number {
    return fold.fold_index;
  }

  // ────────────────────────────────────────────────────────────────
  // Chart
  // ────────────────────────────────────────────────────────────────
  private createChart(): void {
    const el = this.chartEl()?.nativeElement;
    if (!el) return;

    this.chart = createChart(el, {
      width: el.clientWidth,
      height: 300,
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
    });

    this.series = this.chart.addSeries(AreaSeries, {
      lineColor: COMBINED_CURVE_COLOR,
      topColor: 'rgba(167, 139, 250, 0.35)',
      bottomColor: 'rgba(167, 139, 250, 0.02)',
      lineWidth: 2,
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

  private renderCurve(data: WalkForwardResponse): void {
    if (!this.series) return;
    const points: AreaData[] = data.result.combined_oos_equity_curve.map((p) => ({
      time: msToUtcSeconds(p.timestamp_ms),
      value: p.equity,
    }));
    this.series.setData(deduplicateByTime(points));
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

/** Same converters as Phase B's run-detail page — kept inline rather
 * than extracting to a shared module since the surface is small and
 * lightweight-charts time-format choice is a chart-layer concern.
 */
function msToUtcSeconds(ms: number): Time {
  return Math.floor(ms / 1000) as Time;
}

function deduplicateByTime(points: AreaData[]): AreaData[] {
  const byTime = new Map<number, AreaData>();
  for (const p of points) {
    byTime.set(p.time as number, p);
  }
  return [...byTime.values()].sort((a, b) => (a.time as number) - (b.time as number));
}

function formatPct(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '?';
  return `${(value * 100).toFixed(0)}%`;
}
