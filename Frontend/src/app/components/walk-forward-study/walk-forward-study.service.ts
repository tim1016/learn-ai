import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import { JobsService } from '../../services/jobs.service';
import { toRefusal } from '../grid-search/grid-search.service';
import type {
  WalkForwardStudyDetail,
  WalkForwardStudyHistoryFilters,
  WalkForwardStudyPreflight,
  WalkForwardStudySpecRequest,
  WalkForwardStudySummary,
} from './walk-forward-study.types';

/**
 * HTTP client for `/api/research/walk-forward-studies` plus the launch and
 * Finish verbs, which go through the jobs boundary (`walk_forward_study`)
 * so progress and cancellation ride the shared job stream. Refusals share
 * Grid Search's `{code, message}` shape and error class.
 */
@Injectable({ providedIn: 'root' })
export class WalkForwardStudyService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);
  private readonly base = `${environment.pythonServiceUrl}/api/research/walk-forward-studies`;

  async preflight(spec: WalkForwardStudySpecRequest): Promise<WalkForwardStudyPreflight> {
    try {
      return await firstValueFrom(this.http.post<WalkForwardStudyPreflight>(`${this.base}/preflight`, spec));
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** Starts a `walk_forward_study` job; resolves to the job id once Python has made the record durable. */
  async launch(spec: WalkForwardStudySpecRequest): Promise<string> {
    try {
      return await this.jobs.startJob('walk_forward_study', { ...spec });
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** Finishes an incomplete study under a new job: completed folds are kept, the rest run. */
  async finish(detail: WalkForwardStudyDetail): Promise<string> {
    return this.jobs.startJob('walk_forward_study', { ...detail.request, resume_study_id: detail.id });
  }

  async list(filters: WalkForwardStudyHistoryFilters = {}): Promise<WalkForwardStudySummary[]> {
    let params = new HttpParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value) params = params.set(key, String(value));
    }
    return firstValueFrom(this.http.get<WalkForwardStudySummary[]>(this.base, { params }));
  }

  async get(id: string): Promise<WalkForwardStudyDetail> {
    return firstValueFrom(this.http.get<WalkForwardStudyDetail>(`${this.base}/${encodeURIComponent(id)}`));
  }

  async delete(id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`${this.base}/${encodeURIComponent(id)}`));
  }
}
