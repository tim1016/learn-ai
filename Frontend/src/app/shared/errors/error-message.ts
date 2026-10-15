import { HttpErrorResponse } from '@angular/common/http';

/**
 * The text a failed call shows. Angular's `HttpErrorResponse` is not an
 * `Error` subclass, so a bare `instanceof Error` test prints a transport
 * failure as "[object Object]".
 */
export function errorMessage(err: unknown): string {
  return err instanceof Error || err instanceof HttpErrorResponse ? err.message : String(err);
}
