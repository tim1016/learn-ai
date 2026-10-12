import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { isRecord } from '../../api/operator-blocker.types';
import { refusalBody } from '../../shared/errors/refusal-body';
import type { SavedSetup, SessionClock, SetupRequest, SetupSummary } from './options-backtest.types';

const BASE = '/api/research/options-backtest';

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
 * The Options Backtest page's setup routes (#3030). The base is relative, so
 * the dev proxy carries the calls whether or not a route is ever guarded.
 */
@Injectable({ providedIn: 'root' })
export class OptionsBacktestService {
  private readonly http = inject(HttpClient);

  /** A full session's open and close, as minutes past New York midnight. */
  sessionClock(): Promise<SessionClock> {
    return firstValueFrom(this.http.get<SessionClock>(`${BASE}/session-clock`));
  }

  /**
   * The setup in plain sentences. The body is exactly what the routes accept:
   * an imported file is sent as it was read, and the server is the judge of it.
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

  private setupUrl(name: string): string {
    return `${BASE}/setups/${encodeURIComponent(name)}`;
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

function readDetail(entry: unknown): ValidationDetail[] {
  if (!isRecord(entry)) return [];
  const { loc, msg } = entry;
  if (typeof msg !== 'string' || !Array.isArray(loc)) return [];
  return [{ loc: loc.filter((part): part is string | number => typeof part === 'string' || typeof part === 'number'), msg }];
}

function toRefusal(error: unknown): SetupRefusedError | null {
  if (!(error instanceof HttpErrorResponse) || (error.status !== 404 && error.status !== 409)) return null;
  const body = refusalBody(error);
  if (body === null || typeof body['code'] !== 'string' || typeof body['message'] !== 'string') return null;
  return new SetupRefusedError(body['code'], body['message'], error.status);
}
