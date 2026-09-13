import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';

import {
  commandBodyOf,
  laneKey,
  type FleetCapability,
  type ResourceTarget,
  withCommand,
} from '../fleet/resource-target';
import { accountUrl, laneUrl } from '../fleet/clerk-scoped-url';
import { firstValueFrom } from 'rxjs';

import { PolledReadScheduler } from './polled-read-scheduler';

import type {
  AlpacaLiveVerdict,
  BrokerAccountSnapshot,
  BrokerActivity,
  BrokerOrder,
  BrokerOrderGroup,
  BrokerPortfolioHistory,
  PortfolioHistoryProof,
  BrokerPosition,
  ClerkStatus,
  CustodyDiagnosis,
  ManualOrderCapability,
  ManualOrderCancelRequest,
  ManualOrderCancellation,
  ManualOrderPreview,
  ManualOrderPreviewRequest,
  ManualOrderSubmitRequest,
  ManualOrderTicket,
  PortfolioHistoryRange,
  SqliteClerkProjection,
  SqliteRecoveryAction,
  SqliteRecoveryActionCheck,
  SqliteRecoveryResult,
  SqliteTimelinePage,
} from '../api/alpaca.types';
import type {
  ClerkTransactionDetail,
  ClerkTransactionFilters,
  ClerkTransactionHistoryResponse,
  ExternalOrderAcknowledgement,
} from '../api/clerk-transaction-history.types';

/** Exact, server-owned Clerk timeline filters; all values bind the keyset cursor. */
export interface SqliteTimelineQuery {
  readonly strategyInstanceId?: string;
  readonly orderRef?: string;
  readonly effectOperationId?: string;
  readonly uncertaintyId?: string;
  readonly executionId?: string;
  readonly transitionKind?: string;
  readonly sequence?: number;
  readonly cursor?: string;
  readonly pageSize?: number;
}

/**
 * Broker System v2 read client — targets `/api/brokers/{broker}/...`, the v2
 * data-plane surface (separate from the v1 `/api/broker/...` family). Phase 1
 * is read-only and Alpaca-only; the broker id is a parameter so later brokers
 * reuse the same client unchanged.
 *
 * It also carries Alpaca-only custody, recovery, manual-order, and transaction
 * families. Delivery C routes each through the same explicit clerk/account
 * URL seam as the broker-neutral reads; only the measured legacy reads called
 * out below remain broker-scoped until Delivery E.
 */
@Injectable({ providedIn: 'root' })
export class BrokersService {
  private readonly http = inject(HttpClient);
  private readonly polls = inject(PolledReadScheduler);
  private readonly base = '/api/brokers';
  private readonly accountReads = new Map<
    string,
    { expiresAtMs: number; promise: Promise<BrokerAccountSnapshot> }
  >();

  private static readonly ACCOUNT_CACHE_MS = 10_000;

  /** Commands receive a target frozen by the opening interaction. */
  private commandBody(
    target: ResourceTarget,
    capability: FleetCapability,
    payload: object,
  ): object {
    return commandBodyOf(withCommand(target, capability, target.idempotencyKey), payload);
  }

  getAccount(target: ResourceTarget): Promise<BrokerAccountSnapshot> {
    const cacheKey = laneKey(
      target.broker,
      target.clerkId,
      target.routingEpoch,
      target.bindingGeneration,
      target.accountId,
    );
    const cached = this.accountReads.get(cacheKey);
    if (cached && cached.expiresAtMs > Date.now()) return cached.promise;

    // Polled, and the promise below is cached — a hang would be handed to
    // every later caller until expiry, not just this one. The cache is keyed
    // by lane (FR-093): one lane's failure or restart never serves another
    // lane's account snapshot.
    const promise = this.polls.get<BrokerAccountSnapshot>(
      laneUrl(target, '/account'),
    );
    const entry = {
      expiresAtMs: Date.now() + BrokersService.ACCOUNT_CACHE_MS,
      promise,
    };
    this.accountReads.set(cacheKey, entry);
    void promise.catch(() => {
      if (this.accountReads.get(cacheKey) === entry) {
        this.accountReads.delete(cacheKey);
      }
    });
    return promise;
  }

  /**
   * The server-derived Alpaca live verdict (ADR 0059 D8). Pure on the
   * server — it never contacts the broker — so it is safe to poll from the
   * shell. The client renders it and never composes one (ADR 0011 §7).
   */
  getLiveVerdict(): Promise<AlpacaLiveVerdict> {
    // Polled from the shell every 5 s, so it shares the scheduler every
    // polled read goes through (#1912) rather than adding a fifth
    // independent poller to the roster's tick.
    return this.polls.get<AlpacaLiveVerdict>(`${this.base}/alpaca/live-verdict`);
  }

  listPositions(target: ResourceTarget): Promise<BrokerPosition[]> {
    return firstValueFrom(
      this.http.get<BrokerPosition[]>(laneUrl(target, '/positions')),
    );
  }

  /**
   * Account-wide Alpaca activity, bounded by the data plane. `afterMs` is an
   * int64 UTC cursor owned by the caller's selected activity window.
   */
  listActivities(
    target: ResourceTarget,
    options: { afterMs?: number; currentSession?: boolean; limit?: number } = {},
  ): Promise<BrokerActivity[]> {
    let params = new HttpParams();
    if (options.afterMs != null) {
      params = params.set('after_ms', options.afterMs);
    }
    if (options.limit != null) {
      params = params.set('limit', options.limit);
    }
    if (options.currentSession) {
      params = params.set('current_session', true);
    }
    return firstValueFrom(
      this.http.get<BrokerActivity[]>(laneUrl(target, '/activities'), { params }),
    );
  }

  /** Broker-owned account equity history; values are rendered without local P&L math. */
  getPortfolioHistory(
    target: ResourceTarget,
    historyRange: PortfolioHistoryRange,
  ): Promise<BrokerPortfolioHistory> {
    const params = new HttpParams().set('range', historyRange);
    return firstValueFrom(
      this.http.get<BrokerPortfolioHistory>(laneUrl(target, '/portfolio-history'), { params }),
    );
  }

  /** C1 curve plus C2/C3 reconciliation proof; the browser derives no P&L. */
  getPortfolioHistoryProof(
    target: ResourceTarget,
    historyRange: PortfolioHistoryRange,
  ): Promise<PortfolioHistoryProof> {
    const params = new HttpParams().set('range', historyRange);
    return firstValueFrom(
      this.http.get<PortfolioHistoryProof>(laneUrl(target, '/portfolio-history-proof'), { params }),
    );
  }

  listOrderGroups(
    broker = 'alpaca',
    options: { status?: 'open' | 'closed' | 'all'; limit?: number } = {},
  ): Promise<BrokerOrderGroup[]> {
    let params = new HttpParams();
    if (options.status) {
      params = params.set('status', options.status);
    }
    if (options.limit != null) {
      params = params.set('limit', options.limit);
    }
    return firstValueFrom(
      this.http.get<BrokerOrderGroup[]>(`${this.base}/${broker}/order-groups`, { params }),
    );
  }

  listOrders(
    clerkId: string,
    options: { status?: 'open' | 'closed' | 'all'; limit?: number } = {},
    broker = 'alpaca',
  ): Promise<BrokerOrder[]> {
    let params = new HttpParams();
    if (options.status) {
      params = params.set('status', options.status);
    }
    if (options.limit != null) {
      params = params.set('limit', options.limit);
    }
    return firstValueFrom(
      this.http.get<BrokerOrder[]>(laneUrl({ broker, clerkId }, '/orders'), { params }),
    );
  }

  /** SQLite authority's policy answer for an ordered manual equity ticket. */
  getSqliteManualOrderCapability(
    clerkId: string,
    accountId: string,
  ): Promise<ManualOrderCapability> {
    return firstValueFrom(
      this.http.get<ManualOrderCapability>(
        accountUrl({ broker: 'alpaca', clerkId, accountId }, '/manual-orders/capability'),
      ),
    );
  }

  previewSqliteManualOrder(
    clerkId: string,
    accountId: string,
    request: ManualOrderPreviewRequest,
  ): Promise<ManualOrderPreview> {
    // A computation over durable state: read-idempotent, no envelope.
    return firstValueFrom(
      this.http.post<ManualOrderPreview>(
        accountUrl({ broker: 'alpaca', clerkId, accountId }, '/manual-orders/preview'),
        request,
      ),
    );
  }

  submitSqliteManualOrder(
    target: ResourceTarget,
    ticketId: string,
    request: ManualOrderSubmitRequest,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.put<ManualOrderTicket>(
        accountUrl(target, `/manual-order-tickets/${encodeURIComponent(ticketId)}`),
        this.commandBody(target, 'manual_orders', request),
      ),
    );
  }

  /** Explicitly activate the next reviewed SQLite manual-ticket leg. */
  continueSqliteManualOrderTicket(
    target: ResourceTarget,
    ticketId: string,
    request: ManualOrderSubmitRequest,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.post<ManualOrderTicket>(
        accountUrl(target, `/manual-order-tickets/${encodeURIComponent(ticketId)}/continue`),
        this.commandBody(target, 'manual_orders', request),
      ),
    );
  }

  /** Cancel all verified working orders owned by one manual ticket. */
  cancelSqliteManualOrderTicket(
    target: ResourceTarget,
    ticketId: string,
    request: ManualOrderCancelRequest,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.post<ManualOrderTicket>(
        accountUrl(target, `/manual-order-tickets/${encodeURIComponent(ticketId)}/cancel`),
        this.commandBody(target, 'manual_orders', request),
      ),
    );
  }

  getSqliteManualOrderTicket(
    clerkId: string,
    accountId: string,
    ticketId: string,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.get<ManualOrderTicket>(
        accountUrl(
          { broker: 'alpaca', clerkId, accountId },
          `/manual-order-tickets/${encodeURIComponent(ticketId)}`,
        ),
      ),
    );
  }

  /** Cancel the exact SQLite-owned manual order reference once, durably. */
  cancelSqliteManualOrder(
    target: ResourceTarget,
    orderRef: string,
    request: ManualOrderCancelRequest,
  ): Promise<ManualOrderCancellation> {
    return firstValueFrom(
      this.http.post<ManualOrderCancellation>(
        accountUrl(target, `/manual-orders/${encodeURIComponent(orderRef)}/cancel`),
        this.commandBody(target, 'manual_orders', request),
      ),
    );
  }

  /**
   * Phase-2 S6 — the clerk's observable state: the exposure hold, the latest
   * reconciliation verdict, and the outstanding-intent count. A protected read
   * under `/api/brokers` (the proxy attaches the shared secret for that prefix).
   */
  getClerkStatus(target: ResourceTarget): Promise<ClerkStatus> {
    // Shares the roster page's poll guard with getAccount (S7), and its
    // scheduler: the two fire on the same 15 s tick (#1912).
    return this.polls.get<ClerkStatus>(laneUrl(target, '/clerk/status'));
  }

  /**
   * Slice 1 — the structured, backend-authored Clerk↔broker custody diagnosis
   * the Accounts page renders verbatim. A protected read under `/api/brokers`
   * (the proxy attaches the shared secret for that prefix), like
   * {@link getClerkStatus}.
   */
  getCustodyDiagnosis(target: ResourceTarget): Promise<CustodyDiagnosis> {
    return firstValueFrom(
      this.http.get<CustodyDiagnosis>(laneUrl(target, '/clerk/custody-diagnosis')),
    );
  }

  getSqliteClerkProjection(
    clerkId: string,
    accountId: string,
  ): Promise<SqliteClerkProjection> {
    return firstValueFrom(
      this.http.get<SqliteClerkProjection>(
        accountUrl({ broker: 'alpaca', clerkId, accountId }, '/custody/snapshot'),
      ),
    );
  }

  getSqliteClerkTimeline(
    clerkId: string,
    accountId: string,
    query: SqliteTimelineQuery = {},
  ): Promise<SqliteTimelinePage> {
    let params = new HttpParams();
    const queryEntries: readonly [string, string | number | undefined][] = [
      ['strategy_instance_id', query.strategyInstanceId],
      ['order_ref', query.orderRef],
      ['effect_operation_id', query.effectOperationId],
      ['uncertainty_id', query.uncertaintyId],
      ['execution_id', query.executionId],
      ['transition_kind', query.transitionKind],
      ['sequence', query.sequence],
      ['cursor', query.cursor],
      ['page_size', query.pageSize],
    ];
    for (const [key, value] of queryEntries) {
      if (value !== undefined) params = params.set(key, String(value));
    }
    return firstValueFrom(
      this.http.get<SqliteTimelinePage>(
        accountUrl({ broker: 'alpaca', clerkId, accountId }, '/custody/timeline'),
        { params },
      ),
    );
  }

  async checkSqliteRecoveryAction(
    clerkId: string,
    accountId: string,
    action: Pick<SqliteRecoveryAction, 'action_id' | 'concurrency_token'>,
    strategyInstanceId: string | null = null,
  ): Promise<SqliteRecoveryAction> {
    // A diagnostic over durable state: read-idempotent, no envelope.
    const actionPath: `/${string}` = strategyInstanceId === null
      ? '/custody/recovery-actions/check'
      : `/custody/bots/${encodeURIComponent(strategyInstanceId)}/recovery-actions/check`;
    const response = await firstValueFrom(
      this.http.post<SqliteRecoveryActionCheck>(
        accountUrl(
          { broker: 'alpaca', clerkId, accountId },
          actionPath,
        ),
        {
          action_id: action.action_id,
          concurrency_token: action.concurrency_token,
        },
      ),
    );
    return response.capability;
  }

  executeSqliteRecoveryAction(
    target: ResourceTarget,
    action: SqliteRecoveryAction,
  ): Promise<SqliteRecoveryResult> {
    return firstValueFrom(
      this.http.post<SqliteRecoveryResult>(
        accountUrl(target, '/custody/recovery-actions/execute'),
        this.commandBody(
          target,
          'custody_command',
          {
            action_id: action.action_id,
            concurrency_token: action.concurrency_token,
            execution_ref: action.execution_ref,
          },
        ),
      ),
    );
  }

  /**
   * Clerk transaction history — the bounded, account-scoped SQLite projection
   * under `/api/accounts/{account_id}/transactions`. `cursor` is opaque and
   * relayed verbatim; the window bounds are int64 ms UTC and filtered
   * server-side, never in the browser.
   */
  accountTransactions(
    clerkId: string,
    accountId: string,
    cursor: string | null = null,
    limit = 50,
    filters: ClerkTransactionFilters = {},
  ): Promise<ClerkTransactionHistoryResponse> {
    const params: Record<string, string | number> = { limit };
    if (cursor !== null) params['cursor'] = cursor;
    if (filters.origin) params['origin'] = filters.origin;
    if (filters.lifecycleState) params['lifecycle_state'] = filters.lifecycleState;
    if (filters.strategyInstanceId) params['strategy_instance_id'] = filters.strategyInstanceId;
    if (filters.runId) params['run_id'] = filters.runId;
    if (filters.fromMs !== null && filters.fromMs !== undefined) params['from_ms'] = filters.fromMs;
    if (filters.toMs !== null && filters.toMs !== undefined) params['to_ms'] = filters.toMs;
    return firstValueFrom(
      this.http.get<ClerkTransactionHistoryResponse>(
        accountUrl({ broker: 'alpaca', clerkId, accountId }, '/custody/transactions'),
        { params },
      ),
    );
  }

  accountTransaction(
    clerkId: string,
    accountId: string,
    transactionId: string,
  ): Promise<ClerkTransactionDetail> {
    return firstValueFrom(
      this.http.get<ClerkTransactionDetail>(
        accountUrl(
          { broker: 'alpaca', clerkId, accountId },
          `/custody/transactions/${encodeURIComponent(transactionId)}`,
        ),
      ),
    );
  }

  acknowledgeExternalOrder(
    target: ResourceTarget,
    externalOrderId: string,
    operator: string,
  ): Promise<ExternalOrderAcknowledgement> {
    return firstValueFrom(
      this.http.post<ExternalOrderAcknowledgement>(
        accountUrl(
          target,
          `/custody/transactions/external-orders/${encodeURIComponent(externalOrderId)}/acknowledge`,
        ),
        this.commandBody(target, 'custody_command', { operator }),
      ),
    );
  }
}
