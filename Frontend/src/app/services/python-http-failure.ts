import { HttpErrorResponse } from '@angular/common/http';
import { Observable, OperatorFunction, catchError, of, throwError } from 'rxjs';
import { isRecord } from '../api/operator-blocker.types';
import { extractServerMessage } from '../components/broker/operation-error';
import { errorMessage } from '../shared/errors/error-message';
import { refusalBody } from '../shared/errors/refusal-body';

/**
 * What this file adds to the shared readers of FastAPI's `detail` (a second reader of that
 * body is the shape of bug #2081, so it delegates):
 *
 * - `extractServerMessage` (`components/broker/operation-error`) reads the sentence: a string
 *   `detail`, a `detail.message` object, the fleet's flat `{reason, message}` body.
 * - `refusalBody` (`shared/errors/refusal-body`) reads the `{ code, message }` object a route
 *   puts in `detail` when it names its refusals.
 * - `errorMessage` (`shared/errors/error-message`) is the fallback for a failure with no body.
 *
 * What none of them provides, and so stays here: the 422 `detail` list of `{ msg }` that
 * FastAPI answers when it refuses to validate a request (joined with "; "), and the two
 * operators below that turn a failure into a result or an `Error`.
 */

/**
 * The sentence a failed Python call shows: the body's own message, else the 422 reasons,
 * else Angular's transport message.
 */
export function pythonFailureMessage(failure: HttpErrorResponse): string {
  const sentence = extractServerMessage(failure, '');
  if (sentence !== '') return sentence;
  const body: unknown = failure.error;
  const detail = isRecord(body) ? body['detail'] : undefined;
  if (Array.isArray(detail)) {
    const reasons = detail.flatMap((entry: unknown) =>
      isRecord(entry) && typeof entry['msg'] === 'string' ? [entry['msg']] : []);
    if (reasons.length > 0) return reasons.join('; ');
  }
  return errorMessage(failure);
}

/**
 * Whether a failure is the named refusal of a route that puts `{ code, message }` in its
 * `detail`: the right status and the right `code`. A bare 404 (a route the server does not
 * have) is not the refusal of a missing row, so a caller that turns a refusal into a
 * resolved miss checks the code, and every other failure still rejects.
 */
export function isPythonRefusal(failure: unknown, status: number, code: string): boolean {
  return failure instanceof HttpErrorResponse && failure.status === status
    && refusalBody(failure)?.['code'] === code;
}

/**
 * For a result that carries `success` and `error`: an HTTP failure becomes that
 * result with `success: false` and the failure's sentence, so a screen's
 * `!result.success` branch is the one path for every kind of failure. Anything that
 * is not an HTTP failure still rejects.
 */
export function failureAsResult<T>(failed: (error: string) => T): OperatorFunction<T, T> {
  return (source: Observable<T>) =>
    source.pipe(
      catchError((err: unknown) =>
        err instanceof HttpErrorResponse ? of(failed(pythonFailureMessage(err))) : throwError(() => err)),
    );
}

/**
 * For a call that has no in-band failure shape: an HTTP failure rejects with an
 * `Error` carrying the failure's sentence (prefixed by `context` when given), so a
 * screen's `err instanceof Error ? err.message : …` shows it.
 */
export function failureAsError<T>(context?: string): OperatorFunction<T, T> {
  return (source: Observable<T>) =>
    source.pipe(
      catchError((err: unknown) => {
        if (!(err instanceof HttpErrorResponse)) return throwError(() => err);
        const sentence = pythonFailureMessage(err);
        return throwError(() => new Error(context === undefined ? sentence : `${context}: ${sentence}`));
      }),
    );
}
