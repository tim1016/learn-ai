import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type { RunLedger } from './strategy-runs.types';
import type {
  WalkForwardListFilters,
  WalkForwardListResponse,
  WalkForwardRequest,
  WalkForwardResponse,
} from './walk-forward.types';

/**
 * HTTP client for `/api/research/strategy-runs/walk-forward`. Same
 * pattern as `StrategyRunsService` — direct FastAPI calls; no GraphQL
 * passthrough yet.
 *
 * `runFromRun` is the ergonomic entry point used by the run-detail
 * page's "Run rolling walk-forward" button: takes an existing run's
 * ledger, copies its spec + window + cost model, and submits a rolling
 * 60/30/30 split with the run as the WF's parent. Custom split
 * policies / windows are a future spec-form addition; v1 ships the
 * default to keep the surface small.
 */
@Injectable({ providedIn: 'root' })
export class WalkForwardService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/strategy-runs/walk-forward`;

  /** `POST /api/research/strategy-runs/walk-forward` */
  async createWalkForward(request: WalkForwardRequest): Promise<WalkForwardResponse> {
    return firstValueFrom(this.http.post<WalkForwardResponse>(this.base, request));
  }

  /** `GET /api/research/strategy-runs/walk-forward/{wf_id}` */
  async getWalkForward(wfId: string): Promise<WalkForwardResponse> {
    return firstValueFrom(
      this.http.get<WalkForwardResponse>(`${this.base}/${encodeURIComponent(wfId)}`),
    );
  }

  /**
   * `GET /api/research/strategy-runs/walk-forward?…` — listing endpoint.
   * Filters are AND-combined; results sort newest-first.
   */
  async listWalkForwards(
    filters: WalkForwardListFilters = {},
  ): Promise<WalkForwardListResponse> {
    let params = new HttpParams();
    if (filters.parent_run_id) {
      params = params.set('parent_run_id', filters.parent_run_id);
    }
    if (filters.spec_hash) params = params.set('spec_hash', filters.spec_hash);
    if (filters.since_ms !== undefined) {
      params = params.set('since_ms', String(filters.since_ms));
    }
    if (filters.limit !== undefined) params = params.set('limit', String(filters.limit));

    return firstValueFrom(
      this.http.get<WalkForwardListResponse>(this.base, { params }),
    );
  }

  /**
   * Convenience: derive a rolling 60/30/30 walk-forward from an
   * existing run's spec + window + cost model. The created WF's
   * ``parent_run_id`` points back at this run so future listings can
   * filter by it. Window defaults to the run's own bounds; backend
   * raises if the window is too short for the split (the runner
   * surfaces this as a failed-status WF that's still persisted).
   *
   * Why hard-code rolling 60/30/30 in v1: it's the most common shape
   * for "did this strategy hold up across the year?" and matches the
   * SPY EMA acceptance fixture's natural slicing. A custom-policy
   * form is a separate component when needed.
   */
  async runFromRun(run: RunLedger): Promise<WalkForwardResponse> {
    return this.createWalkForward({
      spec: run.strategy_spec_json,
      start_date: msToDateString(run.start_ms),
      end_date: msToDateString(run.end_ms),
      split_policy: { kind: 'rolling', train_days: 60, test_days: 30, step_days: 30 },
      initial_cash: run.initial_cash,
      fill_mode: run.fill_mode,
      commission_per_order: run.commission_per_order,
      slippage_per_share: run.slippage_per_share,
      random_seed: run.random_seed,
      parent_run_id: run.run_id,
    });
  }
}

/**
 * Convert `int64 ms UTC` (NY-midnight anchored) to `YYYY-MM-DD`.
 * The backend's `_date_str_to_ny_midnight_ms` re-parses this in NY
 * timezone, so producing a UTC date here is fine — the run's stored
 * `start_ms` is NY-midnight UTC, and `Date.toISOString().slice(0,10)`
 * extracts the UTC date which matches.
 */
function msToDateString(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}
