import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import { JobsService } from '../../services/jobs.service';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import type {
  CellPageQuery,
  GridSearchCellPage,
  GridSearchDetail,
  GridSearchHistoryFilters,
  GridSearchPreflight,
  GridSearchRefusal,
  GridSearchSpecRequest,
  GridSearchSummary,
} from './grid-search.types';

/** A preflight or launch the server refused for a reason the researcher can act on. */
export class GridSearchRefusedError extends Error {
  constructor(readonly refusal: GridSearchRefusal) {
    super(refusal.message);
  }
}

function toRefusal(error: unknown): GridSearchRefusedError | null {
  if (!(error instanceof HttpErrorResponse) || error.status !== 400) return null;
  const detail: unknown = error.error?.detail;
  if (detail && typeof detail === 'object' && 'code' in detail && 'message' in detail) {
    return new GridSearchRefusedError(detail as GridSearchRefusal);
  }
  return null;
}

/**
 * HTTP client for `/api/research/grid-search` (direct FastAPI, like the other
 * research surfaces) plus the launch and Finish verbs, which go through the
 * jobs boundary so progress and cancellation ride the shared job stream.
 */
@Injectable({ providedIn: 'root' })
export class GridSearchService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);
  private readonly base = `${environment.pythonServiceUrl}/api/research/grid-search`;

  async loadStrategies(): Promise<StrategyInfo[]> {
    return firstValueFrom(this.http.get<StrategyInfo[]>(`${environment.pythonServiceUrl}/api/engine/strategies`));
  }

  async preflight(spec: GridSearchSpecRequest): Promise<GridSearchPreflight> {
    try {
      return await firstValueFrom(this.http.post<GridSearchPreflight>(`${this.base}/preflight`, spec));
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** Starts a `grid_search` job; resolves to the job id once Python has made the record durable. */
  async launch(spec: GridSearchSpecRequest): Promise<string> {
    try {
      return await this.jobs.startJob('grid_search', { ...spec });
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** Re-runs only the missing cells of an incomplete search under a new job. */
  async finish(detail: GridSearchDetail): Promise<string> {
    return this.jobs.startJob('grid_search', { ...detail.request, resume_search_id: detail.id });
  }

  async list(filters: GridSearchHistoryFilters = {}): Promise<GridSearchSummary[]> {
    let params = new HttpParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value) params = params.set(key, String(value));
    }
    return firstValueFrom(this.http.get<GridSearchSummary[]>(this.base, { params }));
  }

  async get(id: string): Promise<GridSearchDetail> {
    return firstValueFrom(this.http.get<GridSearchDetail>(`${this.base}/${encodeURIComponent(id)}`));
  }

  async cells(id: string, query: CellPageQuery): Promise<GridSearchCellPage> {
    const params = new HttpParams({
      fromObject: { sort_by: query.sort_by, direction: query.direction, page: String(query.page), page_size: String(query.page_size) },
    });
    return firstValueFrom(this.http.get<GridSearchCellPage>(`${this.base}/${encodeURIComponent(id)}/cells`, { params }));
  }

  async delete(id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`${this.base}/${encodeURIComponent(id)}`));
  }
}
