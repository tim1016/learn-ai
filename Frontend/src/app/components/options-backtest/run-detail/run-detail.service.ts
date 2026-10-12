import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { isRecord } from '../../../api/operator-blocker.types';
import { refusalBody } from '../../../shared/errors/refusal-body';
import { readDetail } from '../options-backtest.service';
import type { CsvDownload, RunComparison, SessionDetail, TradeQuery, TradesPage } from './run-detail.types';

const BASE = '/api/research/options-backtest';
/** The statuses the closer-look routes refuse with, each in the `{detail: {code, message}}` form. */
const REFUSALS: readonly number[] = [404, 409, 500];
/** What a file is called when the server named none. */
export const UNNAMED_EXPORT = 'options-backtest-trades.csv';
/** The server sent no code of its own with a 422 (a parameter it will not take); this stands for it. */
const INVALID_REQUEST = 'HTTP_422';

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

/**
 * The file name in a `Content-Disposition` header, read as RFC 6266 says: `filename*` (RFC 8187, the form for a name that
 * is not plain ASCII) is the name when the header holds both, and `filename` is the way back when it holds only that or
 * when the starred name cannot be used. Only UTF-8 is read in the starred form, the one charset RFC 8187 allows. A name
 * that is empty or only dots, holds a path separator, a control character or a bidirectional or format control (which
 * can change how a name reads), or cannot be decoded is not trusted; with none left the caller names the file.
 */
export function filenameOf(header: string | null): string | null {
  if (header === null) return null;
  return safeName(starredName(header)) ?? safeName(plainName(header));
}

function plainName(header: string): string {
  const found = /filename="([^"]+)"|filename=([^;\s"][^;\s]*)/i.exec(header);
  return found?.[1] ?? found?.[2] ?? '';
}

/** `filename*=UTF-8'language'percent-encoded-bytes`, decoded. */
function starredName(header: string): string {
  const found = /filename\*\s*=\s*UTF-8'[A-Za-z0-9-]*'([A-Za-z0-9!#$&+.^_`|~%-]+)/i.exec(header);
  if (found === null) return '';
  try {
    return decodeURIComponent(found[1]);
  } catch (malformed) {
    // A percent sign that starts no byte, or bytes that are not UTF-8: not a name, so the plain one is looked for.
    if (malformed instanceof URIError) return '';
    throw malformed;
  }
}

/** Arabic letter mark, the left-to-right and right-to-left marks, the embedding and override controls, the isolates, and the byte order mark. */
const FORMAT_CONTROL = /[\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/;

/** The C0 controls and DEL, and the C1 controls. */
function isControl(character: string): boolean {
  const code = character.charCodeAt(0);
  return code < 32 || (code >= 127 && code <= 159);
}

function safeName(name: string): string | null {
  const trimmed = name.trim();
  const unusable = trimmed === '' || /^\.+$/.test(trimmed) || /[\\/]/.test(trimmed) || FORMAT_CONTROL.test(trimmed) || [...trimmed].some(isControl);
  return unusable ? null : trimmed;
}

function toRefusal(error: unknown): RunDetailRefusedError | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  if (error.status === 422) return invalidRequest(error);
  if (!REFUSALS.includes(error.status)) return null;
  const body = refusalBody(error);
  return refusalFrom(body, error.status);
}

/** A 422 is FastAPI's `{detail: [{loc, msg}]}` for a parameter it will not take: its first message, naming the parameter it is about. */
function invalidRequest(error: HttpErrorResponse): RunDetailRefusedError | null {
  const detail: unknown = isRecord(error.error) ? error.error['detail'] : undefined;
  const first = Array.isArray(detail) ? detail.flatMap(readDetail).at(0) : undefined;
  if (first === undefined) return null;
  const parameter = first.loc.at(-1);
  return new RunDetailRefusedError(INVALID_REQUEST, typeof parameter === 'string' ? `${parameter}: ${first.msg}` : first.msg, 422);
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
