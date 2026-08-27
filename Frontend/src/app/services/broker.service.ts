import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  DataPlaneHealth,
  ExpirationsResponse,
  BrokerCapabilityResponse,
  IbkrApiEvidenceEvent,
  IbkrConnectionHealth,
  IbkrStrikeList,
  OptionContractsResponse,
} from '../api/broker-models';

/**
 * REST client for the retained ``/api/broker`` market-data feed surface:
 * feed session lifecycle (``connect`` / ``disconnect`` / ``reconnect``),
 * connection and data-plane health, capability probes, the option-chain
 * market-data reads (``expirations`` / ``strikes`` /
 * ``searchOptionContracts``), and the ``ibkrApiEvidence`` audit read.
 *
 * SSE endpoints (option-chain, option-surface) do **not** route through
 * this service — use the ``brokerSse()`` helper in ``broker-sse.ts`` so
 * each component owns the EventSource lifetime explicitly.
 */
@Injectable({ providedIn: 'root' })
export class BrokerService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/broker';

  health(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(this.http.get<IbkrConnectionHealth>(`${this.base}/health`));
  }

  dataPlaneHealth(): Promise<DataPlaneHealth> {
    return firstValueFrom(
      this.http.get<DataPlaneHealth>(`${this.base}/data-plane/health`),
    );
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
