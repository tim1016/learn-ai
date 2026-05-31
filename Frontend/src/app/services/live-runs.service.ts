import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  CommandsSummary,
  CommandWriteRequest,
  CommandWriteResponse,
  DesiredStateWriteRequest,
  DesiredStateWriteResponse,
  EngineStrategyInfo,
  HostRunnerActionResponse,
  HostRunnerDeployRequest,
  HostRunnerDeployResponse,
  HostRunnerHealth,
  HostRunnerStartRequest,
  HostRunnerStopRequest,
  LiveRunStatus,
  LiveRunSummary,
  LogLine,
  QcAuditCopyListing,
  SpecStrategyFixture,
} from '../api/live-runs.types';
import type {
  FleetContamination,
  InstanceDesiredStateRequest,
  LiveInstanceStatus,
  LiveInstanceSummary,
  SetInstanceDesiredStateResponse,
} from '../api/live-instances.types';

@Injectable({ providedIn: 'root' })
export class LiveRunsService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/live-runs';
  private readonly instancesBase = '/api/live-instances';
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

  /**
   * UI-3 — write durable operator intent (pause/resume/stop) to the
   * desired-state sidecar. Backed by the sibling backend PR
   * `prd-a/ui-1-status-and-controls-api`. The write is addressed by
   * `run_id`; the backend resolves the run's `strategy_instance_id` and
   * writes `artifacts/live_state/<strategy_instance_id>/desired_state.json`.
   */
  writeDesiredState(
    runId: string,
    request: DesiredStateWriteRequest,
  ): Promise<DesiredStateWriteResponse> {
    return firstValueFrom(
      this.http.post<DesiredStateWriteResponse>(
        `${this.base}/${encodeURIComponent(runId)}/desired-state`,
        request,
      ),
    );
  }

  /** UI-4 — read the per-run command pending/ack timeline. */
  getCommands(runId: string): Promise<CommandsSummary> {
    return firstValueFrom(
      this.http.get<CommandsSummary>(`${this.base}/${encodeURIComponent(runId)}/commands`),
    );
  }

  /**
   * UI-4 — write a per-run command-channel verb
   * (PAUSE/RESUME/STOP/FLATTEN/MARK_POISONED/RECONCILE). The backend
   * writes `commands/command.<seq>.<verb>.pending.json` atomically; the
   * bot acks asynchronously, surfaced via `getCommands`.
   */
  writeCommand(runId: string, request: CommandWriteRequest): Promise<CommandWriteResponse> {
    return firstValueFrom(
      this.http.post<CommandWriteResponse>(
        `${this.base}/${encodeURIComponent(runId)}/commands`,
        request,
      ),
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

  // --- Instance-addressed operator console (ADR 0004) ---

  /** Account fleet overview: every known strategy instance, live or not. */
  getInstances(): Promise<LiveInstanceSummary[]> {
    return firstValueFrom(this.http.get<LiveInstanceSummary[]>(this.instancesBase));
  }

  /** Instance control-room status: live binding (registry) + evidence + intent. */
  getInstanceStatus(instanceId: string): Promise<LiveInstanceStatus> {
    return firstValueFrom(
      this.http.get<LiveInstanceStatus>(`${this.instancesBase}/${encodeURIComponent(instanceId)}/status`),
    );
  }

  /**
   * The single operator intent knob (ADR 0004): writes durable desired-state
   * and, if a live binding exists, actuates it on the bound run. PAUSED/RUNNING/
   * STOPPED is liveness-independent — live actuation or gates the next start.
   */
  setInstanceDesiredState(
    instanceId: string,
    request: InstanceDesiredStateRequest,
  ): Promise<SetInstanceDesiredStateResponse> {
    return firstValueFrom(
      this.http.post<SetInstanceDesiredStateResponse>(
        `${this.instancesBase}/${encodeURIComponent(instanceId)}/desired-state`,
        request,
      ),
    );
  }

  /** Unified one-shot command timeline for an instance's bound run (#397). */
  getInstanceCommands(instanceId: string): Promise<CommandsSummary> {
    return firstValueFrom(
      this.http.get<CommandsSummary>(`${this.instancesBase}/${encodeURIComponent(instanceId)}/commands`),
    );
  }

  /** Issue a one-shot command (FLATTEN/RECONCILE/MARK_POISONED) to the bound run (#397). */
  issueInstanceCommand(instanceId: string, request: CommandWriteRequest): Promise<CommandWriteResponse> {
    return firstValueFrom(
      this.http.post<CommandWriteResponse>(
        `${this.instancesBase}/${encodeURIComponent(instanceId)}/commands`,
        request,
      ),
    );
  }

  /** Account/fleet contamination: net vs Σ instance expecteds (ADR 0005, #399). */
  getAccountFleet(): Promise<FleetContamination> {
    return firstValueFrom(this.http.get<FleetContamination>(`${this.instancesBase}/account`));
  }

  /** Deploy (create a run): data plane forwards to the daemon (ADR 0006, #415).
   * 201 created / 200 idempotent no-op; precondition failures map to 4xx/5xx. */
  deployInstance(request: HostRunnerDeployRequest): Promise<HostRunnerDeployResponse> {
    return firstValueFrom(
      this.http.post<HostRunnerDeployResponse>(this.instancesBase, request),
    );
  }

  /** Committed QC audit copies for the deploy picker (ADR 0006, #413). */
  getQcAuditCopies(): Promise<QcAuditCopyListing> {
    return firstValueFrom(
      this.http.get<QcAuditCopyListing>(`${this.instancesBase}/qc-audit-copies`),
    );
  }

  /** Registered engine strategies — the deploy form's algorithm dropdown. */
  getEngineStrategies(): Promise<EngineStrategyInfo[]> {
    return firstValueFrom(this.http.get<EngineStrategyInfo[]>('/api/engine/strategies'));
  }

  /** Canonical strategy spec fixtures, including repo-relative paths for deploy. */
  getSpecStrategyFixtures(): Promise<SpecStrategyFixture[]> {
    return firstValueFrom(this.http.get<SpecStrategyFixture[]>('/api/spec-strategy/fixtures'));
  }
}
