import { CommonModule, DatePipe, DecimalPipe, PercentPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MessageModule } from 'primeng/message';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import { BaselinesService } from '../../../../services/baselines.service';
import type {
  BaselineResponse,
  BaselineStatus,
  NullDistribution,
} from '../../../../services/baselines.types';

interface NullSummary {
  metric_name: string;
  parent_value: number | null;
  empirical_percentile: number | null;
  empirical_p_value: number | null;
  null_count: number;
  p5: number | null;
  p50: number | null;
  p95: number | null;
}

@Component({
  selector: 'app-baselines-detail-page',
  imports: [
    CommonModule,
    RouterLink,
    MessageModule,
    TableModule,
    TagModule,
    DatePipe,
    DecimalPipe,
    PercentPipe,
  ],
  templateUrl: './baselines-detail-page.component.html',
  styleUrls: ['./baselines-detail-page.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BaselinesDetailPageComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly service = inject(BaselinesService);
  private readonly destroyRef = inject(DestroyRef);

  readonly baseline = signal<BaselineResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly baselineId = signal<string | null>(null);

  readonly nullSummaries = computed<NullSummary[]>(() => {
    const data = this.baseline();
    if (!data) return [];
    return data.result.null_distributions.map(summarise);
  });

  readonly methodParamsEntries = computed<[string, unknown][]>(() => {
    const data = this.baseline();
    if (!data) return [];
    return Object.entries(data.config.method_params);
  });

  constructor() {
    this.route.paramMap
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((params) => {
        const id = params.get('baseline_id');
        this.baselineId.set(id);
        if (id) {
          void this.load(id);
        }
      });
  }

  async load(baselineId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const data = await this.service.getBaseline(baselineId);
      this.baseline.set(data);
    } catch (err) {
      this.error.set(this.formatError(err));
    } finally {
      this.loading.set(false);
    }
  }

  statusSeverity(status: BaselineStatus): 'success' | 'danger' {
    return status === 'completed' ? 'success' : 'danger';
  }

  shortHash(value: string | null | undefined, len = 16): string {
    if (!value) return '—';
    return value.slice(0, len);
  }

  parametersLabel(parameters: Record<string, unknown>): string {
    const entries = Object.entries(parameters);
    if (entries.length === 0) return '—';
    return entries.map(([k, v]) => `${k}=${formatValue(v)}`).join(', ');
  }

  private formatError(err: unknown): string {
    if (err instanceof Error) return err.message;
    if (typeof err === 'object' && err !== null && 'message' in err) {
      return String((err as { message: unknown }).message);
    }
    return 'Unknown error';
  }
}

function summarise(d: NullDistribution): NullSummary {
  const successful = d.null_values.filter((v) => Number.isFinite(v));
  const sorted = [...successful].sort((a, b) => a - b);
  return {
    metric_name: d.metric_name,
    parent_value: d.parent_value,
    empirical_percentile: d.empirical_percentile,
    empirical_p_value: d.empirical_p_value,
    null_count: successful.length,
    p5: percentile(sorted, 0.05),
    p50: percentile(sorted, 0.5),
    p95: percentile(sorted, 0.95),
  };
}

/**
 * Linear-interpolation percentile matching numpy.percentile's default
 * rule (`linear`). Returns ``null`` for an empty array. The detail
 * page only shows summaries computed locally for visualisation; the
 * authoritative `empirical_percentile` and `empirical_p_value` are
 * always the server's values.
 */
function percentile(sorted: number[], q: number): number | null {
  if (sorted.length === 0) return null;
  if (sorted.length === 1) return sorted[0];
  const idx = q * (sorted.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  const frac = idx - lo;
  return sorted[lo] + (sorted[hi] - sorted[lo]) * frac;
}

function formatValue(v: unknown): string {
  if (typeof v === 'number') return String(v);
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return `[${v.map(formatValue).join(', ')}]`;
  return JSON.stringify(v);
}
