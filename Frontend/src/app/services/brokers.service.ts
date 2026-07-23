import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  BrokerAccountSnapshot,
  BrokerOrder,
  BrokerPosition,
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
}
