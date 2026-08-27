import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { firstValueWithinPollTimeout } from './poll-timeout';

import type {
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
 * Also carries the two neighbouring Alpaca Clerk surfaces that are not under
 * the `/api/brokers` prefix: `/api/alpaca-clerk-sqlite` (snapshot, timeline,
 * recovery actions) and `/api/accounts/{account_id}/transactions` (the Clerk
 * transaction-history projection). Both are served by Alpaca-only routers, so
 * this is the whole of the frontend's Alpaca Clerk read surface.
 */
@Injectable({ providedIn: 'root' })
export class BrokersService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/brokers';
  private readonly accountsBase = '/api/accounts';
  private readonly accountReads = new Map<
    string,
    { expiresAtMs: number; promise: Promise<BrokerAccountSnapshot> }
  >();

  private static readonly ACCOUNT_CACHE_MS = 10_000;

  getAccount(broker = 'alpaca'): Promise<BrokerAccountSnapshot> {
    const cached = this.accountReads.get(broker);
    if (cached && cached.expiresAtMs > Date.now()) return cached.promise;

    // Polled, and the promise below is cached — a hang would be handed to
    // every later caller until expiry, not just this one.
    const promise = firstValueWithinPollTimeout(
      this.http.get<BrokerAccountSnapshot>(`${this.base}/${broker}/account`),
    );
    const entry = {
      expiresAtMs: Date.now() + BrokersService.ACCOUNT_CACHE_MS,
      promise,
    };
    this.accountReads.set(broker, entry);
    void promise.catch(() => {
      if (this.accountReads.get(broker) === entry) {
        this.accountReads.delete(broker);
      }
    });
    return promise;
  }

  listPositions(broker = 'alpaca'): Promise<BrokerPosition[]> {
    return firstValueFrom(
      this.http.get<BrokerPosition[]>(`${this.base}/${broker}/positions`),
    );
  }

  /**
   * Account-wide Alpaca activity, bounded by the data plane. `afterMs` is an
   * int64 UTC cursor owned by the caller's selected activity window.
   */
  listActivities(
    broker = 'alpaca',
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
      this.http.get<BrokerActivity[]>(`${this.base}/${broker}/activities`, { params }),
    );
  }

  /** Broker-owned account equity history; values are rendered without local P&L math. */
  getPortfolioHistory(
    broker: string,
    historyRange: PortfolioHistoryRange,
  ): Promise<BrokerPortfolioHistory> {
    const params = new HttpParams().set('range', historyRange);
    return firstValueFrom(
      this.http.get<BrokerPortfolioHistory>(`${this.base}/${broker}/portfolio-history`, { params }),
    );
  }

  /** C1 curve plus C2/C3 reconciliation proof; the browser derives no P&L. */
  getPortfolioHistoryProof(
    broker: string,
    historyRange: PortfolioHistoryRange,
  ): Promise<PortfolioHistoryProof> {
    const params = new HttpParams().set('range', historyRange);
    return firstValueFrom(
      this.http.get<PortfolioHistoryProof>(`${this.base}/${broker}/portfolio-history-proof`, { params }),
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
    broker = 'alpaca',
    options: { status?: 'open' | 'closed' | 'all'; limit?: number } = {},
  ): Promise<BrokerOrder[]> {
    let params = new HttpParams();
    if (options.status) {
      params = params.set('status', options.status);
    }
    if (options.limit != null) {
      params = params.set('limit', options.limit);
    }
    return firstValueFrom(
      this.http.get<BrokerOrder[]>(`${this.base}/${broker}/orders`, { params }),
    );
  }

  /** SQLite authority's policy answer for an ordered manual equity ticket. */
  getSqliteManualOrderCapability(accountId: string): Promise<ManualOrderCapability> {
    return firstValueFrom(
      this.http.get<ManualOrderCapability>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-orders/capability`,
      ),
    );
  }

  previewSqliteManualOrder(
    accountId: string,
    request: ManualOrderPreviewRequest,
  ): Promise<ManualOrderPreview> {
    return firstValueFrom(
      this.http.post<ManualOrderPreview>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-orders/preview`,
        request,
      ),
    );
  }

  submitSqliteManualOrder(
    accountId: string,
    ticketId: string,
    request: ManualOrderSubmitRequest,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.put<ManualOrderTicket>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-order-tickets/${encodeURIComponent(ticketId)}`,
        request,
      ),
    );
  }

  /** Explicitly activate the next reviewed SQLite manual-ticket leg. */
  continueSqliteManualOrderTicket(
    accountId: string,
    ticketId: string,
    request: ManualOrderSubmitRequest,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.post<ManualOrderTicket>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-order-tickets/${encodeURIComponent(ticketId)}/continue`,
        request,
      ),
    );
  }

  /** Cancel all verified working orders owned by one manual ticket. */
  cancelSqliteManualOrderTicket(
    accountId: string,
    ticketId: string,
    request: ManualOrderCancelRequest,
  ): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.post<ManualOrderTicket>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-order-tickets/${encodeURIComponent(ticketId)}/cancel`,
        request,
      ),
    );
  }

  getSqliteManualOrderTicket(accountId: string, ticketId: string): Promise<ManualOrderTicket> {
    return firstValueFrom(
      this.http.get<ManualOrderTicket>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-order-tickets/${encodeURIComponent(ticketId)}`,
      ),
    );
  }

  /** Cancel the exact SQLite-owned manual order reference once, durably. */
  cancelSqliteManualOrder(
    accountId: string,
    orderRef: string,
    request: ManualOrderCancelRequest,
  ): Promise<ManualOrderCancellation> {
    return firstValueFrom(
      this.http.post<ManualOrderCancellation>(
        `${this.base}/alpaca/accounts/${encodeURIComponent(accountId)}/manual-orders/${encodeURIComponent(orderRef)}/cancel`,
        request,
      ),
    );
  }

  /**
   * Phase-2 S6 — the clerk's observable state: the exposure hold, the latest
   * reconciliation verdict, and the outstanding-intent count. A protected read
   * under `/api/brokers` (the proxy attaches the shared secret for that prefix).
   */
  getClerkStatus(broker = 'alpaca'): Promise<ClerkStatus> {
    // Shares the roster page's poll guard with getAccount (S7).
    return firstValueWithinPollTimeout(
      this.http.get<ClerkStatus>(`${this.base}/${broker}/clerk/status`),
    );
  }

  /**
   * Slice 1 — the structured, backend-authored Clerk↔broker custody diagnosis
   * the Accounts page renders verbatim. A protected read under `/api/brokers`
   * (the proxy attaches the shared secret for that prefix), like
   * {@link getClerkStatus}.
   */
  getCustodyDiagnosis(broker = 'alpaca'): Promise<CustodyDiagnosis> {
    return firstValueFrom(
      this.http.get<CustodyDiagnosis>(`${this.base}/${broker}/clerk/custody-diagnosis`),
    );
  }

  getSqliteClerkProjection(accountId: string): Promise<SqliteClerkProjection> {
    return firstValueFrom(
      this.http.get<SqliteClerkProjection>(
        `/api/alpaca-clerk-sqlite/accounts/${encodeURIComponent(accountId)}/snapshot`,
      ),
    );
  }

  getSqliteClerkTimeline(
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
        `/api/alpaca-clerk-sqlite/accounts/${encodeURIComponent(accountId)}/timeline`,
        { params },
      ),
    );
  }

  async checkSqliteRecoveryAction(
    accountId: string,
    action: Pick<SqliteRecoveryAction, 'action_id' | 'concurrency_token'>,
    strategyInstanceId: string | null = null,
  ): Promise<SqliteRecoveryAction> {
    const botScope = strategyInstanceId === null
      ? ''
      : `/bots/${encodeURIComponent(strategyInstanceId)}`;
    const response = await firstValueFrom(
      this.http.post<SqliteRecoveryActionCheck>(
        `/api/alpaca-clerk-sqlite/accounts/${encodeURIComponent(accountId)}${botScope}/recovery-actions/check`,
        {
          action_id: action.action_id,
          concurrency_token: action.concurrency_token,
        },
      ),
    );
    return response.capability;
  }

  executeSqliteRecoveryAction(
    accountId: string,
    action: SqliteRecoveryAction,
  ): Promise<SqliteRecoveryResult> {
    return firstValueFrom(
      this.http.post<SqliteRecoveryResult>(
        `/api/alpaca-clerk-sqlite/accounts/${encodeURIComponent(accountId)}/recovery-actions/execute`,
        {
          action_id: action.action_id,
          concurrency_token: action.concurrency_token,
          execution_ref: action.execution_ref,
        },
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
        `${this.accountsBase}/${encodeURIComponent(accountId)}/transactions`,
        { params },
      ),
    );
  }

  accountTransaction(
    accountId: string,
    transactionId: string,
  ): Promise<ClerkTransactionDetail> {
    return firstValueFrom(
      this.http.get<ClerkTransactionDetail>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/transactions/${encodeURIComponent(transactionId)}`,
      ),
    );
  }

  acknowledgeExternalOrder(
    accountId: string,
    externalOrderId: string,
    operator: string,
  ): Promise<ExternalOrderAcknowledgement> {
    return firstValueFrom(
      this.http.post<ExternalOrderAcknowledgement>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/transactions/external-orders/${encodeURIComponent(externalOrderId)}/acknowledge`,
        { operator },
      ),
    );
  }
}
