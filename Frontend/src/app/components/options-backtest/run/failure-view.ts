import type { RunFailure } from '../options-backtest.types';

type FailureItem = RunFailure['items'][number];

/** The things that failed for one reason: a failure can name every session of a long range, so they are shown by code, with a count. */
export interface FailureGroup {
  readonly code: string;
  readonly items: readonly FailureItem[];
}

/** A group this small is shown open; a larger one waits to be opened. */
export const SMALL_GROUP = 5;

/** The items grouped by their code, in the order each code is first met. A code is whatever the server wrote: none is known to this page. */
export function failureGroups(items: readonly FailureItem[]): readonly FailureGroup[] {
  const groups = new Map<string, FailureItem[]>();
  for (const item of items) groups.set(item.code, [...(groups.get(item.code) ?? []), item]);
  return [...groups].map(([code, grouped]) => ({ code, items: grouped }));
}
