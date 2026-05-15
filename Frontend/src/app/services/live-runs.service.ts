import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  HostRunnerActionResponse,
  HostRunnerHealth,
  HostRunnerStartRequest,
  HostRunnerStopRequest,
  LiveRunStatus,
  LiveRunSummary,
  LogLine,
} from '../api/live-runs.types';

@Injectable({ providedIn: 'root' })
export class LiveRunsService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/live-runs';
  private readonly daemonBase = environment.liveRunnerDaemonUrl;

  listRuns(params?: {
    limit?: number;
    status?: string;
    from_ms?: number;
    to_ms?: number;
  }): Promise<LiveRunSummary[]> {
    return firstValueFrom(
      this.http.get<LiveRunSummary[]>(this.base, { params: params as Record<string, string | number | boolean> ?? {} }),
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

  getHostRunnerHealth(): Promise<HostRunnerHealth> {
    return firstValueFrom(this.http.get<HostRunnerHealth>(`${this.daemonBase}/health`));
  }

  startHostRunner(runId: string, request: HostRunnerStartRequest): Promise<HostRunnerActionResponse> {
    return firstValueFrom(
      this.http.post<HostRunnerActionResponse>(`${this.daemonBase}/runs/${encodeURIComponent(runId)}/start`, request),
    );
  }

  stopHostRunner(runId: string, request: HostRunnerStopRequest): Promise<HostRunnerActionResponse> {
    return firstValueFrom(
      this.http.post<HostRunnerActionResponse>(`${this.daemonBase}/runs/${encodeURIComponent(runId)}/stop`, request),
    );
  }
}
