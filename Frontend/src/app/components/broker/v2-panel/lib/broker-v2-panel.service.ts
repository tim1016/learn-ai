import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type { components } from '../../../../api/broker.types';
import type {
  SqliteRecoveryAction,
  SqliteRecoveryActionCheck,
} from '../../../../api/alpaca.types';
import type {
  BotCatalogView,
  BotPanelView,
  BotPanelLiveSnapshot,
  BotRunHistoryPage,
  BotRunView,
  ChartHistoryPreset,
  ChartHistoryResponse,
  ChartLiveResolution,
  ChartLiveResponse,
  EvidencePage,
  PanelAction,
  PanelActionRequest,
  PanelActionResult,
  PanelProfile,
} from './broker-v2-panel.types';

export type DeployBotBody = components['schemas']['AlpacaPaperDeployRequest'];
export type DeployBotReceipt = components['schemas']['AlpacaPaperDeployReceipt'];
export type DeployBotView = components['schemas']['AlpacaPaperDeployView'];
export type DeployBotStrategy = components['schemas']['AlpacaPaperDeployStrategy'];
export type DeployReadinessCheck = components['schemas']['AlpacaPaperDeployReadinessCheck'];
export type DeployExecutionMode = components['schemas']['AlpacaPaperExecutionMode'];
export type DeploySizingOption = components['schemas']['AlpacaPaperSizingOption'];
export type RunAdmissionDecision = components['schemas']['RunAdmissionDecision'];

/**
 * HTTP client for the broker-v2 panel surface.
 *
 * Targets `/api/brokers/{broker}/accounts/{accountId}/...` (the account-scoped
 * endpoints from S1). The base URL is parameterised so S4 operator lens reuses
 * this service without change.
 */
@Injectable({ providedIn: 'root' })
export class BrokerV2PanelService {
  private readonly http = inject(HttpClient);

  private base(broker: string, accountId: string): string {
    return `/api/brokers/${encodeURIComponent(broker)}/accounts/${encodeURIComponent(accountId)}`;
  }

  getPanelProfile(broker: string): Promise<PanelProfile> {
    return firstValueFrom(
      this.http.get<PanelProfile>(`/api/brokers/${encodeURIComponent(broker)}/panel-profile`),
    );
  }

  async checkSqliteSafeFlattenPlan(
    accountId: string,
    sid: string,
    action: Pick<PanelAction, 'action_id' | 'concurrency_token'>,
  ): Promise<SqliteRecoveryAction> {
    const response = await firstValueFrom(
      this.http.post<SqliteRecoveryActionCheck>(
        `/api/alpaca-clerk-sqlite/accounts/${encodeURIComponent(accountId)}/bots/${encodeURIComponent(sid)}/recovery-actions/check`,
        {
          action_id: action.action_id,
          concurrency_token: action.concurrency_token,
        },
      ),
    );
    return response.capability;
  }

  deployBot(
    broker: string,
    accountId: string,
    body: DeployBotBody,
  ): Promise<DeployBotReceipt> {
    return firstValueFrom(
      this.http.post<DeployBotReceipt>(
        `${this.base(broker, accountId)}/bots`,
        body,
      ),
    );
  }

  previewStartAdmission(
    broker: string,
    accountId: string,
    body: DeployBotBody,
  ): Promise<RunAdmissionDecision> {
    return firstValueFrom(
      this.http.post<RunAdmissionDecision>(
        `${this.base(broker, accountId)}/bots/admission`,
        body,
      ),
    );
  }

  getDeployView(
    broker: string,
    accountId: string,
  ): Promise<DeployBotView> {
    return firstValueFrom(
      this.http.get<DeployBotView>(
        `${this.base(broker, accountId)}/bots/deploy`,
      ),
    );
  }

  getCatalog(broker: string, accountId: string): Promise<BotCatalogView[]> {
    return firstValueFrom(
      this.http.get<BotCatalogView[]>(`${this.base(broker, accountId)}/bots/catalog`),
    );
  }

  getPanel(
    broker: string,
    accountId: string,
    sid: string,
    transactionRef?: string,
  ): Promise<BotPanelView> {
    let params = new HttpParams();
    if (transactionRef) {
      params = params.set('transaction_ref', transactionRef);
    }
    return firstValueFrom(
      this.http.get<BotPanelView>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/panel`,
        { params },
      ),
    );
  }

  getCurrentRun(broker: string, sid: string): Promise<BotRunView> {
    return firstValueFrom(
      this.http.get<BotRunView>(
        `/api/brokers/${encodeURIComponent(broker)}/bots/${encodeURIComponent(sid)}/runs/current`,
      ),
    );
  }

  getRunHistory(
    broker: string,
    sid: string,
    cursor?: string,
  ): Promise<BotRunHistoryPage> {
    let params = new HttpParams().set('limit', '1');
    if (cursor) params = params.set('cursor', cursor);
    return firstValueFrom(
      this.http.get<BotRunHistoryPage>(
        `/api/brokers/${encodeURIComponent(broker)}/bots/${encodeURIComponent(sid)}/runs/history`,
        { params },
      ),
    );
  }

  runAction(
    broker: string,
    accountId: string,
    sid: string,
    request: PanelActionRequest,
  ): Promise<PanelActionResult> {
    return firstValueFrom(
      this.http.post<PanelActionResult>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/actions`,
        request,
      ),
    );
  }

  /**
   * Run a presented panel action, with a bounded fresh-token retry on a 409.
   *
   * Every 409 from the action endpoint is a PRE-EXECUTION rejection
   * (`StaleRevisionError` / `ActionNotAvailableError` — the mid-flight
   * `ActionOutcomeUnknownError` is a 500), so the action never ran and a retry
   * cannot double-fire. On a 409 we refetch the authoritative panel and retry
   * ONCE with the current token — but only if: the action requires no operator
   * confirmation, is still offered, is still enabled, and its token changed;
   * otherwise the original error is re-thrown so the operator sees an honest
   * message instead of a silent re-post.
   *
   * The confirmation guard is load-bearing, not incidental: `flatten_stop`'s
   * token is derived from live exposure/working-order state
   * (`action_policy.py`'s `revision_inputs`), and its confirmation text quotes
   * those exact numbers back to the operator. Silently resubmitting a
   * refreshed action after that text was shown would let the operator
   * unknowingly flatten a materially different position than the one they
   * confirmed. Confirmed actions therefore always re-throw on a 409 — the
   * operator sees the state changed and must re-confirm explicitly.
   *
   * Stop's token, by contrast, is a pure function of `running`
   * (`action_policy.py:362`) — a single boolean. `enabled` already IS
   * `running`, so a presented "Stop, enabled" action can only ever recompute
   * to the SAME token. There is no reachable state where Stop is both
   * re-offered enabled AND carries a different token, so this retry can never
   * fire for Stop; it is eligible only because it carries no confirmation, not
   * because it benefits. Defect #10's fix for the 2026-07-30 Stop-409 storm is
   * the backend's `panel_action_rejected` log line naming the 409 subclass
   * (`StaleRevisionError` vs `ActionNotAvailableError`), not this retry —
   * making Stop itself recoverable needs a real design change (e.g. treating
   * "refetch shows Stop disabled because already-stopped" as an idempotent
   * success), tracked separately.
   *
   * This is the single public action entry point, so every caller (the panel
   * shell AND the fleet list) shares the same policy and there is no bare,
   * dead-ending variant to reach by accident.
   */
  async runBotAction(
    broker: string,
    accountId: string,
    sid: string,
    action: PanelAction,
    reason: string | null = null,
  ): Promise<PanelActionResult> {
    try {
      return await this.submitAction(broker, accountId, sid, action, reason);
    } catch (error) {
      if (
        !(error instanceof HttpErrorResponse) ||
        error.status !== 409 ||
        action.confirmation !== null
      ) {
        throw error;
      }
      const panel = await this.getPanel(broker, accountId, sid);
      const fresh = panel.actions.find(
        (candidate) => candidate.action_id === action.action_id,
      );
      if (
        fresh === undefined ||
        !fresh.enabled ||
        fresh.concurrency_token === action.concurrency_token
      ) {
        throw error;
      }
      // Same operator-confirmed action, resubmitted against a refreshed
      // token — not a new action, so the same reason still applies.
      return await this.submitAction(broker, accountId, sid, fresh, reason);
    }
  }

  private submitAction(
    broker: string,
    accountId: string,
    sid: string,
    action: PanelAction,
    reason: string | null,
  ): Promise<PanelActionResult> {
    const request: PanelActionRequest = {
      action_id: action.action_id,
      revision: action.revision,
      concurrency_token: action.concurrency_token,
      idempotency_key: crypto.randomUUID(),
      reason,
    };
    return this.runAction(broker, accountId, sid, request);
  }

  getLiveChart(
    broker: string,
    accountId: string,
    sid: string,
    resolution: ChartLiveResolution,
  ): Promise<ChartLiveResponse> {
    const params = new HttpParams().set('resolution', resolution);
    return firstValueFrom(
      this.http.get<ChartLiveResponse>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/chart/live`,
        { params },
      ),
    );
  }

  getLiveSnapshot(
    broker: string,
    accountId: string,
    sid: string,
    resolution: ChartLiveResolution,
  ): Promise<BotPanelLiveSnapshot> {
    const params = new HttpParams().set('resolution', resolution);
    return firstValueFrom(
      this.http.get<BotPanelLiveSnapshot>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/live-snapshot`,
        { params },
      ),
    );
  }

  liveStreamUrl(
    broker: string,
    accountId: string,
    sid: string,
    resolution: ChartLiveResolution,
    cursor?: string,
  ): string {
    const params = new URLSearchParams({ resolution });
    if (cursor) params.set('cursor', cursor);
    return `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/live-stream?${params.toString()}`;
  }

  getHistoryChart(
    broker: string,
    accountId: string,
    sid: string,
    preset: ChartHistoryPreset,
  ): Promise<ChartHistoryResponse> {
    const params = new HttpParams().set('preset', preset);
    return firstValueFrom(
      this.http.get<ChartHistoryResponse>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/chart/history`,
        { params },
      ),
    );
  }

  /** §14 Operator-gated raw evidence — bounded, paged, audit-logged. */
  getEvidence(
    broker: string,
    accountId: string,
    sid: string,
    options: {
      transactionRef?: string;
      cursor?: string | number;
      pageSize?: number;
      clientHint?: string;
    } = {},
  ): Promise<EvidencePage> {
    let params = new HttpParams();
    if (options.transactionRef) params = params.set('transaction_ref', options.transactionRef);
    if (options.cursor !== undefined) params = params.set('cursor', String(options.cursor));
    if (options.pageSize !== undefined) params = params.set('page_size', String(options.pageSize));
    if (options.clientHint) params = params.set('client_hint', options.clientHint);
    return firstValueFrom(
      this.http.get<EvidencePage>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/evidence`,
        { params },
      ),
    );
  }
}
