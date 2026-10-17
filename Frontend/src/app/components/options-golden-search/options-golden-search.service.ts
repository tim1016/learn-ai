import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { isRecord } from '../../api/operator-blocker.types';
import { extractServerMessage } from '../broker/operation-error';
import { readDetail } from '../options-backtest/options-backtest.service';
import { JobsService } from '../../services/jobs.service';
import type { EvidenceWindow, OptionsStudy, PlanRequest, Preflight, SearchDeclarations, SessionResult, StudyCommand, StudySummary } from './options-golden-search.types';

/** The durable command committed; its exact dispatch remains available for retry. */
export class OptionsSearchDispatchError extends Error {
  constructor(readonly study: OptionsStudy, cause: unknown) {
    super('The command was saved, but its research job could not start. Refresh and resume this study.', { cause });
  }
}

@Injectable({ providedIn: 'root' })
export class OptionsGoldenSearchService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);
  private readonly base = '/api/research/options-golden-search';

  declarations(setupName: string): Promise<SearchDeclarations> {
    return firstValueFrom(this.http.get<SearchDeclarations>(`${this.base}/declarations`, { params: new HttpParams().set('setup_name', setupName) }));
  }

  preflight(request: PlanRequest): Promise<Preflight> {
    return firstValueFrom(this.http.post<Preflight>(`${this.base}/preflight`, request));
  }

  create(request: PlanRequest, preflightHash: string, idempotencyKey: string): Promise<OptionsStudy> {
    return firstValueFrom(this.http.post<OptionsStudy>(`${this.base}/studies`, { request, preflight_hash: preflightHash, idempotency_key: idempotencyKey }));
  }

  list(): Promise<StudySummary[]> {
    return firstValueFrom(this.http.get<StudySummary[]>(`${this.base}/studies`));
  }

  get(id: string): Promise<OptionsStudy> {
    return firstValueFrom(this.http.get<OptionsStudy>(`${this.base}/studies/${encodeURIComponent(id)}`));
  }

  session(studyId: string, candidateHash: string, window: EvidenceWindow, sessionOpenMs: number): Promise<SessionResult> {
    const params = new HttpParams().set('window', window);
    return firstValueFrom(this.http.get<SessionResult>(`${this.base}/studies/${encodeURIComponent(studyId)}/candidates/${encodeURIComponent(candidateHash)}/sessions/${sessionOpenMs}`, { params }));
  }

  async command(id: string, request: StudyCommand): Promise<OptionsStudy> {
    const study = await firstValueFrom(this.http.post<OptionsStudy>(`${this.base}/studies/${encodeURIComponent(id)}/commands`, request));
    if (study.dispatch !== null) {
      try {
        await this.jobs.startJob('options_golden_search', { ...study.dispatch });
      } catch (error) {
        throw new OptionsSearchDispatchError(study, error);
      }
    }
    return study;
  }
}

/** Preserve FastAPI's field-level reasons so a rejected plan can be corrected. */
export function optionsSearchErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof HttpErrorResponse && error.status === 422 && isRecord(error.error)) {
    const detail: unknown = error.error['detail'];
    const messages = Array.isArray(detail) ? detail.flatMap(readDetail).map((item) => `${item.loc.join('.')}: ${item.msg}`) : [];
    if (messages.length > 0) return messages.join('; ');
  }
  return extractServerMessage(error, fallback);
}
