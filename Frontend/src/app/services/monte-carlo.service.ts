import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type { RunLedger } from './strategy-runs.types';
import type {
  MonteCarloListFilters,
  MonteCarloListResponse,
  MonteCarloRequest,
  MonteCarloResponse,
} from './monte-carlo.types';

/**
 * HTTP client for `/api/research/strategy-runs/monte-carlo`. Same
 * pattern as `WalkForwardService` — direct FastAPI calls, no GraphQL
 * passthrough yet.
 *
 * `runReshuffleFromRun` is the ergonomic entry point for the
 * run-detail page's "Run reshuffle Monte Carlo" button: fixes
 * `method='reshuffle'` and `simulation_count=1000` (the architecture
 * spec's recommended default), leaves seed at 0 for deterministic
 * reproducibility, and supplies the standard set of breach
 * thresholds the workbench shows in the detail-page table.
 */
@Injectable({ providedIn: 'root' })
export class MonteCarloService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/strategy-runs/monte-carlo`;

  /** `POST /api/research/strategy-runs/monte-carlo` */
  async createMonteCarlo(request: MonteCarloRequest): Promise<MonteCarloResponse> {
    return firstValueFrom(this.http.post<MonteCarloResponse>(this.base, request));
  }

  /** `GET /api/research/strategy-runs/monte-carlo/{mc_id}` */
  async getMonteCarlo(mcId: string): Promise<MonteCarloResponse> {
    return firstValueFrom(
      this.http.get<MonteCarloResponse>(`${this.base}/${encodeURIComponent(mcId)}`),
    );
  }

  /** `GET /api/research/strategy-runs/monte-carlo?…` */
  async listMonteCarlos(
    filters: MonteCarloListFilters = {},
  ): Promise<MonteCarloListResponse> {
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
      this.http.get<MonteCarloListResponse>(this.base, { params }),
    );
  }

  /**
   * Convenience: derive a 1000-simulation reshuffle MC from an
   * existing run. Reshuffle is the default because it's the cleanest
   * test of "does the order of trades matter?" without the IID
   * assumption that resample requires.
   *
   * Default breach thresholds {5%, 10%, 20%, 30%} match the workbench's
   * detail-page table — the user can re-run with custom thresholds
   * once a custom-form UI lands.
   */
  async runReshuffleFromRun(run: RunLedger): Promise<MonteCarloResponse> {
    return this.createMonteCarlo({
      parent_run_id: run.run_id,
      method: 'reshuffle',
      simulation_count: 1000,
      random_seed: 0,
      breach_thresholds: [0.05, 0.10, 0.20, 0.30],
    });
  }
}
