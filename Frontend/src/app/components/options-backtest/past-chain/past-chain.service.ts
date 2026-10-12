import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { isRecord } from '../../../api/operator-blocker.types';
import { refusalBody } from '../../../shared/errors/refusal-body';
import type { ValidationDetail } from '../options-backtest.service';
import type { ChainDay, ChainExpiry, DownloadAnswer, DownloadRequest } from './past-chain.types';

/** Relative, so the dev proxy can carry the guarded download: it adds the data plane's control secret itself. */
const BASE = '/api/research/options-backtest/past-chain';
/** The statuses the three routes refuse with a reason of their own: a request they cannot answer (400) and a catalog they cannot reach (503). */
const REFUSAL_STATUSES: readonly number[] = [400, 503];

/** What the day is asked for: the underlying and its trading date as New York midnight. */
export interface DayQuery {
  readonly underlying: string;
  readonly tradingDateMs: number;
}

/**
 * One expiry at one instant. `atMs` is null for "the session's close" (the server's own default) and `strikes` is
 * null for "the server's own number": the page sends neither unless the owner chose one. `strikes` is the digits typed.
 */
export interface ExpiryQuery extends DayQuery {
  readonly expiryMs: number;
  readonly atMs: number | null;
  readonly strikes: string | null;
}

/** The server refused with a reason of its own: its code, its sentence, and the status it came with (400 or 503). */
export class PastChainRefusedError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** The server read the request and it does not validate (HTTP 422): what is wrong, and where. */
export class PastChainInvalidError extends Error {
  constructor(readonly details: readonly ValidationDetail[]) {
    super('The server did not accept the request.');
  }
}

/** The data plane would not take the download (HTTP 401 or 403): this page has no right to start one. */
export class DownloadNotAllowedError extends Error {
  constructor(
    /** The server's own sentence, when it sent one. */
    readonly serverMessage: string | null,
    readonly status: number,
  ) {
    super('This page is not allowed to start a download here.');
  }
}

/**
 * The Past chain tab's three routes (#3030). The two reads are open. The download spends vendor requests and writes
 * the lake, so the data plane guards it with its control secret; the page never holds that secret. It makes a plain
 * relative POST, `dataPlaneControlIntentInterceptor` marks it as the browser's own control call, and the dev proxy
 * attaches the secret on the way, as it does for the Data Lake page.
 */
@Injectable({ providedIn: 'root' })
export class PastChainService {
  private readonly http = inject(HttpClient);

  /** Every expiry the underlying's listing held on the date, each tagged. */
  async day(query: DayQuery): Promise<ChainDay> {
    const params = new HttpParams().set('underlying', query.underlying).set('trading_date_ms', String(query.tradingDateMs));
    try {
      return await firstValueFrom(this.http.get<ChainDay>(`${BASE}/day`, { params }));
    } catch (error) {
      throw toReadError(error);
    }
  }

  /** One expiry's calls and puts around the money, each with the price it last traded at by the instant. */
  async expiry(query: ExpiryQuery): Promise<ChainExpiry> {
    let params = new HttpParams()
      .set('underlying', query.underlying)
      .set('trading_date_ms', String(query.tradingDateMs))
      .set('expiry_ms', String(query.expiryMs));
    if (query.atMs !== null) params = params.set('at_ms', String(query.atMs));
    if (query.strikes !== null) params = params.set('strikes', query.strikes);
    try {
      return await firstValueFrom(this.http.get<ChainExpiry>(`${BASE}/expiry`, { params }));
    } catch (error) {
      throw toReadError(error);
    }
  }

  /** Captures into the lake what the request names. A vendor or lake failure is an item of the answer, never a failed call. */
  async download(request: DownloadRequest): Promise<DownloadAnswer> {
    try {
      return await firstValueFrom(this.http.post<DownloadAnswer>(`${BASE}/download`, request));
    } catch (error) {
      throw toNotAllowed(error) ?? toReadError(error);
    }
  }
}

/** A refusal or a 422 as a typed error; anything else (the network, a 500) is let through as it came. */
function toReadError(error: unknown): unknown {
  return toRefusal(error) ?? toInvalid(error) ?? error;
}

function toRefusal(error: unknown): PastChainRefusedError | null {
  if (!(error instanceof HttpErrorResponse) || !REFUSAL_STATUSES.includes(error.status)) return null;
  const body = refusalBody(error);
  if (body === null) return null;
  // The routes' own refusals are `{detail: {code, message}}`; the data plane's guard answers flat, with a `reason`.
  const code = typeof body['code'] === 'string' ? body['code'] : body['reason'];
  const message = body['message'];
  return typeof code === 'string' && typeof message === 'string' ? new PastChainRefusedError(code, message, error.status) : null;
}

function toInvalid(error: unknown): PastChainInvalidError | null {
  if (!(error instanceof HttpErrorResponse) || error.status !== 422) return null;
  const detail: unknown = isRecord(error.error) ? error.error['detail'] : undefined;
  const found = Array.isArray(detail) ? detail.flatMap(readDetail) : [];
  return new PastChainInvalidError(found.length > 0 ? found : [{ loc: [], msg: 'The server did not accept the request.' }]);
}

function readDetail(entry: unknown): ValidationDetail[] {
  if (!isRecord(entry)) return [];
  const { loc, msg } = entry;
  if (typeof msg !== 'string' || !Array.isArray(loc)) return [];
  return [{ loc: loc.filter((part): part is string | number => typeof part === 'string' || typeof part === 'number'), msg }];
}

function toNotAllowed(error: unknown): DownloadNotAllowedError | null {
  if (!(error instanceof HttpErrorResponse) || (error.status !== 401 && error.status !== 403)) return null;
  const message = refusalBody(error)?.['message'];
  return new DownloadNotAllowedError(typeof message === 'string' ? message : null, error.status);
}
