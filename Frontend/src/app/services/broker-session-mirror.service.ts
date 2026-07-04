import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { BrokerSessionMirrorSnapshot } from '../api/broker-session-mirror.types';

@Injectable({ providedIn: 'root' })
export class BrokerSessionMirrorService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/broker/session-mirror';

  snapshot(): Promise<BrokerSessionMirrorSnapshot> {
    return firstValueFrom(this.http.get<BrokerSessionMirrorSnapshot>(this.base));
  }
}
