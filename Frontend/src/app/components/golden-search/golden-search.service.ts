import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { JobsService } from '../../services/jobs.service';
import { toRefusal } from '../grid-search/grid-search.service';
import type {
  CandidateDetail,
  CandidateKey,
  CreateStudyRequest,
  DefaultsMonths,
  EvaluationPage,
  EvaluationQuery,
  GoldenSearchDefaults,
  GoldenSearchPreflight,
  PlanCharts,
  ProtocolRequest,
  QualificationDeployOffer,
  StrategyCapability,
  StudyCommandRequest,
  StudyDetail,
  StudyListFilters,
  SearchCharts,
  StudySummary,
  TestOverTimeCharts,
} from './golden-search.types';

export { GridSearchRefusedError as GoldenSearchRefusedError } from '../grid-search/grid-search.service';

/** A command the server refused because the study moved on (`STALE_REVISION`) or the key was reused (`IDEMPOTENCY_CONFLICT`). */
export class StudyConflictError extends Error {
  constructor(
    readonly code: string,
    message: string,
    /** The study as it is now, when the server sent it (a stale revision does). */
    readonly current: StudyDetail | null,
  ) {
    super(message);
  }
}

/** The server authorized a stage, but its job could not be started; the study still says which stage is pending. */
export class StageDispatchError extends Error {
  constructor(
    readonly study: StudyDetail,
    cause: unknown,
  ) {
    super('The stage was authorized but its job did not start.', { cause });
  }
}

export interface CommandOutcome {
  readonly study: StudyDetail;
  /** The job a dispatched stage now runs under, or null when the command authorized none. */
  readonly jobId: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isStudyDetail(value: unknown): value is StudyDetail {
  return isRecord(value) && typeof value['id'] === 'string' && typeof value['revision'] === 'number' && typeof value['state'] === 'string';
}

/** Translates a 409 with a `{code, message}` detail into a typed conflict; anything else stays as it was. */
export function toConflict(error: unknown): StudyConflictError | null {
  if (!(error instanceof HttpErrorResponse) || error.status !== 409) return null;
  const detail: unknown = isRecord(error.error) ? error.error['detail'] : undefined;
  if (!isRecord(detail) || typeof detail['code'] !== 'string' || typeof detail['message'] !== 'string') return null;
  const current = isStudyDetail(detail['study']) ? detail['study'] : null;
  return new StudyConflictError(detail['code'], detail['message'], current);
}

/**
 * HTTP client for `/api/research/golden-search` (#2696). The base is relative
 * so the dev proxy attaches the data-plane control header to the guarded
 * mutations. Every stage change is a study command; when the server answers
 * one with a `dispatch`, the authorized stage is started as a `golden_search`
 * job through the jobs boundary, which echoes the stage token back to Python.
 */
@Injectable({ providedIn: 'root' })
export class GoldenSearchService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);
  private readonly base = '/api/research/golden-search';
  private readonly qualificationsBase = '/api/research/golden-qualifications';

  async capabilities(): Promise<StrategyCapability[]> {
    return firstValueFrom(this.http.get<StrategyCapability[]>(`${this.base}/capabilities`));
  }

  /** The prefilled plan; with `months`, the server lays the development and final-test dates out for those lengths. */
  async defaults(strategyKey: string, symbol: string, months?: DefaultsMonths): Promise<GoldenSearchDefaults> {
    const query: Record<string, string> = { strategy_key: strategyKey, symbol };
    if (months !== undefined) {
      query['final_months'] = String(months.final_months);
      query['training_months'] = String(months.training_months);
      query['test_months'] = String(months.test_months);
    }
    return firstValueFrom(this.http.get<GoldenSearchDefaults>(`${this.base}/defaults`, { params: new HttpParams({ fromObject: query }) }));
  }

  /** Protocol problems come back as refusals in the body; only a malformed request is an error. */
  async preflight(protocol: ProtocolRequest): Promise<GoldenSearchPreflight> {
    return firstValueFrom(this.http.post<GoldenSearchPreflight>(`${this.base}/preflight`, protocol));
  }

  /** Locks a plan into a new study; a refused lock rejects with `GoldenSearchRefusedError`. */
  async createStudy(request: CreateStudyRequest): Promise<CommandOutcome> {
    let study: StudyDetail;
    try {
      study = await firstValueFrom(this.http.post<StudyDetail>(`${this.base}/studies`, request));
    } catch (error) {
      throw toConflict(error) ?? toRefusal(error) ?? error;
    }
    return this.dispatch(study);
  }

  async list(filters: StudyListFilters = {}): Promise<StudySummary[]> {
    let params = new HttpParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== '' && value !== false) params = params.set(key, String(value));
    }
    return firstValueFrom(this.http.get<StudySummary[]>(`${this.base}/studies`, { params }));
  }

  async get(id: string): Promise<StudyDetail> {
    return firstValueFrom(this.http.get<StudyDetail>(`${this.base}/studies/${encodeURIComponent(id)}`));
  }

  /**
   * Sends one command and starts the stage it authorized. Rejects with
   * `StudyConflictError` (409), `GoldenSearchRefusedError` (400), or
   * `StageDispatchError` when the command committed but its job did not start.
   */
  async command(studyId: string, request: StudyCommandRequest): Promise<CommandOutcome> {
    let study: StudyDetail;
    try {
      study = await firstValueFrom(this.http.post<StudyDetail>(`${this.base}/studies/${encodeURIComponent(studyId)}/commands`, request));
    } catch (error) {
      throw toConflict(error) ?? toRefusal(error) ?? error;
    }
    return this.dispatch(study);
  }

  /** Hides a study from history; its protocol, trials and exposure stay recorded. */
  async hide(id: string): Promise<void> {
    try {
      await firstValueFrom(this.http.delete(`${this.base}/studies/${encodeURIComponent(id)}`));
    } catch (error) {
      throw toConflict(error) ?? toRefusal(error) ?? error;
    }
  }

  async evaluations(id: string, query: EvaluationQuery): Promise<EvaluationPage> {
    let params = new HttpParams({ fromObject: { page: String(query.page), page_size: String(query.page_size) } });
    if (query.stage) params = params.set('stage', query.stage);
    if (query.fold_index !== undefined) params = params.set('fold_index', String(query.fold_index));
    return firstValueFrom(this.http.get<EvaluationPage>(`${this.base}/studies/${encodeURIComponent(id)}/evaluations`, { params }));
  }

  /** The Plan step's charts for a locked study. */
  async planCharts(id: string): Promise<PlanCharts> {
    return firstValueFrom(this.http.get<PlanCharts>(`${this.base}/studies/${encodeURIComponent(id)}/charts/plan`));
  }

  /** The Search step's charts: each recorded procedure's path, knob moves, profiles and scored points. */
  async searchCharts(id: string): Promise<SearchCharts> {
    return firstValueFrom(this.http.get<SearchCharts>(`${this.base}/studies/${encodeURIComponent(id)}/charts/search`));
  }

  /** The Test over time step's charts: every fold, planned or run. */
  async testOverTimeCharts(id: string): Promise<TestOverTimeCharts> {
    return firstValueFrom(this.http.get<TestOverTimeCharts>(`${this.base}/studies/${encodeURIComponent(id)}/charts/test-over-time`));
  }

  async candidate(id: string, key: CandidateKey): Promise<CandidateDetail> {
    return firstValueFrom(this.http.get<CandidateDetail>(`${this.base}/studies/${encodeURIComponent(id)}/candidates/${encodeURIComponent(key)}`));
  }

  /** What Deploy may apply for an approved qualification, with its current status. */
  async deployOffer(qualificationId: string): Promise<QualificationDeployOffer> {
    return firstValueFrom(this.http.get<QualificationDeployOffer>(`${this.qualificationsBase}/${encodeURIComponent(qualificationId)}/deploy-offer`));
  }

  private async dispatch(study: StudyDetail): Promise<CommandOutcome> {
    if (study.dispatch === null) return { study, jobId: null };
    try {
      const jobId = await this.jobs.startJob(study.dispatch.job_type, { ...study.dispatch.payload });
      return { study, jobId };
    } catch (error) {
      throw new StageDispatchError(study, error);
    }
  }
}
