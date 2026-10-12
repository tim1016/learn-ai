import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { isRecord } from '../../../api/operator-blocker.types';
import { refusalBody } from '../../../shared/errors/refusal-body';
import type { CsvDownload, RunComparison, SessionDetail, TradeQuery, TradesPage } from './run-detail.types';

const BASE = '/api/research/options-backtest';
/** The statuses the closer-look routes refuse with, each in the `{detail: {code, message}}` form. */
const REFUSALS: readonly number[] = [404, 409, 500];
/** What a file is called when the server named none. */
export const UNNAMED_EXPORT = 'options-backtest-trades.csv';

/**
 * The server refused with a reason of its own: no such run (404), the run is not complete (409), the session or the
 * leg is not in the run (404), two runs that cannot be compared (409), or a stored session that cannot be drawn (500).
 * The message is the server's, to be shown as it is.
 */
export class RunDetailRefusedError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/**
 * The routes that read a saved run closely (#3030): its leg trades, one of its sessions, every trade as a CSV, and two
 * runs side by side. The base is relative, so the dev proxy carries the calls. Nothing here changes a run.
 */
@Injectable({ providedIn: 'root' })
export class RunDetailService {
  private readonly http = inject(HttpClient);

  /** One page of a complete run's trades, in the server's order. Only the filters that are set are sent. */
  async trades(runId: number, query: TradeQuery = {}): Promise<TradesPage> {
    let params = new HttpParams();
    if (query.sessionOpenMs != null) params = params.set('session_open_ms', query.sessionOpenMs);
    if (query.legId != null && query.legId !== '') params = params.set('leg_id', query.legId);
    if (query.limit !== undefined) params = params.set('limit', query.limit);
    if (query.offset !== undefined) params = params.set('offset', query.offset);
    try {
      return await firstValueFrom(this.http.get<TradesPage>(`${BASE}/runs/${runId}/trades`, { params }));
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** One session of a complete run: what was stored for it, and the series, levels and fills a timeline draws. */
  async session(runId: number, sessionOpenMs: number): Promise<SessionDetail> {
    try {
      return await firstValueFrom(this.http.get<SessionDetail>(`${BASE}/runs/${runId}/sessions/${sessionOpenMs}`));
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }

  /** Every trade of a complete run as the server's CSV, untouched, and the name the server gave it. */
  async tradesCsv(runId: number): Promise<CsvDownload> {
    try {
      const response = await firstValueFrom(
        this.http.get(`${BASE}/runs/${runId}/trades.csv`, { observe: 'response', responseType: 'blob' }),
      );
      const blob = response.body;
      if (blob === null) throw new Error('The server sent an empty answer for the trades file.');
      return { blob, filename: filenameOf(response.headers.get('Content-Disposition')) };
    } catch (error) {
      throw (await toBlobRefusal(error)) ?? error;
    }
  }

  /** Two complete runs of one underlying, worked out by the server. */
  async compare(a: number, b: number): Promise<RunComparison> {
    const params = new HttpParams().set('a', a).set('b', b);
    try {
      return await firstValueFrom(this.http.get<RunComparison>(`${BASE}/runs/compare`, { params }));
    } catch (error) {
      throw toRefusal(error) ?? error;
    }
  }
}

/** The file name in a `Content-Disposition` header (`attachment; filename="x.csv"`); null when it names none that is safe to use. */
export function filenameOf(header: string | null): string | null {
  if (header === null) return null;
  const found = /filename="([^"]+)"|filename=([^;\s"][^;\s]*)/i.exec(header);
  const name = (found?.[1] ?? found?.[2] ?? '').trim();
  return name === '' || /[\\/]/.test(name) ? null : name;
}

function toRefusal(error: unknown): RunDetailRefusedError | null {
  if (!(error instanceof HttpErrorResponse) || !REFUSALS.includes(error.status)) return null;
  const body = refusalBody(error);
  return refusalFrom(body, error.status);
}

function refusalFrom(body: Record<string, unknown> | null, status: number): RunDetailRefusedError | null {
  if (body === null || typeof body['code'] !== 'string' || typeof body['message'] !== 'string') return null;
  return new RunDetailRefusedError(body['code'], body['message'], status);
}

/** A refusal of the CSV route arrives as a Blob, because the call asked for one; read its text to find the server's words. */
async function toBlobRefusal(error: unknown): Promise<RunDetailRefusedError | null> {
  if (!(error instanceof HttpErrorResponse) || !REFUSALS.includes(error.status) || !(error.error instanceof Blob)) return null;
  try {
    const parsed: unknown = JSON.parse(await error.error.text());
    const detail = isRecord(parsed) ? parsed['detail'] : undefined;
    return refusalFrom(isRecord(detail) ? detail : null, error.status);
  } catch (unreadable) {
    // A body that is not the server's JSON: say nothing of it here, and let the caller show its own words.
    if (unreadable instanceof SyntaxError) return null;
    throw unreadable;
  }
}
