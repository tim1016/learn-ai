import { HttpErrorResponse } from '@angular/common/http';

import { extractServerMessage } from '../../broker/operation-error';
import { DownloadNotAllowedError, PastChainInvalidError, PastChainRefusedError } from './past-chain.service';

/**
 * Where a refusal of a read belongs on the tab. The server names its reasons by code and by the parameter a 422 is
 * about; the page puts each at the control it is about, in the server's own words, and what it cannot place goes to
 * the area that was being read.
 */
export type ControlName = 'underlying' | 'date' | 'time' | 'strikes';

export interface ReadProblem {
  /** The server's sentence, or the browser's reason when the call never reached it. */
  readonly message: string;
  /** The server's code, when it sent one. */
  readonly code: string | null;
  /** The control the reason is about, or the area that was being read when it is about none. */
  readonly control: ControlName | 'area';
  /** Asking again can change the answer: the catalog was out of reach, or the call never arrived. */
  readonly canRetry: boolean;
}

/** The refusal the server gives for a weekend, a holiday, or a date it cannot schedule. */
export const NOT_A_SESSION = 'OPTIONS_BACKTEST_NOT_A_SESSION';
const TIME_REFUSALS: readonly string[] = ['OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION', 'OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE'];
const STRIKES_REFUSALS: readonly string[] = ['OPTIONS_BACKTEST_WINDOW_TOO_WIDE'];
/** What a 422's `loc` names, and the control that holds it. */
const PARAMETER_CONTROLS: ReadonlyMap<string, ControlName> = new Map<string, ControlName>([
  ['underlying', 'underlying'],
  ['trading_date_ms', 'date'],
  ['at_ms', 'time'],
  ['strikes', 'strikes'],
]);

export function readProblemOf(error: unknown, fallback: string): ReadProblem {
  if (error instanceof PastChainRefusedError) {
    return { message: error.message, code: error.code, control: refusalControl(error.code), canRetry: error.status === 503 };
  }
  if (error instanceof PastChainInvalidError) {
    return { message: error.details.map((detail) => detail.msg).join(' '), code: null, control: invalidControl(error), canRetry: false };
  }
  return { message: extractServerMessage(error, fallback), code: null, control: 'area', canRetry: true };
}

function refusalControl(code: string): ControlName | 'area' {
  if (TIME_REFUSALS.includes(code)) return 'time';
  return STRIKES_REFUSALS.includes(code) ? 'strikes' : 'area';
}

/** The control the first parameter a 422 names belongs to; the area when it names none the tab has a control for. */
function invalidControl(error: PastChainInvalidError): ControlName | 'area' {
  for (const detail of error.details) {
    for (const part of detail.loc) {
      const control = typeof part === 'string' ? PARAMETER_CONTROLS.get(part) : undefined;
      if (control !== undefined) return control;
    }
  }
  return 'area';
}

/** Why a download did not come back with an answer (as against an item that failed, which is an answer). */
export type DownloadProblem =
  | { readonly kind: 'not_allowed'; readonly serverMessage: string | null }
  | {
      readonly kind: 'refused';
      readonly message: string;
      readonly code: string | null;
      /**
       * True only when the server refused the request (a 4xx) and so did nothing. After a 5xx, or a call that never
       * came back, it may have fetched some or all of what was asked, so nothing is said to have been left undone.
       */
      readonly nothingDownloaded: boolean;
    };

export function downloadProblemOf(error: unknown): DownloadProblem {
  if (error instanceof DownloadNotAllowedError) return { kind: 'not_allowed', serverMessage: error.serverMessage };
  const problem = readProblemOf(error, 'The download could not be started.');
  const status = statusOf(error);
  return { kind: 'refused', message: problem.message, code: problem.code, nothingDownloaded: status !== null && status >= 400 && status < 500 };
}

/** The HTTP status a failed call came back with, when it came back with one. */
function statusOf(error: unknown): number | null {
  if (error instanceof PastChainRefusedError) return error.status;
  if (error instanceof PastChainInvalidError) return 422;
  return error instanceof HttpErrorResponse ? error.status : null;
}
