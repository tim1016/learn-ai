import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../environments/environment';
import type { ExhaustiveRunResponse } from './exhaustive-run.types';

/** Read client for persisted Python-owned Exhaustive Run evidence. */
@Injectable({ providedIn: 'root' })
export class ExhaustiveRunService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/exhaustive-runs`;

  async getLatestForWalkForward(walkForwardId: string): Promise<ExhaustiveRunResponse | null> {
    try {
      return await firstValueFrom(
        this.http.get<ExhaustiveRunResponse>(
          `${this.base}/by-walk-forward/${encodeURIComponent(walkForwardId)}`,
        ),
      );
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 404) return null;
      throw error;
    }
  }
}
