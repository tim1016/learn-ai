import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  ExpirationsResponse,
  IbkrConnectionHealth,
  IbkrStrikeList,
  OptionContractsResponse,
} from '../api/broker-models';

/**
 * REST client for the retained ``/api/broker`` market-data feed surface:
 * feed session lifecycle (``connect`` / ``disconnect`` / ``reconnect``),
 * connection health, and the option-chain market-data reads
 * (``expirations`` / ``strikes`` / ``searchOptionContracts``).
 *
 * SSE endpoints (option-chain, option-surface) do **not** route through
 * this service — use the ``brokerSse()`` helper in ``broker-sse.ts`` so
 * each component owns the EventSource lifetime explicitly.
 */
@Injectable({ providedIn: 'root' })
export class MarketDataFeedService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/broker';

  health(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(this.http.get<IbkrConnectionHealth>(`${this.base}/health`));
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
