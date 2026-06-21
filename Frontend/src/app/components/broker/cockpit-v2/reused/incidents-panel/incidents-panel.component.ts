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
import { INCIDENT_COPY, type IncidentCopy, getIncidentCopy } from './incidents-copy';
import type { IncidentCategory, IncidentRow } from './incidents.types';

const POLL_INTERVAL_MS = 5_000;

interface DisplayRow extends IncidentRow {
  copy: IncidentCopy;
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

  protected readonly incidentsResource = resource<IncidentRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadIncidents(params),
  });

  protected readonly rows = computed<DisplayRow[]>(() =>
    (this.incidentsResource.value() ?? []).map((r) => ({
      ...r,
      copy: getIncidentCopy(r.incident_category),
    })),
  );

  protected readonly hasData = computed<boolean>(() => this.rows().length > 0);

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

  /** Track key includes the message prefix so consecutive identical
   * (ts_ms, logger) headers from a re-emitted log are distinguished. */
  protected trackRow(_i: number, r: DisplayRow): string {
    return `${r.ts_ms}:${r.logger}:${r.message.slice(0, 40)}`;
  }
}
