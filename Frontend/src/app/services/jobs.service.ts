import { Injectable, computed, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export type JobStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type JobEventType =
  | 'job.started'
  | 'job.phase'
  | 'job.progress'
  | 'job.log'
  | 'job.completed'
  | 'job.failed'
  | 'job.cancelled';

export interface JobEvent {
  type: JobEventType;
  // Discriminated payload — kept loose; consumers narrow at the use site.
  [key: string]: unknown;
}

export interface JobState {
  id: string;
  type: string;
  status: JobStatus;
  phase?: string;
  current?: number;
  total?: number;
  unit?: string;
  message?: string;
  errorCode?: string;
  errorMessage?: string;
  resultUrl?: string;
  // Last few log lines so the drawer can show them inline.
  recentLogs: { level: string; message: string; ts: number }[];
  // Wall-clock at which the job started/finished, for elapsed display.
  startedAt?: number;
  finishedAt?: number;
}

interface ServerJobState {
  id: string;
  type: string;
  status: string;
  phase?: string;
  started_at?: string;
  completed_at?: string;
  error_code?: string;
  error_message?: string;
}

const TERMINAL: JobStatus[] = ['completed', 'failed', 'cancelled'];
const MAX_RECENT_LOGS = 5;

/**
 * Process-wide registry of in-flight and recently-finished jobs.
 *
 * Survives route changes (`providedIn: 'root'`). On startup, queries
 * `/jobs?active=true` to resume any jobs that were running when the
 * page reloaded — each gets an EventSource with `Last-Event-ID` so the
 * server replays missed events from the Redis stream.
 */
@Injectable({ providedIn: 'root' })
export class JobsService {
  private http = inject(HttpClient);

  private readonly _jobs = signal<Map<string, JobState>>(new Map());
  // Per-job EventSource handles, kept out of the signal to avoid
  // serializing them into change detection.
  private readonly sources = new Map<string, EventSource>();
  // Last seen event id per job — used when a connection drops and we
  // reopen with `Last-Event-ID`. The browser's native EventSource
  // automatically supplies the last seen id on reconnect, but if the
  // user closed the tab and reopens we need to bootstrap from the
  // server side, so we don't track this manually.

  readonly jobs = computed(() => Array.from(this._jobs().values()));

  readonly activeJobs = computed(() =>
    this.jobs().filter(j => !TERMINAL.includes(j.status)),
  );

  readonly hasActive = computed(() => this.activeJobs().length > 0);

  constructor() {
    void this.resumeActive();
  }

  job(id: string): JobState | undefined {
    return this._jobs().get(id);
  }

  /**
   * POST /jobs/{type} → returns the new job id and opens an SSE stream.
   */
  async startJob(type: string, payload: Record<string, unknown>): Promise<string> {
    const resp = await firstValueFrom(
      this.http.post<{ id: string; status: string }>(`/api/jobs/${type}`, payload),
    );
    this.upsert({
      id: resp.id,
      type,
      status: 'queued',
      recentLogs: [],
    });
    this.openStream(resp.id);
    return resp.id;
  }

  /** DELETE /jobs/{id}. The worker checks the cancel flag cooperatively. */
  async cancelJob(id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`/api/jobs/${id}`));
  }

  /** Fetch the full result of a completed job. */
  async fetchResult<T = unknown>(id: string): Promise<T> {
    return firstValueFrom(this.http.get<T>(`/api/jobs/${id}/result`));
  }

  /** Drop a job from the local registry (e.g., user dismissed it from the drawer). */
  dismiss(id: string): void {
    this.closeStream(id);
    this._jobs.update(m => {
      const next = new Map(m);
      next.delete(id);
      return next;
    });
  }

  // ---------------------------------------------------------------------
  // Internals
  // ---------------------------------------------------------------------

  private async resumeActive(): Promise<void> {
    try {
      const list = await firstValueFrom(
        this.http.get<ServerJobState[]>('/api/jobs', { params: { active: 'true' } }),
      );
      for (const s of list) {
        const status = (s.status as JobStatus) ?? 'queued';
        this.upsert({
          id: s.id,
          type: s.type,
          status,
          phase: s.phase,
          startedAt: s.started_at ? Number(s.started_at) : undefined,
          recentLogs: [],
        });
        if (!TERMINAL.includes(status)) {
          this.openStream(s.id);
        }
      }
    } catch {
      // Backend might not be up yet; the page still works without
      // resumption — newly-started jobs will register normally.
    }
  }

  private openStream(id: string): void {
    if (this.sources.has(id)) return;
    const source = new EventSource(`/api/jobs/${id}/events`);
    this.sources.set(id, source);

    source.onmessage = ev => this.applyEvent(id, ev.data);
    source.onerror = () => {
      // EventSource auto-reconnects unless we close it. If the job
      // already reached terminal state, close to free the connection.
      const job = this._jobs().get(id);
      if (job && TERMINAL.includes(job.status)) {
        this.closeStream(id);
      }
    };
  }

  private closeStream(id: string): void {
    const source = this.sources.get(id);
    if (source) {
      source.close();
      this.sources.delete(id);
    }
  }

  private applyEvent(id: string, raw: string): void {
    let evt: JobEvent;
    try {
      evt = JSON.parse(raw) as JobEvent;
    } catch {
      return;
    }
    this._jobs.update(m => {
      const next = new Map(m);
      const prev = next.get(id);
      if (!prev) return m;
      const updated = applyJobEvent(prev, evt);
      next.set(id, updated);
      return next;
    });

    if (evt.type === 'job.completed' || evt.type === 'job.failed' || evt.type === 'job.cancelled') {
      // The server side closes the stream after a terminal event; close
      // our handle so the browser doesn't try to reconnect.
      setTimeout(() => this.closeStream(id), 0);
    }
  }

  private upsert(job: JobState): void {
    this._jobs.update(m => {
      const next = new Map(m);
      const existing = next.get(job.id);
      next.set(job.id, existing ? { ...existing, ...job } : job);
      return next;
    });
  }
}

// Pure reducer — exported for unit testing.
export function applyJobEvent(prev: JobState, evt: JobEvent): JobState {
  switch (evt.type) {
    case 'job.started':
      return { ...prev, status: 'running', startedAt: Date.now() };
    case 'job.phase':
      return { ...prev, phase: evt['phase'] as string };
    case 'job.progress':
      return {
        ...prev,
        current: evt['current'] as number,
        total: evt['total'] as number,
        unit: (evt['unit'] as string) ?? prev.unit,
        message: (evt['message'] as string) ?? prev.message,
      };
    case 'job.log': {
      const log = {
        level: (evt['level'] as string) ?? 'info',
        message: (evt['message'] as string) ?? '',
        ts: Date.now(),
      };
      const recent = [...prev.recentLogs, log].slice(-MAX_RECENT_LOGS);
      return { ...prev, recentLogs: recent };
    }
    case 'job.completed':
      return {
        ...prev,
        status: 'completed',
        finishedAt: Date.now(),
        resultUrl: evt['result_url'] as string,
      };
    case 'job.failed':
      return {
        ...prev,
        status: 'failed',
        finishedAt: Date.now(),
        errorCode: evt['code'] as string,
        errorMessage: evt['message'] as string,
      };
    case 'job.cancelled':
      return {
        ...prev,
        status: 'cancelled',
        finishedAt: Date.now(),
        message: evt['reason'] as string,
      };
    default:
      return prev;
  }
}
