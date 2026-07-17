import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  AccountAcceptExposureOverrideRequest,
  AccountAcceptExposureOverrideResponse,
  AccountEmergencyFlattenResponse,
  AccountClearFreezeRequest,
  AccountClearFreezeResponse,
  JournalCurePreview,
  JournalCureReceipt,
  JournalCureRequest,
  OperatorRecoveryFlattenRequest,
  OperatorRecoveryFlattenResponse,
  LegacyStaleClaimCandidatesResponse,
  LegacyStaleClaimRetireRequest,
  LegacyStaleClaimRetirementReceipt,
  AccountReconciliationAutomationPolicy,
  AccountReconciliationAutomationPolicyUpdate,
  AccountReconciliationReceipt,
  AccountTriageResponse,
} from '../api/account-reconciliation.types';
import type { AccountsRosterResponse, AccountServiceStatusResponse } from '../api/account-directory.types';
import type { AccountEventsRequest, AccountEventsResponse } from '../api/account-events.types';
import type {
  AccountTruthResponse,
  DataPlaneHealth,
  DiagnosticReport,
  ExpirationsResponse,
  BrokerCapabilityResponse,
  IbkrApiEvidenceEvent,
  IbkrAccountSummary,
  IbkrConnectionHealth,
  IbkrOpenOrder,
  IbkrOrderAck,
  IbkrOrderSpec,
  IbkrOrderWhatIfPreview,
  IbkrPositionsSnapshot,
  IbkrStrikeList,
  OptionContractsResponse,
  SymbolSearchResponse,
} from '../api/broker-models';

/**
 * REST client for the Phase 1-3 IBKR broker endpoints.
 *
 * SSE endpoints (option-chain, pnl/stream, pnl/positions/stream,
 * orders/stream) do **not** route through this service — use the
 * ``brokerSse()`` helper in ``broker-sse.ts`` so each component owns
 * the EventSource lifetime explicitly.
 */
export type SymbolSearchSecType =
  | 'STK'
  | 'OPT'
  | 'FUT'
  | 'FOP'
  | 'IND'
  | 'CASH'
  | 'BOND'
  | 'CFD'
  | 'CMDTY';

@Injectable({ providedIn: 'root' })
export class BrokerService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/broker';
  private readonly accountsBase = '/api/accounts';

  health(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(this.http.get<IbkrConnectionHealth>(`${this.base}/health`));
  }

  dataPlaneHealth(): Promise<DataPlaneHealth> {
    return firstValueFrom(
      this.http.get<DataPlaneHealth>(`${this.base}/data-plane/health`),
    );
  }

  diagnose(): Promise<DiagnosticReport> {
    return firstValueFrom(this.http.get<DiagnosticReport>(`${this.base}/diagnose`));
  }

  ibkrApiEvidence(afterSeq = 0, limit = 250): Promise<IbkrApiEvidenceEvent[]> {
    return firstValueFrom(
      this.http.get<IbkrApiEvidenceEvent[]>(`${this.base}/ibkr/evidence`, {
        params: { after_seq: afterSeq, limit },
      }),
    );
  }

  connect(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(
      this.http.post<IbkrConnectionHealth>(`${this.base}/connect`, {}),
    );
  }

  disconnect(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(
      this.http.post<IbkrConnectionHealth>(`${this.base}/disconnect`, {}),
    );
  }

  reconnect(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(
      this.http.post<IbkrConnectionHealth>(`${this.base}/reconnect`, {}),
    );
  }

  capability(): Promise<BrokerCapabilityResponse> {
    return firstValueFrom(
      this.http.get<BrokerCapabilityResponse>(`${this.base}/capability`),
    );
  }

  probeCapability(symbols: string[] = ['SPY', 'QQQ']): Promise<BrokerCapabilityResponse> {
    return firstValueFrom(
      this.http.post<BrokerCapabilityResponse>(
        `${this.base}/capability/probe`,
        {},
        { params: { symbols: symbols.join(',') } },
      ),
    );
  }

  account(): Promise<IbkrAccountSummary> {
    return firstValueFrom(this.http.get<IbkrAccountSummary>(`${this.base}/account`));
  }

  positions(): Promise<IbkrPositionsSnapshot> {
    return firstValueFrom(this.http.get<IbkrPositionsSnapshot>(`${this.base}/positions`));
  }

  accountTruth(): Promise<AccountTruthResponse> {
    return firstValueFrom(
      this.http.get<AccountTruthResponse>(`${this.base}/account-truth`),
    );
  }

  reconcileAccount(accountId: string): Promise<AccountReconciliationReceipt> {
    return firstValueFrom(
      this.http.post<AccountReconciliationReceipt>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/reconciliation`,
        {},
      ),
    );
  }

  latestAccountReconciliation(accountId: string): Promise<AccountReconciliationReceipt> {
    return firstValueFrom(
      this.http.get<AccountReconciliationReceipt>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/reconciliation/latest`,
      ),
    );
  }

  updateAccountReconciliationAutomation(
    accountId: string,
    payload: AccountReconciliationAutomationPolicyUpdate,
  ): Promise<AccountReconciliationAutomationPolicy> {
    return firstValueFrom(
      this.http.put<AccountReconciliationAutomationPolicy>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/reconciliation/automation`,
        payload,
      ),
    );
  }

  accountTriage(accountId: string): Promise<AccountTriageResponse> {
    return firstValueFrom(
      this.http.get<AccountTriageResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/triage`,
      ),
    );
  }

  accounts(): Promise<AccountsRosterResponse> {
    return firstValueFrom(this.http.get<AccountsRosterResponse>(this.accountsBase));
  }

  accountServiceStatus(accountId: string): Promise<AccountServiceStatusResponse> {
    return firstValueFrom(
      this.http.get<AccountServiceStatusResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/clerk`,
      ),
    );
  }

  accountEvents(accountId: string, request: AccountEventsRequest): Promise<AccountEventsResponse> {
    const params: Record<string, string | number | readonly (string | number | boolean)[]> = {
      view: request.view,
      limit: request.limit ?? 50,
    };
    if (request.kinds?.length) params['kinds'] = request.kinds;
    if (request.beforeSeq !== undefined) params['before_seq'] = request.beforeSeq;
    if (request.afterSeq !== undefined) params['after_seq'] = request.afterSeq;
    return firstValueFrom(
      this.http.get<AccountEventsResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/events`,
        { params },
      ),
    );
  }

  legacyStaleClaimCandidates(accountId: string): Promise<LegacyStaleClaimCandidatesResponse> {
    return firstValueFrom(
      this.http.get<LegacyStaleClaimCandidatesResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/legacy-stale-claims/candidates`,
      ),
    );
  }

  retireLegacyStaleClaim(
    accountId: string,
    payload: LegacyStaleClaimRetireRequest,
  ): Promise<LegacyStaleClaimRetirementReceipt> {
    return firstValueFrom(
      this.http.post<LegacyStaleClaimRetirementReceipt>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/legacy-stale-claims/retire`,
        payload,
      ),
    );
  }

  previewJournalCure(
    accountId: string,
    botOrderNamespace: string,
    symbol: string,
  ): Promise<JournalCurePreview> {
    return firstValueFrom(
      this.http.get<JournalCurePreview>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/journal-cures/preview`,
        { params: { bot_order_namespace: botOrderNamespace, symbol } },
      ),
    );
  }

  applyJournalCure(accountId: string, payload: JournalCureRequest): Promise<JournalCureReceipt> {
    return firstValueFrom(
      this.http.post<JournalCureReceipt>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/journal-cures`,
        payload,
      ),
    );
  }

  submitOperatorRecoveryFlatten(
    accountId: string,
    payload: OperatorRecoveryFlattenRequest,
  ): Promise<OperatorRecoveryFlattenResponse> {
    return firstValueFrom(
      this.http.post<OperatorRecoveryFlattenResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/operator-recovery-flatten`,
        payload,
      ),
    );
  }

  emergencyFlattenAccount(accountId: string): Promise<AccountEmergencyFlattenResponse> {
    return firstValueFrom(
      this.http.post<AccountEmergencyFlattenResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/emergency-flatten`,
        { account: accountId, confirm: true },
      ),
    );
  }

  clearAccountFreeze(
    accountId: string,
    payload: AccountClearFreezeRequest = {},
  ): Promise<AccountClearFreezeResponse> {
    return firstValueFrom(
      this.http.post<AccountClearFreezeResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/freeze/clear`,
        payload,
      ),
    );
  }

  acceptExposureOverride(
    accountId: string,
    payload: AccountAcceptExposureOverrideRequest,
  ): Promise<AccountAcceptExposureOverrideResponse> {
    return firstValueFrom(
      this.http.post<AccountAcceptExposureOverrideResponse>(
        `${this.accountsBase}/${encodeURIComponent(accountId)}/freeze/accept-exposure-override`,
        payload,
      ),
    );
  }

  expirations(symbol: string): Promise<ExpirationsResponse> {
    return firstValueFrom(
      this.http.get<ExpirationsResponse>(`${this.base}/expirations/${symbol}`),
    );
  }

  strikes(symbol: string, expiryMs: number): Promise<IbkrStrikeList> {
    return firstValueFrom(
      this.http.get<IbkrStrikeList>(
        `${this.base}/strikes/${encodeURIComponent(symbol)}`,
        { params: { expiry_ms: expiryMs } },
      ),
    );
  }

  /**
   * Slice 1F — proxy to IBKR ``reqMatchingSymbols``. Returns matching
   * contracts for the typed pattern; the cockpit's leg picker debounces
   * before calling so a single keystroke does not draw an IBKR token.
   */
  searchSymbols(q: string, secType?: SymbolSearchSecType): Promise<SymbolSearchResponse> {
    const params: Record<string, string> = { q };
    if (secType !== undefined) params['sec_type'] = secType;
    return firstValueFrom(
      this.http.get<SymbolSearchResponse>(`${this.base}/symbols/search`, { params }),
    );
  }

  /**
   * Slice 1F — proxy to IBKR ``reqContractDetails``. Qualifies a
   * drill-down (symbol, expiry, strike, right) pick and returns
   * ``con_id`` + ``local_symbol`` + multiplier for persistence with the
   * declared option leg.
   */
  searchOptionContracts(
    symbol: string,
    expiryMs: number,
    strike: number,
    right: 'C' | 'P',
  ): Promise<OptionContractsResponse> {
    return firstValueFrom(
      this.http.get<OptionContractsResponse>(
        `${this.base}/option-contracts/${encodeURIComponent(symbol)}`,
        { params: { expiry_ms: expiryMs, strike, right } },
      ),
    );
  }

  openOrders(): Promise<IbkrOpenOrder[]> {
    return firstValueFrom(this.http.get<IbkrOpenOrder[]>(`${this.base}/orders/open`));
  }

  completedOrders(): Promise<IbkrOpenOrder[]> {
    return firstValueFrom(
      this.http.get<IbkrOpenOrder[]>(`${this.base}/orders/completed`),
    );
  }

  orderWhatIf(spec: IbkrOrderSpec): Promise<IbkrOrderWhatIfPreview> {
    return firstValueFrom(
      this.http.post<IbkrOrderWhatIfPreview>(`${this.base}/orders/what-if`, spec),
    );
  }

  placeOrder(spec: IbkrOrderSpec): Promise<IbkrOrderAck> {
    return firstValueFrom(this.http.post<IbkrOrderAck>(`${this.base}/orders`, spec));
  }

  cancelOrder(orderId: number): Promise<IbkrOpenOrder> {
    return firstValueFrom(
      this.http.delete<IbkrOpenOrder>(`${this.base}/orders/${orderId}`),
    );
  }
}
