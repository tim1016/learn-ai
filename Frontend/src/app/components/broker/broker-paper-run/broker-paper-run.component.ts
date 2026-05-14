import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';
import { JsonPipe } from '@angular/common';
import { rxResource } from '@angular/core/rxjs-interop';
import { from, of, switchMap, timer } from 'rxjs';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { LiveRunsService } from '../../../services/live-runs.service';
import type { LiveRunSummary, LogLine, RunState } from '../../../api/live-runs.types';
import { fmtTimestampNy, fmtInteger } from '../format';

type FilterChip = 'today' | 'last14' | 'halted' | 'complete' | 'all';

const ACTIVE_STATES: ReadonlySet<RunState> = new Set([
  'running',
  'warming_up',
  'waiting_for_bars',
  'stale',
]);

function startOfTodayNyMs(): number {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const y = Number(parts.find((p) => p.type === 'year')?.value ?? '0');
  const mo = Number(parts.find((p) => p.type === 'month')?.value ?? '1') - 1;
  const d = Number(parts.find((p) => p.type === 'day')?.value ?? '1');
  const roughUtc = Date.UTC(y, mo, d);
  const checkStr = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(roughUtc));
  const [hh, mm] = checkStr.split(':').map(Number);
  return roughUtc - hh * 3_600_000 - mm * 60_000;
}

const FOURTEEN_DAYS_MS = 14 * 24 * 60 * 60 * 1000;
const WARMUP_BARS_REQUIRED = 15;

@Component({
  selector: 'app-broker-paper-run',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeaderComponent, SectionErrorComponent, JsonPipe],
  templateUrl: './broker-paper-run.component.html',
  styleUrl: './broker-paper-run.component.scss',
})
export class BrokerPaperRunComponent {
  private readonly svc = inject(LiveRunsService);

  // ── Run picker state ──────────────────────────────────────────────────
  readonly activeFilter = signal<FilterChip>('today');
  readonly selectedRunId = signal<string | null>(null);

  readonly runList = resource({
    loader: () => this.svc.listRuns({ limit: 50 }),
  });

  readonly filteredRuns = computed<LiveRunSummary[]>(() => {
    const runs = this.runList.value() ?? [];
    const filter = this.activeFilter();
    const now = Date.now();
    switch (filter) {
      case 'today':
        return runs.filter((r) => r.created_at_ms >= startOfTodayNyMs());
      case 'last14':
        return runs.filter((r) => r.created_at_ms >= now - FOURTEEN_DAYS_MS);
      case 'halted':
        return runs.filter((r) => r.state === 'halted');
      case 'complete':
        return runs.filter((r) => r.state === 'complete');
      default:
        return runs;
    }
  });

  // ── Status polling ────────────────────────────────────────────────────
  readonly status = resource({
    request: () => this.selectedRunId(),
    loader: ({ request: runId }) => {
      if (!runId) return Promise.resolve(null);
      return this.svc.getStatus(runId);
    },
  });

  private readonly pollIntervalMs = computed<number>(() => {
    const state = this.status.value()?.state;
    return state === 'idle' || state === 'complete' ? 10_000 : 5_000;
  });

  // ── Log-tail polling ──────────────────────────────────────────────────
  readonly logTail = rxResource({
    request: () => this.selectedRunId(),
    loader: ({ request: runId }) => {
      if (!runId) return of<LogLine[]>([]);
      return timer(0, 10_000).pipe(
        switchMap(() => from(this.svc.getLogTail(runId, 200))),
      );
    },
  });

  // ── Computed display helpers ──────────────────────────────────────────
  readonly topStripDynamicClasses = computed<string>(() => {
    const parts: string[] = [];
    const state = this.status.value()?.state;
    if (state) parts.push(`state-${state}`);
    if (this.actionRequired()) parts.push('action-required');
    return parts.join(' ');
  });

  readonly lastBarAgeLabel = computed<string>(() => {
    const s = this.status.value();
    if (!s || s.last_bar_age_s == null) return '—';
    const secs = Math.round(s.last_bar_age_s);
    if (secs < 60) return `${secs} s ago`;
    return `${Math.round(secs / 60)} m ago`;
  });

  readonly lastBarAgeStale = computed<boolean>(() => {
    const s = this.status.value();
    return s != null && s.last_bar_age_s != null && s.last_bar_age_s > 90;
  });

  readonly actionRequired = computed<boolean>(() => {
    const s = this.status.value();
    if (!s) return false;
    return s.state === 'halted' || s.state === 'poisoned' || s.state === 'stopped';
  });

  readonly warmupProgress = computed<string>(() => {
    const s = this.status.value();
    if (!s) return '—';
    return `${s.decisions.row_count} / ${WARMUP_BARS_REQUIRED}`;
  });

  readonly runIdShort = computed<string>(() => {
    const id = this.selectedRunId();
    return id ? id.slice(0, 8) : '—';
  });

  // ── Formatters (expose to template) ──────────────────────────────────
  readonly fmtTimestampNy = fmtTimestampNy;
  readonly fmtInteger = fmtInteger;

  // ── Filter chips ──────────────────────────────────────────────────────
  readonly filterChips: { key: FilterChip; label: string }[] = [
    { key: 'today', label: 'Today' },
    { key: 'last14', label: 'Last 14 days' },
    { key: 'halted', label: 'Halted' },
    { key: 'complete', label: 'Completed' },
    { key: 'all', label: 'All' },
  ];

  constructor() {
    // Auto-select the first active run when the list loads
    effect(() => {
      const runs = this.runList.value();
      if (runs == null || runs.length === 0 || this.selectedRunId() !== null) return;
      const active = runs.find((r) => ACTIVE_STATES.has(r.state));
      this.selectedRunId.set((active ?? runs[0]).run_id);
    });

    // Adaptive polling: only runs when a run is selected; onCleanup handles re-run and destroy
    effect((onCleanup) => {
      if (!this.selectedRunId()) return;
      const interval = this.pollIntervalMs();
      const id = setInterval(() => this.status.reload(), interval);
      onCleanup(() => clearInterval(id));
    });
  }

  selectRun(runId: string): void {
    this.selectedRunId.set(runId);
  }

  setFilter(chip: FilterChip): void {
    this.activeFilter.set(chip);
  }

  copyRunId(): void {
    const id = this.selectedRunId();
    if (id) void navigator.clipboard.writeText(id);
  }

  reloadRunList(): void {
    this.runList.reload();
  }

  fmtBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }


  getField(obj: Record<string, unknown> | null, key: string): unknown {
    return obj != null ? (obj[key] ?? null) : null;
  }

  formatField(obj: Record<string, unknown> | null, key: string): string {
    const val = this.getField(obj, key);
    return val != null ? String(val) : '—';
  }

  getFillTs(fill: Record<string, unknown>): number | null {
    const v = fill['ts_ms'];
    return typeof v === 'number' ? v : null;
  }

  getSelectValue(event: Event): string {
    return (event.target as HTMLSelectElement).value;
  }
}
