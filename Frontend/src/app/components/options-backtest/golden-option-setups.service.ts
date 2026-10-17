import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

export type GoldenOptionSetup = components['schemas']['GoldenOptionSetupView'];

const BASE = '/api/research/golden-option-setups';

@Injectable({ providedIn: 'root' })
export class GoldenOptionSetupsService {
  private readonly http = inject(HttpClient);

  list(): Promise<GoldenOptionSetup[]> {
    return firstValueFrom(this.http.get<GoldenOptionSetup[]>(BASE));
  }

  approve(runId: number, note: string, idempotencyKey: string): Promise<GoldenOptionSetup> {
    return firstValueFrom(this.http.post<GoldenOptionSetup>(`${BASE}/approve`, {
      run_id: runId, note, idempotency_key: idempotencyKey,
    }));
  }

  revoke(id: string, reason: string, idempotencyKey: string): Promise<GoldenOptionSetup> {
    return firstValueFrom(this.http.post<GoldenOptionSetup>(`${BASE}/${encodeURIComponent(id)}/revoke`, {
      reason, idempotency_key: idempotencyKey,
    }));
  }

  reprove(id: string, idempotencyKey: string): Promise<GoldenOptionSetup> {
    return firstValueFrom(this.http.post<GoldenOptionSetup>(`${BASE}/${encodeURIComponent(id)}/reprove`, {
      idempotency_key: idempotencyKey,
    }));
  }
}
