import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  BrokerSessionEventPage,
  BrokerSessionMirrorSnapshot,
} from '../api/broker-session-mirror.types';

@Injectable({ providedIn: 'root' })
export class BrokerSessionMirrorService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/broker/session-mirror';

  snapshot(): Promise<BrokerSessionMirrorSnapshot> {
    return firstValueFrom(this.http.get<BrokerSessionMirrorSnapshot>(this.base));
  }

  events(params: {
    client_id?: number | null;
    after_seq?: number;
    limit?: number;
  } = {}): Promise<BrokerSessionEventPage> {
    const query: Record<string, number> = {};
    if (params.client_id !== undefined && params.client_id !== null) {
      query['client_id'] = params.client_id;
    }
    if (params.after_seq !== undefined) query['after_seq'] = params.after_seq;
    if (params.limit !== undefined) query['limit'] = params.limit;
    return firstValueFrom(
      this.http.get<BrokerSessionEventPage>(`${this.base}/events`, {
        params: query,
      }),
    );
  }
}
