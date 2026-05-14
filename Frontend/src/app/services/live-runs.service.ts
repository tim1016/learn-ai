import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type { LiveRunStatus, LiveRunSummary, LogLine } from '../api/live-runs.types';

@Injectable({ providedIn: 'root' })
export class LiveRunsService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/live-runs';

  listRuns(params?: {
    limit?: number;
    status?: string;
    from_ms?: number;
    to_ms?: number;
  }): Promise<LiveRunSummary[]> {
    return firstValueFrom(
      this.http.get<LiveRunSummary[]>(this.base, { params: params as Record<string, unknown> ?? {} }),
    );
  }

  getStatus(runId: string): Promise<LiveRunStatus> {
    return firstValueFrom(this.http.get<LiveRunStatus>(`${this.base}/${encodeURIComponent(runId)}/status`));
  }

  getLogTail(runId: string, lines = 200): Promise<LogLine[]> {
    return firstValueFrom(
      this.http.get<LogLine[]>(`${this.base}/${encodeURIComponent(runId)}/log-tail`, {
        params: { lines },
      }),
    );
  }
}
