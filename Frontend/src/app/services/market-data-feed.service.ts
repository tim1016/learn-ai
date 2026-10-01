import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  ExpirationsResponse,
  IbkrConnectionHealth,
  IbkrStrikeList,
} from '../api/broker-models';

/**
 * REST client for the retained ``/api/broker`` market-data feed surface:
 * connection health and the option-chain market-data reads
 * (``expirations`` / ``strikes``).
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

}
