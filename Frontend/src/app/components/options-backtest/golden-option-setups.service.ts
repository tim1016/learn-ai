import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

export type GoldenOptionSetup = components['schemas']['GoldenOptionSetupView'];

const BASE = '/api/research/golden-option-setups';

@Injectable({ providedIn: 'root' })
export class GoldenOptionSetupsService {
  private readonly http = inject(HttpClient);

  list(sourceRunId?: number): Promise<GoldenOptionSetup[]> {
    return firstValueFrom(this.http.get<GoldenOptionSetup[]>(BASE,
      sourceRunId === undefined ? {} : { params: { source_run_id: sourceRunId } }));
  }

  get(id: string): Promise<GoldenOptionSetup> {
    return firstValueFrom(this.http.get<GoldenOptionSetup>(`${BASE}/${encodeURIComponent(id)}`));
  }

  /**
   * Approves one completed run. `expectedSetupSha256` is the hash of the content the owner reviewed, and every
   * approval carries one: the server refuses the approval, and approves nothing, when the run's content is any other.
   */
  approve(runId: number, note: string, idempotencyKey: string, expectedSetupSha256: string): Promise<GoldenOptionSetup> {
    return firstValueFrom(this.http.post<GoldenOptionSetup>(`${BASE}/approve`, {
      run_id: runId, note, idempotency_key: idempotencyKey, expected_setup_sha256: expectedSetupSha256,
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
