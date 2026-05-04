import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  DiagnosticReport,
  ExpirationsResponse,
  IbkrAccountSummary,
  IbkrConnectionHealth,
  IbkrOpenOrder,
  IbkrOrderAck,
  IbkrOrderSpec,
  IbkrPositionsSnapshot,
  IbkrStrikeList,
} from '../api/broker-models';

/**
 * REST client for the Phase 1-3 IBKR broker endpoints.
 *
 * SSE endpoints (option-chain, pnl/stream, pnl/positions/stream,
 * orders/stream) do **not** route through this service — use the
 * ``brokerSse()`` helper in ``broker-sse.ts`` so each component owns
 * the EventSource lifetime explicitly.
 */
@Injectable({ providedIn: 'root' })
export class BrokerService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/broker';

  health(): Promise<IbkrConnectionHealth> {
    return firstValueFrom(this.http.get<IbkrConnectionHealth>(`${this.base}/health`));
  }

  diagnose(): Promise<DiagnosticReport> {
    return firstValueFrom(this.http.get<DiagnosticReport>(`${this.base}/diagnose`));
  }

  account(): Promise<IbkrAccountSummary> {
    return firstValueFrom(this.http.get<IbkrAccountSummary>(`${this.base}/account`));
  }

  positions(): Promise<IbkrPositionsSnapshot> {
    return firstValueFrom(this.http.get<IbkrPositionsSnapshot>(`${this.base}/positions`));
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

  openOrders(): Promise<IbkrOpenOrder[]> {
    return firstValueFrom(this.http.get<IbkrOpenOrder[]>(`${this.base}/orders/open`));
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
