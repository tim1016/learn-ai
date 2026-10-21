import { HttpErrorResponse } from '@angular/common/http';
import { Observable, OperatorFunction, catchError, of, throwError } from 'rxjs';

/**
 * The sentence a failed Python call shows. FastAPI answers with `{ "detail": "..." }`
 * (a string for a route's own failure, a `{ code, message }` object for a route that
 * names its refusals, a list of `{ msg }` for a request it refused to validate);
 * anything else falls back to Angular's transport message.
 */
export function pythonFailureMessage(failure: HttpErrorResponse): string {
  const body: unknown = failure.error;
  const detail = isRecord(body) ? body['detail'] : undefined;
  if (typeof detail === 'string' && detail !== '') return detail;
  if (isRecord(detail) && typeof detail['message'] === 'string' && detail['message'] !== '') {
    return detail['message'];
  }
  if (Array.isArray(detail)) {
    const reasons = detail.flatMap((entry: unknown) =>
      isRecord(entry) && typeof entry['msg'] === 'string' ? [entry['msg']] : []);
    if (reasons.length > 0) return reasons.join('; ');
  }
  return failure.message;
}

/**
 * Whether a failure is the named refusal of a route that puts `{ code, message }` in its
 * `detail`: the right status and the right `code`. A bare 404 (a route the server does not
 * have) is not the refusal of a missing row, so a caller that turns a refusal into a
 * resolved miss checks the code, and every other failure still rejects.
 */
export function isPythonRefusal(failure: unknown, status: number, code: string): boolean {
  if (!(failure instanceof HttpErrorResponse) || failure.status !== status) return false;
  const body: unknown = failure.error;
  const detail = isRecord(body) ? body['detail'] : undefined;
  return isRecord(detail) && detail['code'] === code;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
