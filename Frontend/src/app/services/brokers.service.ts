import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { BrokerAccountSnapshot } from '../api/alpaca.types';

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
}
