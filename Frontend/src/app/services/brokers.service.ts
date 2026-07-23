import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  BrokerAccountSnapshot,
  BrokerOrder,
  BrokerOrderRequest,
  BrokerPosition,
  OrderCancelResult,
  OrderSubmitResult,
} from '../api/alpaca.types';

/**
 * Broker System v2 read client — targets `/api/brokers/{broker}/...`, the v2
 * data-plane surface (separate from the v1 `/api/broker/...` family). Phase 1
 * is read-only and Alpaca-only; the broker id is a parameter so later brokers
 * reuse the same client unchanged.
 */
@Injectable({ providedIn: 'root' })
export class BrokersService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/brokers';

  getAccount(broker = 'alpaca'): Promise<BrokerAccountSnapshot> {
    return firstValueFrom(
      this.http.get<BrokerAccountSnapshot>(`${this.base}/${broker}/account`),
    );
  }

  listPositions(broker = 'alpaca'): Promise<BrokerPosition[]> {
    return firstValueFrom(
      this.http.get<BrokerPosition[]>(`${this.base}/${broker}/positions`),
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

  /**
   * Phase-2 S1 — submit one or more equity market legs (write path). This is a
   * control mutation: `/api/brokers` is a registered control prefix
   * (`contracts/data-plane-control-surfaces.json`), so the data-plane control
   * intent interceptor marks the POST and the dev proxy attaches the shared
   * secret. No per-call marking is needed here beyond hitting that prefix.
   */
  submitOrder(
    broker: string,
    request: BrokerOrderRequest,
  ): Promise<OrderSubmitResult> {
    return firstValueFrom(
      this.http.post<OrderSubmitResult>(`${this.base}/${broker}/orders`, request),
    );
  }

  /**
   * Phase-2 S3 — cancel one working order by its broker-assigned id (write
   * path). Like {@link submitOrder}, this is a control mutation: DELETE to the
   * registered `/api/brokers` control prefix, so the data-plane control intent
   * interceptor marks it and the dev proxy attaches the shared secret.
   * `orderId` is the opaque broker id, passed through verbatim.
   */
  cancelOrder(broker: string, orderId: string): Promise<OrderCancelResult> {
    return firstValueFrom(
      this.http.delete<OrderCancelResult>(
        `${this.base}/${broker}/orders/${encodeURIComponent(orderId)}`,
      ),
    );
  }
}
