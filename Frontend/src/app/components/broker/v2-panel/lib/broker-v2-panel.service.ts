import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { PolledReadScheduler } from '../../../../services/polled-read-scheduler';
import type { components } from '../../../../api/broker.types';
import type {
  HistoricalExecutionRecoveryPlan,
  HistoricalExecutionRecoveryReceipt,
  SqliteExtendedLimitConfirmation,
  SqliteRecoveryResult,
} from '../../../../api/alpaca.types';
import {
  ResourceTarget,
  commandBodyOf,
  type FleetCapability,
  withCommand,
} from '../../../../fleet/resource-target';
import { operationUrl } from '../../../../fleet/operation-url';
import type {
  BotCatalogView,
  BotPanelView,
  CohortActionResult,
  CohortArchiveRequest,
  CohortArchiveView,
  CohortFlattenRequest,
  CohortFlattenView,
  BotPanelLiveSnapshot,
  BotRunHistoryPage,
  BotRunView,
  ChartHistoryTimeframe,
  ChartHistoryResponse,
  ChartLiveResolution,
  ChartLiveResponse,
  EvidencePage,
  PanelAction,
  PanelActionRequest,
  PanelActionResult,
  PanelProfile,
  PanelQuiesceActionId,
} from './broker-v2-panel.types';

/** Keyed by the generated quiesce action union (the backend's
 * `QuiesceActionId`), so a set the backend widens or narrows fails to compile
 * here instead of drifting (#2351). */
const QUIESCE_ACTIONS: Readonly<Record<PanelQuiesceActionId, true>> = {
  stop: true,
  flatten_stop: true,
  stop_bot_decisions: true,
  cancel_verified_working_orders: true,
  execute_safe_flatten: true,
  reconcile_now: true,
  discharge_attributed_residue: true,
};

function isQuiesceAction(actionId: PanelActionRequest['action_id']): actionId is PanelQuiesceActionId {
  return Object.hasOwn(QUIESCE_ACTIONS, actionId);
}

/** The key the deploy-window fallback posts under. A coordinator that did
 * route the quiesce attempt recorded it under the frozen key, and it refuses
 * that key on a different operation. */
const LEGACY_ACTIONS_KEY_SUFFIX = ':actions';
const IDEMPOTENCY_KEY_MAX_LENGTH = 128;

/** A 404 no handler produced: the framework's own unrouted answer
 * (`{"detail": "Not Found"}`), never a typed refusal — a fleet refusal
 * carries a `reason`, a panel refusal a structured `detail`. */
function isUnroutedNotFound(error: unknown): boolean {
  if (!(error instanceof HttpErrorResponse) || error.status !== 404) return false;
  const body: unknown = error.error;
  if (typeof body !== 'object' || body === null || 'reason' in body) return false;
  return 'detail' in body && body.detail === 'Not Found';
}

export type DeployBotBody = components['schemas']['AlpacaPaperDeployRequest'];
export type DeployBotReceipt = components['schemas']['AlpacaPaperDeployReceipt'];
export type DeployBotView = components['schemas']['AlpacaPaperDeployView'];
export type DeployBotStrategy = components['schemas']['AlpacaPaperDeployStrategy'];
export type DeployStrategyParamsSchema = components['schemas']['StrategyParamsSchema'];
export type DeployStrategyParamProperty = components['schemas']['ParamPropertySchema'];
export type DeployReadinessCheck = components['schemas']['AlpacaPaperDeployReadinessCheck'];
export type DeployExecutionMode = components['schemas']['AlpacaPaperExecutionMode'];
export type DeploySizingOption = components['schemas']['AlpacaPaperSizingOption'];
export type RunAdmissionDecision = components['schemas']['RunAdmissionDecision'];
// Pydantic emits -Input/-Output variants for the plan (its nested evidence
// model carries a defaulted field); the two are byte-identical, so one alias
// serves both the plan response and the confirm request body.
export type PaperAccessPlan = components['schemas']['CanaryActivationPlan-Output'];
export type PaperAccessEvent = components['schemas']['CanaryAdmissionEvent'];

/**
 * HTTP client for the broker-v2 panel surface.
 *
 * Targets the clerk-scoped routing surface (fleet delivery B):
 * `/api/brokers/{broker}/clerks/{clerkId}/accounts/{accountId}/…`. Every
 * request carries broker and clerk identity (FR-092), commands carry the
 * §10.3 envelope frozen from the caller's `ResourceTarget` (FR-094/095),
 * and the two unscoped legacy bot-run reads stay on their compatibility
 * paths until delivery E's measured retirement.
 */
@Injectable({ providedIn: 'root' })
export class BrokerV2PanelService {
  private readonly http = inject(HttpClient);
  private readonly polls = inject(PolledReadScheduler);

  /**
   * Wrap one command payload with the §10.3 envelope. The envelope's
   * idempotency identity is frozen by the interaction owner before it calls
   * the service. The capability is the endpoint's declared catalog
   * capability, not caller input.
   */
  private commandBody(
    target: ResourceTarget,
    capability: FleetCapability,
    payload: object,
  ): object {
    const payloadKey = (payload as { idempotency_key?: string }).idempotency_key;
    if (target.idempotencyKey === null) {
      throw new Error('A durable command requires a target with a frozen idempotency key.');
    }
    if (payloadKey !== undefined && payloadKey !== target.idempotencyKey) {
      throw new Error('The command payload idempotency key must match its frozen target.');
    }
    return commandBodyOf(withCommand(target, capability, target.idempotencyKey), payload);
  }

  getPanelProfile(broker: string): Promise<PanelProfile> {
    // Retained-legacy broker-level read (delivery B inventory); clerk scoping
    // for it would ride a future catalog entry, not this delivery.
    return firstValueFrom(
      this.http.get<PanelProfile>(`/api/brokers/${encodeURIComponent(broker)}/panel-profile`),
    );
  }

  deployBot(target: ResourceTarget, body: DeployBotBody): Promise<DeployBotReceipt> {
    return firstValueFrom(
      this.http.post<DeployBotReceipt>(
        operationUrl('bot_create', target),
        this.commandBody(target, 'bot_action', body),
      ),
    );
  }

  previewStartAdmission(
    target: ResourceTarget,
    body: DeployBotBody,
  ): Promise<RunAdmissionDecision> {
    // A plan over durable state — read-idempotent, no envelope required.
    return firstValueFrom(
      this.http.post<RunAdmissionDecision>(
        operationUrl('bot_admission_plan', target),
        body,
      ),
    );
  }

  /**
   * `symbol` scopes the channel-health verdict to the instrument the operator
   * intends to trade. Omitted, the view reports account-level channel presence
   * and connectivity only (issue #1777).
   */
  getDeployView(
    target: ResourceTarget,
    symbol?: string,
  ): Promise<DeployBotView> {
    const params = symbol ? new HttpParams().set('symbol', symbol) : undefined;
    return firstValueFrom(
      this.http.get<DeployBotView>(operationUrl('bots_deploy_read', target), { params }),
    );
  }

  preparePaperAccess(
    target: ResourceTarget,
    strategyKey: string,
    reason: string,
  ): Promise<PaperAccessPlan> {
    return firstValueFrom(
      this.http.post<PaperAccessPlan>(
        operationUrl('paper_access_plan', { ...target, programKey: strategyKey }),
        { reason },
      ),
    );
  }

  confirmPaperAccess(
    target: ResourceTarget,
    strategyKey: string,
    plan: PaperAccessPlan,
  ): Promise<PaperAccessEvent> {
    return firstValueFrom(
      this.http.post<PaperAccessEvent>(
        operationUrl('paper_access_confirm', { ...target, programKey: strategyKey }),
        this.commandBody(target, 'deploy', { plan, confirmation_token: plan.confirmation_token }),
      ),
    );
  }

  getCatalog(target: ResourceTarget): Promise<BotCatalogView[]> {
    // Polled every few seconds; a hang here freezes the roster (S7), and
    // overlapping polls are what turn a 267 ms read into a 2.58 s one
    // (#1912) — the scheduler answers both.
    return this.polls.get<BotCatalogView[]>(operationUrl('bots_catalog_read', target));
  }

  getPanel(
    target: ResourceTarget,
    sid: string,
    transactionRef?: string,
  ): Promise<BotPanelView> {
    let params = new HttpParams();
    if (transactionRef) {
      params = params.set('transaction_ref', transactionRef);
    }
    // Polled by the roster's detail pane on the same tick as the catalog
    // (#1912), so it shares the roster's scheduler rather than adding to
    // the fan-out it is trying to remove.
    return this.polls.get<BotPanelView>(
      operationUrl('bot_panel_read', { ...target, sid }),
      params,
    );
  }

  /**
   * The archivable roster, grouped and backend-authored (ADR 0052).
   *
   * Fetched on demand, never polled: it builds a panel projection per
   * candidate, which is priced for an operator opening a surface rather
   * than for a poll loop. Deliberately outside the polled-read scheduler for the
   * same reason — it is not one of the reads whose fan-out #1912 coalesces.
   */
  getCohortArchiveView(target: ResourceTarget): Promise<CohortArchiveView> {
    return firstValueFrom(
      this.http.get<CohortArchiveView>(operationUrl('bot_cohort_archive_read', target)),
    );
  }

  /** Archive the named legs; every leg comes back with a typed outcome. */
  runCohortArchive(
    target: ResourceTarget,
    request: CohortArchiveRequest,
  ): Promise<CohortActionResult> {
    return firstValueFrom(
      this.http.post<CohortActionResult>(
        operationUrl('bot_cohort_archive', target),
        this.commandBody(target, 'bot_action', request),
      ),
    );
  }

  /**
   * The cohort-flatten presentation: (strategy, symbol) cohorts with each
   * member's presented flatten facts (ADR 0051 Decision 3).
   *
   * Fetched on demand, never polled — like the archive view, it builds a
   * panel projection per cohort member, priced for an operator opening a
   * surface rather than for a poll loop.
   */
  getCohortFlattenView(target: ResourceTarget): Promise<CohortFlattenView> {
    return firstValueFrom(
      this.http.get<CohortFlattenView>(operationUrl('bot_cohort_flatten_read', target)),
    );
  }

  /** Flatten exactly the named legs; every attempted leg comes back typed. */
  runCohortFlatten(
    target: ResourceTarget,
    request: CohortFlattenRequest,
  ): Promise<CohortActionResult> {
    return firstValueFrom(
      this.http.post<CohortActionResult>(
        operationUrl('bot_cohort_flatten', target),
        this.commandBody(target, 'bot_action', request),
      ),
    );
  }

  getCurrentRun(target: ResourceTarget, sid: string): Promise<BotRunView> {
    return firstValueFrom(
      this.http.get<BotRunView>(
        operationUrl('bot_run_current_read', { ...target, sid }),
      ),
    );
  }

  getRunHistory(
    target: ResourceTarget,
    sid: string,
    cursor?: string,
  ): Promise<BotRunHistoryPage> {
    let params = new HttpParams().set('limit', '1');
    if (cursor) params = params.set('cursor', cursor);
    return firstValueFrom(
      this.http.get<BotRunHistoryPage>(
        operationUrl('bot_run_history_read', { ...target, sid }),
        { params },
      ),
    );
  }

  /**
   * Run one presented panel action. The quiesce actions — stop, flatten,
   * the recovery stop/cancel/flatten and reconcile — travel on their own
   * catalog operation, which a draining lane still routes while it refuses
   * every other action (#2351, ADR 0063 §2) — so the operator can make a
   * draining lane quiet from this panel.
   *
   * Deploy window: `my-frontend` serves this code the moment the main
   * checkout is pulled, while a coordinator or clerk still running the
   * previous build has no `/actions/quiesce` route until it restarts. Only
   * that unrouted 404 — nothing ran — falls back to `/actions`, under a
   * derived key; a typed refusal never does.
   */
  async runAction(
    target: ResourceTarget,
    sid: string,
    request: PanelActionRequest,
  ): Promise<PanelActionResult> {
    if (!isQuiesceAction(request.action_id)) {
      return this.postPanelAction('bot_panel_action', target, sid, request);
    }
    try {
      return await this.postPanelAction('bot_panel_quiesce_action', target, sid, request);
    } catch (error) {
      const fallbackKey = `${request.idempotency_key}${LEGACY_ACTIONS_KEY_SUFFIX}`;
      if (!isUnroutedNotFound(error) || fallbackKey.length > IDEMPOTENCY_KEY_MAX_LENGTH) {
        throw error;
      }
      return this.postPanelAction(
        'bot_panel_action',
        withCommand(target, 'bot_action', fallbackKey),
        sid,
        { ...request, idempotency_key: fallbackKey },
      );
    }
  }

  private postPanelAction(
    operation: 'bot_panel_action' | 'bot_panel_quiesce_action',
    target: ResourceTarget,
    sid: string,
    request: PanelActionRequest,
  ): Promise<PanelActionResult> {
    return firstValueFrom(
      this.http.post<PanelActionResult>(
        operationUrl(operation, { ...target, sid }),
        this.commandBody(target, 'bot_action', request),
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
    target: ResourceTarget,
    sid: string,
    action: PanelAction,
    reason: string | null = null,
  ): Promise<PanelActionResult> {
    if (target.idempotencyKey === null) {
      throw new Error('A bot action requires a target frozen by its interaction owner.');
    }
    // A retry is still the same command, so it must keep the exact target the
    // interaction owner captured before presenting the action.
    const actionTarget = target;
    try {
      return await this.submitAction(actionTarget, sid, action, reason);
    } catch (error) {
      if (
        !(error instanceof HttpErrorResponse) ||
        error.status !== 409 ||
        action.confirmation !== null
      ) {
        throw error;
      }
      const panel = await this.getPanel(actionTarget, sid);
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
      return await this.submitAction(actionTarget, sid, fresh, reason);
    }
  }

  private submitAction(
    target: ResourceTarget,
    sid: string,
    action: PanelAction,
    reason: string | null,
  ): Promise<PanelActionResult> {
    const idempotencyKey = target.idempotencyKey;
    if (idempotencyKey === null) {
      throw new Error('A bot action requires a target frozen by its interaction owner.');
    }
    const request: PanelActionRequest = {
      action_id: action.action_id,
      revision: action.revision,
      concurrency_token: action.concurrency_token,
      idempotency_key: idempotencyKey,
      reason,
    };
    return this.runAction(target, sid, request);
  }

  /**
   * Read the exact Alpaca paper activity for a presented SQLite recovery
   * capability. This has its own endpoint because it is a signed evidence
   * ceremony, not a generic panel action.
   */
  prepareHistoricalExecutionRecovery(
    target: ResourceTarget,
    sid: string,
    concurrencyToken: string,
  ): Promise<HistoricalExecutionRecoveryPlan> {
    return firstValueFrom(
      this.http.post<HistoricalExecutionRecoveryPlan>(
        operationUrl('custody_historical_recovery_prepare', { ...target, sid }),
        this.commandBody(target, 'custody_command', { concurrency_token: concurrencyToken }),
      ),
    );
  }

  /** Confirm exactly the short-lived evidence plan returned by prepare. */
  confirmHistoricalExecutionRecovery(
    target: ResourceTarget,
    sid: string,
    plan: HistoricalExecutionRecoveryPlan,
  ): Promise<HistoricalExecutionRecoveryReceipt> {
    return firstValueFrom(
      this.http.post<HistoricalExecutionRecoveryReceipt>(
        operationUrl('custody_historical_recovery_confirm', { ...target, sid }),
        this.commandBody(target, 'custody_command', { plan, confirmation_token: plan.confirmation_token }),
      ),
    );
  }

  /**
   * Send an extended-hours safe flatten at the operator's confirmed limit
   * (#2007). Goes through the bot-scoped custody route, not the generic panel
   * action: outside the regular session the panel presents the unpriced
   * flatten disabled, and only a priced one can be sent.
   */
  executeExtendedSafeFlatten(
    target: ResourceTarget,
    sid: string,
    concurrencyToken: string,
    extendedLimit: SqliteExtendedLimitConfirmation,
  ): Promise<SqliteRecoveryResult> {
    return firstValueFrom(
      this.http.post<SqliteRecoveryResult>(
        operationUrl('custody_bot_recovery_execute', { ...target, sid }),
        this.commandBody(target, 'custody_command', {
          action_id: 'execute_safe_flatten',
          concurrency_token: concurrencyToken,
          extended_limit: extendedLimit,
        }),
      ),
    );
  }

  getLiveChart(
    target: ResourceTarget,
    sid: string,
    resolution: ChartLiveResolution,
  ): Promise<ChartLiveResponse> {
    const params = new HttpParams().set('resolution', resolution);
    // The tape polls beside the detail pane's panel read (#1912).
    return this.polls.get<ChartLiveResponse>(
      operationUrl('bot_chart_live', { ...target, sid }),
      params,
    );
  }

  getLiveSnapshot(
    target: ResourceTarget,
    sid: string,
    resolution: ChartLiveResolution,
  ): Promise<BotPanelLiveSnapshot> {
    const params = new HttpParams().set('resolution', resolution);
    return firstValueFrom(
      this.http.get<BotPanelLiveSnapshot>(
        operationUrl('bot_live_snapshot', { ...target, sid }),
        { params },
      ),
    );
  }

  liveStreamUrl(
    target: ResourceTarget,
    sid: string,
    resolution: ChartLiveResolution,
    cursor?: string,
  ): string {
    const params = new URLSearchParams({ resolution });
    if (cursor) params.set('cursor', cursor);
    return `${operationUrl('bot_live_stream', { ...target, sid })}?${params.toString()}`;
  }

  getHistoryChart(
    target: ResourceTarget,
    sid: string,
    timeframe: ChartHistoryTimeframe,
  ): Promise<ChartHistoryResponse> {
    const params = new HttpParams().set('timeframe', timeframe);
    return firstValueFrom(
      this.http.get<ChartHistoryResponse>(
        operationUrl('bot_chart_history', { ...target, sid }),
        { params },
      ),
    );
  }

  /** §14 Operator-gated raw evidence — bounded, paged, audit-logged. */
  getEvidence(
    target: ResourceTarget,
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
        operationUrl('bot_evidence', { ...target, sid }),
        { params },
      ),
    );
  }
}
