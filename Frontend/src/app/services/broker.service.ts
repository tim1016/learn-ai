import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  ClerkTransactionDetail,
  ClerkTransactionFilters,
  ClerkTransactionHistoryResponse,
  ExternalOrderAcknowledgement,
} from '../api/clerk-transaction-history.types';
import type {
  DataPlaneHealth,
  DiagnosticReport,
  ExpirationsResponse,
  BrokerCapabilityResponse,
  IbkrApiEvidenceEvent,
  IbkrConnectionHealth,
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

  accountTransactions(
    accountId: string,
    cursor: string | null = null,
    limit = 50,
    filters: ClerkTransactionFilters = {},
  ): Promise<ClerkTransactionHistoryResponse> {
    const params: Record<string, string | number> = { limit };
    if (filters.broker) params['broker'] = filters.broker;
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

}
