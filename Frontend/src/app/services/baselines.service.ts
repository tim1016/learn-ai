import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  BaselineListFilters,
  BaselineListResponse,
  BaselineMethod,
  BaselineRequest,
  BaselineResponse,
} from './baselines.types';
import type { RunLedger } from './strategy-runs.types';

/**
 * HTTP client for `/api/research/strategy-runs/baselines`. Same
 * pattern as `WalkForwardService` / `MonteCarloService` — direct
 * FastAPI calls, no GraphQL passthrough yet.
 *
 * `runFromRun(run, method)` is the ergonomic entry point used by
 * the run-detail page's two buttons. The default sample counts
 * follow the architecture spec's recommendations: 1 for
 * `buy_and_hold` (parameter-less, deterministic) and 30 for
 * `random_ema_windows` (the smallest count that gives a stable
 * null distribution).
 */
@Injectable({ providedIn: 'root' })
export class BaselinesService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/strategy-runs/baselines`;

  /** `POST /api/research/strategy-runs/baselines` */
  async createBaseline(request: BaselineRequest): Promise<BaselineResponse> {
    return firstValueFrom(this.http.post<BaselineResponse>(this.base, request));
  }

  /** `GET /api/research/strategy-runs/baselines/{baseline_id}` */
  async getBaseline(baselineId: string): Promise<BaselineResponse> {
    return firstValueFrom(
      this.http.get<BaselineResponse>(`${this.base}/${encodeURIComponent(baselineId)}`),
    );
  }

  /** `GET /api/research/strategy-runs/baselines?…` */
  async listBaselines(
    filters: BaselineListFilters = {},
  ): Promise<BaselineListResponse> {
    let params = new HttpParams();
    if (filters.parent_run_id) {
      params = params.set('parent_run_id', filters.parent_run_id);
    }
    if (filters.method) params = params.set('method', filters.method);
    if (filters.since_ms !== undefined) {
      params = params.set('since_ms', String(filters.since_ms));
    }
    if (filters.limit !== undefined) params = params.set('limit', String(filters.limit));

    return firstValueFrom(
      this.http.get<BaselineListResponse>(this.base, { params }),
    );
  }

  /**
   * Convenience: derive a baseline from a run.
   *
   * - `buy_and_hold` defaults to `sample_count=1` (parameter-less,
   *   deterministic; >1 only for engine-determinism sanity-checking)
   * - `random_ema_windows` defaults to `sample_count=30` and the
   *   architecture-spec-recommended ranges `fast ∈ [3, 12]`,
   *   `slow ∈ [10, 30]`. Custom ranges and counts are deferred to a
   *   future spec-form component.
   */
  async runFromRun(
    run: RunLedger,
    method: BaselineMethod,
  ): Promise<BaselineResponse> {
    if (method === 'buy_and_hold') {
      return this.createBaseline({
        parent_run_id: run.run_id,
        method: 'buy_and_hold',
        sample_count: 1,
        random_seed: 0,
      });
    }
    return this.createBaseline({
      parent_run_id: run.run_id,
      method: 'random_ema_windows',
      sample_count: 30,
      random_seed: 0,
      fast_range: [3, 12],
      slow_range: [10, 30],
    });
  }
}
