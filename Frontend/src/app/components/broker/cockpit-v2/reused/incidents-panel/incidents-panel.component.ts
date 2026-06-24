import { HttpClient } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import {
  INCIDENT_COPY,
  type IncidentCopy,
  type IncidentSourceLabel,
  composeIncidentMessage,
  getIncidentCopy,
  getIncidentSourceLabel,
} from './incidents-copy';
import {
  INCIDENT_SOURCES,
  type IncidentCategory,
  type IncidentRow,
  type IncidentSource,
} from './incidents.types';

const POLL_INTERVAL_MS = 5_000;

type SourceFilter = 'all' | IncidentSource;

/** Source-filter chip options shown above the table. "All" first, then
 * broker → app → infra → operator → unknown to mirror the side-of-the-
 * world ordering an operator scans by. */
const SOURCE_FILTER_OPTIONS: readonly SourceFilter[] = ['all', ...INCIDENT_SOURCES] as const;

interface DisplayRow extends IncidentRow {
  copy: IncidentCopy;
  /** Template-expanded message (hybrid-C dynamic_facts substituted). */
  composedMessage: string;
  /** Badge + tone for the source dimension. */
  sourceLabel: IncidentSourceLabel;
}

interface SourceFilterChip {
  key: SourceFilter;
  label: string;
  count: number;
}

/**
 * Recent Incidents panel — operator-language WARNING / ERROR / CRITICAL
 * events from live.log, classified by the backend `incident_category`
 * enum and rendered via the frontend's `INCIDENT_COPY` map.
 *
 * Per #565 PR 6 this replaces the prior `bot-failures-table` (which
 * rendered raw log headers + `ib_async.wrapper Error 1100` lines). The
 * panel itself contains no run-log modal — it emits `rawLogRequested`
 * for the parent, which already owns the modal lifecycle.
 *
 * Severity tone drives the row tint. A null / unknown `incident_category`
 * from the backend falls back to UNKNOWN copy for rollout safety.
 */
@Component({
  selector: 'app-incidents-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './incidents-panel.component.html',
  styleUrl: './incidents-panel.component.scss',
})
export class IncidentsPanelComponent {
  readonly runId = input<string | null>(null);
  readonly rawLogRequested = output();

  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly expanded = signal<Set<number>>(new Set());
  /** Per-session source filter (codex D7: no localStorage). Resets to
   * `'all'` when the component is destroyed / the route changes. */
  protected readonly sourceFilter = signal<SourceFilter>('all');

  protected readonly incidentsResource = resource<IncidentRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadIncidents(params),
  });

  /** All rows after copy resolution + template expansion + source-label
   * resolution. Filtering happens downstream so the chip counts can read
   * the unfiltered total. */
  protected readonly allRows = computed<DisplayRow[]>(() =>
    (this.incidentsResource.value() ?? []).map((r) => {
      const copy = getIncidentCopy(r.incident_category);
      return {
        ...r,
        copy,
        composedMessage: composeIncidentMessage(copy.message, r.dynamic_facts),
        sourceLabel: getIncidentSourceLabel(r.incident_source),
      };
    }),
  );

  /** Rows after applying the source filter chip. */
  protected readonly rows = computed<DisplayRow[]>(() => {
    const filter = this.sourceFilter();
    if (filter === 'all') return this.allRows();
    return this.allRows().filter((r) => r.sourceLabel.tone === filter);
  });

  /** Chips shown above the table. Counts read from `allRows` so they
   * stay stable as the user clicks through filters. */
  protected readonly sourceChips = computed<SourceFilterChip[]>(() => {
    const all = this.allRows();
    const counts = new Map<SourceFilter, number>();
    counts.set('all', all.length);
    for (const r of all) {
      const key = r.sourceLabel.tone as SourceFilter;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return SOURCE_FILTER_OPTIONS.filter(
      (key) => key === 'all' || (counts.get(key) ?? 0) > 0,
    ).map((key) => ({
      key,
      label: key === 'all' ? 'All' : getIncidentSourceLabel(key).longName,
      count: counts.get(key) ?? 0,
    }));
  });

  protected readonly hasData = computed<boolean>(() => this.allRows().length > 0);

  /** Map an IncidentCategory to a known key for the INCIDENT_COPY map.
   * Defensive against backend emitting unknown categories. */
  protected categoryKey(c: IncidentCategory | null | undefined): IncidentCategory {
    if (c === null || c === undefined) return 'unknown';
    return c in INCIDENT_COPY ? c : 'unknown';
  }

  constructor() {
    const timer = setInterval(() => this.incidentsResource.reload(), POLL_INTERVAL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  private async loadIncidents(runId: string | null): Promise<IncidentRow[]> {
    if (!runId) return [];
    return firstValueFrom(
      this.http.get<IncidentRow[]>(
        `/api/live-runs/${encodeURIComponent(runId)}/incidents`,
      ),
    );
  }

  protected toggle(i: number): void {
    this.expanded.update((s) => {
      const next = new Set(s);
      if (next.has(i)) {
        next.delete(i);
      } else {
        next.add(i);
      }
      return next;
    });
  }

  protected isExpanded(i: number): boolean {
    return this.expanded().has(i);
  }

  protected openRawLog(): void {
    this.rawLogRequested.emit();
  }

  protected setSourceFilter(filter: SourceFilter): void {
    this.sourceFilter.set(filter);
    // Collapse any expanded rows that the new filter hides — keeps the
    // expand state from "leaking" into a future filter switch.
    this.expanded.set(new Set());
  }

  /** Track key includes the message prefix so consecutive identical
   * (ts_ms, logger) headers from a re-emitted log are distinguished. */
  protected trackRow(_i: number, r: DisplayRow): string {
    return `${r.ts_ms}:${r.logger}:${r.message.slice(0, 40)}`;
  }
}
