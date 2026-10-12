import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { isRecord } from '../../api/operator-blocker.types';
import { refusalBody } from '../../shared/errors/refusal-body';
import { JobsService } from '../../services/jobs.service';
import { toRefusal as toJobRefusal } from '../grid-search/grid-search.service';
import type {
  DataCheck,
  RunDetail,
  RunListItem,
  RunStartRequest,
  SavedSetup,
  SessionClock,
  SetupLimits,
  SetupRequest,
  SetupSummary,
} from './options-backtest.types';

const BASE = '/api/research/options-backtest';
/** The statuses a route refuses with, in the `{code, message}` form. */
const SETUP_REFUSALS: readonly number[] = [404, 409];
const RUN_REFUSALS: readonly number[] = [400, 404, 409, 503];

/** One thing FastAPI's 422 says is wrong: where (`loc`, e.g. `body.legs.0.contracts`) and what (`msg`). */
export interface ValidationDetail {
  readonly loc: readonly (string | number)[];
  readonly msg: string;
}

/** The server read the setup and it does not validate (HTTP 422). */
export class SetupInvalidError extends Error {
  constructor(readonly details: readonly ValidationDetail[]) {
    super('The setup is not valid.');
  }
}

/** The server refused with a reason of its own: no such setup (404), or the name is an example's (409). */
export class SetupRefusedError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/**
 * The server refused a run call with a reason of its own: the range is not one
 * a run can take (400), the data catalog cannot be asked (503), no such run
 * (404), or a run that is going cannot be deleted (409).
 */
export class RunRefusedError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** Which runs to list. */
export interface RunQuery {
  /** Only the runs of this job: the one run a job made. */
  readonly jobId?: string;
  readonly limit?: number;
}

/**
 * The Options Backtest page's routes (#3030). The base is relative, so the dev
 * proxy carries the calls whether or not a route is ever guarded. A run is
 * started through the shared jobs service, as Grid Search's is.
 */
@Injectable({ providedIn: 'root' })
export class OptionsBacktestService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);

  /** A full session's open and close, as minutes past New York midnight. */
  sessionClock(): Promise<SessionClock> {
    return firstValueFrom(this.http.get<SessionClock>(`${BASE}/session-clock`));
  }

  /**
   * The setup in plain sentences, and the setup as the server read it (every
   * default filled in). The body is exactly what the routes accept: an
   * imported file is sent as it was read, and the server is the judge of it.
   */
  async describe(body: unknown): Promise<SetupSummary> {
    try {
      return await firstValueFrom(this.http.post<SetupSummary>(`${BASE}/setups/describe`, body));
    } catch (error) {
      throw toInvalid(error) ?? error;
    }
  }

  /** The two examples, then the saved setups, newest first. */
  list(): Promise<SavedSetup[]> {
    return firstValueFrom(this.http.get<SavedSetup[]>(`${BASE}/setups`));
  }

  async save(name: string, setup: SetupRequest): Promise<SavedSetup> {
    try {
      return await firstValueFrom(this.http.put<SavedSetup>(this.setupUrl(name), setup));
    } catch (error) {
      throw toRefusal(error) ?? toInvalid(error) ?? error;
    }
  }

  async remove(name: string): Promise<void> {
    try {
      await firstValueFrom(this.http.delete(this.setupUrl(name)));
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** What the server holds a setup, a name and a range to. The page copies none of it. */
  limits(): Promise<SetupLimits> {
    return firstValueFrom(this.http.get<SetupLimits>(`${BASE}/limits`));
  }

  /** Which sessions of the range can be tested, from what is stored. Nothing is fetched to answer. */
  async checkData(setup: SetupRequest, startMs: number, endMs: number): Promise<DataCheck> {
    try {
      return await firstValueFrom(this.http.post<DataCheck>(`${BASE}/check-data`, { setup, start_ms: startMs, end_ms: endMs }));
    } catch (error) {
      throw toRunRefusal(error) ?? error;
    }
  }

  /** This page's runs, newest first. */
  runs(query: RunQuery = {}): Promise<RunListItem[]> {
    let params = new HttpParams();
    if (query.jobId !== undefined) params = params.set('job_id', query.jobId);
    if (query.limit !== undefined) params = params.set('limit', query.limit);
    return firstValueFrom(this.http.get<RunListItem[]>(`${BASE}/runs`, { params }));
  }

  async run(id: number): Promise<RunDetail> {
    try {
      return await firstValueFrom(this.http.get<RunDetail>(this.runUrl(id)));
    } catch (error) {
      throw toRunRefusal(error) ?? error;
    }
  }

  async deleteRun(id: number): Promise<void> {
    try {
      await firstValueFrom(this.http.delete(this.runUrl(id)));
    } catch (error) {
      throw toRunRefusal(error) ?? error;
    }
  }

  /**
   * Starts the run as a `options_backtest` job and resolves to the job id once
   * the server has made the run's record. A range the server refuses (400)
   * starts no job and comes back as a `RunRefusedError` with the server's
   * message; so does any other refusal that carries the server's code and message.
   */
  async startRun(request: RunStartRequest): Promise<string> {
    try {
      return await this.jobs.startJob('options_backtest', { ...request });
    } catch (error) {
      const refusal = toJobRefusal(error);
      if (refusal !== null) throw new RunRefusedError(refusal.refusal.code, refusal.refusal.message, 400);
      throw toStartRefusal(error) ?? error;
    }
  }

  private setupUrl(name: string): string {
    return `${BASE}/setups/${encodeURIComponent(name)}`;
  }

  private runUrl(id: number): string {
    return `${BASE}/runs/${id}`;
  }
}

function toInvalid(error: unknown): SetupInvalidError | null {
  if (!(error instanceof HttpErrorResponse) || error.status !== 422) return null;
  return new SetupInvalidError(validationDetails(error.error));
}

/** FastAPI's `{detail: [{loc, msg, ...}]}`; a 422 in any other shape is still one problem, said plainly. */
function validationDetails(body: unknown): ValidationDetail[] {
  const detail = isRecord(body) ? body['detail'] : undefined;
  const found = Array.isArray(detail) ? detail.flatMap(readDetail) : [];
  return found.length > 0 ? found : [{ loc: [], msg: 'The server did not accept the setup.' }];
}

export function readDetail(entry: unknown): ValidationDetail[] {
  if (!isRecord(entry)) return [];
  const { loc, msg } = entry;
  if (typeof msg !== 'string' || !Array.isArray(loc)) return [];
  return [{ loc: loc.filter((part): part is string | number => typeof part === 'string' || typeof part === 'number'), msg }];
}

function toRefusal(error: unknown): SetupRefusedError | null {
  const refusal = readRefusal(error, SETUP_REFUSALS);
  return refusal === null ? null : new SetupRefusedError(refusal.code, refusal.message, refusal.status);
}

/**
 * A refusal of the job route other than a bad range (the run is already known, the data plane is down), with the
 * server's own code and message, wherever the jobs boundary put them: in the body, or as the text of a `detail`.
 */
function toStartRefusal(error: unknown): RunRefusedError | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  const body = refusalBody(error) ?? forwardedBody(error);
  if (body === null || typeof body['code'] !== 'string' || typeof body['message'] !== 'string') return null;
  return new RunRefusedError(body['code'], body['message'], error.status);
}

/** The data plane's JSON body, which the jobs boundary forwards as the text of a ProblemDetails `detail`. */
function forwardedBody(error: HttpErrorResponse): Record<string, unknown> | null {
  const detail: unknown = isRecord(error.error) ? error.error['detail'] : undefined;
  if (typeof detail !== 'string') return null;
  try {
    const parsed: unknown = JSON.parse(detail);
    const inner = isRecord(parsed) ? parsed['detail'] : undefined;
    return isRecord(inner) ? inner : null;
  } catch {
    return null;
  }
}

function toRunRefusal(error: unknown): RunRefusedError | null {
  const refusal = readRefusal(error, RUN_REFUSALS);
  return refusal === null ? null : new RunRefusedError(refusal.code, refusal.message, refusal.status);
}

/** The `{code, message}` a route refused with, when the status is one of those the route refuses with. */
function readRefusal(error: unknown, statuses: readonly number[]): { code: string; message: string; status: number } | null {
  if (!(error instanceof HttpErrorResponse) || !statuses.includes(error.status)) return null;
  const body = refusalBody(error);
  if (body === null || typeof body['code'] !== 'string' || typeof body['message'] !== 'string') return null;
  return { code: body['code'], message: body['message'], status: error.status };
}
