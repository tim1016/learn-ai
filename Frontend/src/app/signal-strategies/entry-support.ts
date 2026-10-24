import { HttpErrorResponse } from '@angular/common/http';

import { pythonFailureMessage } from '../services/python-http-failure';
import { errorMessage } from '../shared/errors/error-message';
import type { StrategyRegistryEntry } from './signal-strategy-evidence';

/**
 * What an old or evidence address's resolver needs to say about a read that went
 * wrong, and to look a strategy up, shared by every resolver that names a strategy's
 * owner from stored evidence (Explore's and Golden's entries).
 */

/** Python's own sentence for a failed read when it answered with one; the transport's words otherwise. */
export function failureText(error: unknown): string {
  return error instanceof HttpErrorResponse ? pythonFailureMessage(error) : errorMessage(error);
}

export function isNotFound(error: unknown): boolean {
  return error instanceof HttpErrorResponse && error.status === 404;
}

/** A query value, or `null` when it is missing or only spaces. */
export function nonBlank(value: string | null): string | null {
  const trimmed = value?.trim() ?? '';
  return trimmed === '' ? null : trimmed;
}

export function registered(entries: readonly StrategyRegistryEntry[], key: string): StrategyRegistryEntry | undefined {
  return entries.find((entry) => entry.strategy_key === key);
}
