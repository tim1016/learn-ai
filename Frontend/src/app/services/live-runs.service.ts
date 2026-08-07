import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  AuditCopySizingLookup,
  BotEventPage,
  CommandsSummary,
  CommandWriteRequest,
  CommandWriteResponse,
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
  SizingPolicy,
  SpecStrategyFixture,
} from '../api/live-runs.types';
import type { DaemonDiagnosticReport } from '../api/daemon-diagnostics.types';
import type {
  FleetAccountSummary,
  FleetContamination,
  LiveInstanceSummary,
  SetInstanceDesiredStateResponse,
} from '../api/live-instances.types';
import type { DeployPreflightResponse } from '../api/operator-blocker.types';

@Injectable({ providedIn: 'root' })
export class LiveRunsService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/live-runs';
  private readonly instancesBase = '/api/live-instances';

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

  getBotEvents(
    runId: string,
    params?: { after_seq?: number; cursor?: string; limit?: number },
  ): Promise<BotEventPage> {
    let query = new HttpParams();
    if (params?.after_seq !== undefined) query = query.set('after_seq', String(params.after_seq));
    if (params?.cursor !== undefined) query = query.set('cursor', params.cursor);
    if (params?.limit !== undefined) query = query.set('limit', String(params.limit));
    return firstValueFrom(
      this.http.get<BotEventPage>(
        `${this.base}/${encodeURIComponent(runId)}/bot-events`,
        { params: query },
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

  // Routed through the data plane, not the daemon directly: PRD #619-C P2
  // made /health auth-gated alongside every other daemon route (host_daemon.py
  // docstring; ADR 0007 — "the browser must never hold that shared secret").
  // The data plane attaches X-Live-Runner-Token from the artifacts bind mount
  // and forwards the result.
  getHostRunnerHealth(): Promise<HostRunnerHealth> {
    return firstValueFrom(
      this.http.get<HostRunnerHealth>(`${this.instancesBase}/daemon-health`),
    );
  }

  renewControlPlaneLease(): Promise<HostRunnerHealth> {
    return firstValueFrom(
      this.http.post<HostRunnerHealth>(`${this.instancesBase}/daemon-health/renew-lease`, {}),
    );
  }

  getDaemonDiagnostics(): Promise<DaemonDiagnosticReport> {
    return firstValueFrom(
      this.http.get<DaemonDiagnosticReport>(`${this.instancesBase}/daemon-diagnose`),
    );
  }

  getInstanceDaemonDiagnostics(instanceId: string): Promise<DaemonDiagnosticReport> {
    return firstValueFrom(
      this.http.get<DaemonDiagnosticReport>(
        `${this.instancesBase}/${encodeURIComponent(instanceId)}/daemon-diagnose`,
      ),
    );
  }

  // Start/Stop route through the data plane for the same reason — the daemon
  // enforces a mandatory X-Live-Runner-Token on every actuation route
  // (ADR 0007), and the browser must never hold that shared secret.
  startHostRunner(runId: string, request: HostRunnerStartRequest): Promise<HostRunnerActionResponse> {
    return firstValueFrom(
      this.http.post<HostRunnerActionResponse>(
        `${this.instancesBase}/runs/${encodeURIComponent(runId)}/start`,
        request,
      ),
    );
  }

  stopHostRunner(runId: string, request: HostRunnerStopRequest): Promise<SetInstanceDesiredStateResponse> {
    return firstValueFrom(
      this.http.post<SetInstanceDesiredStateResponse>(
        `${this.instancesBase}/runs/${encodeURIComponent(runId)}/stop`,
        request,
      ),
    );
  }

  /** Account fleet overview: every known strategy instance, live or not. */
  getInstances(): Promise<LiveInstanceSummary[]> {
    return firstValueFrom(this.http.get<LiveInstanceSummary[]>(this.instancesBase));
  }

  /** Account/fleet contamination: net vs Σ instance expecteds (ADR 0005, #399). */
  getAccountFleet(): Promise<FleetContamination> {
    return firstValueFrom(this.http.get<FleetContamination>(`${this.instancesBase}/account`));
  }

  /**
   * PRD #616 — composed account-row DTO (account identity + position
   * contamination).  The new cockpit (PRD #617) reads this; the legacy
   * `/account` endpoint stays for back-compat callers.
   */
  getAccountSummary(accountId?: string): Promise<FleetAccountSummary> {
    const params = accountId === undefined ? undefined : new HttpParams().set('account_id', accountId);
    return firstValueFrom(
      this.http.get<FleetAccountSummary>(`${this.instancesBase}/account-summary`, { params }),
    );
  }

  /** Deploy (create a run): data plane forwards to the daemon (ADR 0006, #415).
   * 201 created / 200 idempotent no-op; precondition failures map to 4xx/5xx. */
  deployInstance(request: HostRunnerDeployRequest): Promise<HostRunnerDeployResponse> {
    return firstValueFrom(
      this.http.post<HostRunnerDeployResponse>(this.instancesBase, request),
    );
  }

  deployPreflight(params: {
    strategyKey: string;
    accountId: string;
    instanceId: string;
  }): Promise<DeployPreflightResponse> {
    const query = new HttpParams()
      .set('strategy_key', params.strategyKey)
      .set('account_id', params.accountId)
      .set('instance_id', params.instanceId);
    return firstValueFrom(
      this.http.get<DeployPreflightResponse>(`${this.instancesBase}/deploy-preflight`, {
        params: query,
      }),
    );
  }

  /** Committed QC audit copies for the deploy picker (ADR 0006, #413). */
  getQcAuditCopies(): Promise<QcAuditCopyListing> {
    return firstValueFrom(
      this.http.get<QcAuditCopyListing>(`${this.instancesBase}/qc-audit-copies`),
    );
  }

  /** ADR 0009 § 3 — Reference parity gate verdict for an audit copy. The
   * optional `proposedSizing` lets the deploy form check a specific policy;
   * omit it on initial render to learn the registered rule. */
  getAuditCopySizingLookup(
    auditCopyPath: string,
    proposedSizing?: SizingPolicy,
  ): Promise<AuditCopySizingLookup> {
    let params = new HttpParams().set('audit_copy_path', auditCopyPath);
    if (proposedSizing) {
      params = params.set('proposed_sizing', JSON.stringify(proposedSizing));
    }
    return firstValueFrom(
      this.http.get<AuditCopySizingLookup>(
        `${this.instancesBase}/audit-copy-sizing-lookup`,
        { params },
      ),
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
