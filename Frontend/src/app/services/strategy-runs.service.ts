import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  StrategyRunListFilters,
  StrategyRunListResponse,
  StrategyRunRequest,
  StrategyRunResponse,
} from './strategy-runs.types';

/**
 * HTTP client for `/api/research/strategy-runs`. Mirrors the FastAPI
 * router exactly — three endpoints, no metric computation, no
 * client-side caching. Cache lives in the calling component's
 * `signal()` so each route refresh hits the server.
 *
 * Following the same pattern as `lean-engine` (no GraphQL passthrough
 * yet; Phase B consumes FastAPI directly). When/if the spec layer adds
 * GraphQL, the consumer signature here doesn't change — only the
 * underlying URL.
 */
@Injectable({ providedIn: 'root' })
export class StrategyRunsService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api/research/strategy-runs`;

  /**
   * `POST /api/research/strategy-runs` — kick off a run, persist, and
   * return the resulting `(ledger, result)` pair. Synchronous: blocks
   * until the engine finishes. For 15-min SPY EMA backtests over a
   * year of data this is sub-second; longer windows may need an SSE/
   * job-queue path (deferred to a later phase).
   */
  async createRun(request: StrategyRunRequest): Promise<StrategyRunResponse> {
    return firstValueFrom(this.http.post<StrategyRunResponse>(this.base, request));
  }

  /** `GET /api/research/strategy-runs/{run_id}` */
  async getRun(runId: string): Promise<StrategyRunResponse> {
    return firstValueFrom(
      this.http.get<StrategyRunResponse>(`${this.base}/${encodeURIComponent(runId)}`),
    );
  }

  /**
   * `GET /api/research/strategy-runs?…` — listing endpoint. Filters
   * are AND-combined server-side; results sort newest-first.
   */
  async listRuns(filters: StrategyRunListFilters = {}): Promise<StrategyRunListResponse> {
    let params = new HttpParams();
    if (filters.spec_hash) params = params.set('spec_hash', filters.spec_hash);
    if (filters.symbol) params = params.set('symbol', filters.symbol);
    if (filters.status) params = params.set('status', filters.status);
    if (filters.parent_run_id) params = params.set('parent_run_id', filters.parent_run_id);
    if (filters.parent_spec_hash) {
      params = params.set('parent_spec_hash', filters.parent_spec_hash);
    }
    if (filters.since_ms !== undefined) {
      params = params.set('since_ms', String(filters.since_ms));
    }
    if (filters.limit !== undefined) params = params.set('limit', String(filters.limit));

    return firstValueFrom(
      this.http.get<StrategyRunListResponse>(this.base, { params }),
    );
  }

  /**
   * Convenience: GET the canonical SPY EMA fixture spec from the
   * spec-strategy fixtures endpoint and submit a run against it.
   *
   * The list page exposes a "Run SPY EMA fixture" button so the
   * workbench is usable without curl — the fixture is the same
   * `StrategySpec` JSON the Phase A acceptance gate runs against, so
   * a click here exercises the full reproducibility contract end-to-end.
   *
   * Date window matches the Phase A acceptance test (`2024-01-02` →
   * `2024-12-31`); the engine clamps to whatever LEAN data is
   * available.
   */
  async runSpyEmaFixture(): Promise<StrategyRunResponse> {
    const fixtureUrl = `${environment.pythonServiceUrl}/api/spec-strategy/fixtures/spy_ema_crossover`;
    const spec = await firstValueFrom(this.http.get<unknown>(fixtureUrl));
    return this.createRun({
      spec,
      start_date: '2024-01-02',
      end_date: '2024-12-31',
      initial_cash: 100_000,
      fill_mode: 'signal_bar_close',
      commission_per_order: 0,
      strategy_spec_id: 'spy_ema_crossover',
    });
  }
}
