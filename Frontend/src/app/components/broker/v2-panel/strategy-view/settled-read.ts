import { effect, signal, type Resource, type Signal } from '@angular/core';

/** A read's last settled outcome: its value, or why it failed. */
export interface SettledRead<T> {
  readonly value: T | null;
  /** `undefined` unless the last settled read failed. */
  readonly error: unknown;
}

const NOTHING_SETTLED = { value: null, error: undefined } as const;

/**
 * A resource's last settled outcome, kept while a newer read loads. A
 * resource clears its value when its params change; a view re-read every
 * decision would otherwise blank what it draws, and re-announce a refusal,
 * each time. Idle clears it. Call it in an injection context.
 */
export function settledRead<T>(resource: Resource<T | undefined>): Signal<SettledRead<T>> {
  const settled = signal<SettledRead<T>>(NOTHING_SETTLED);
  effect(() => {
    const status = resource.status();
    if (status === 'resolved' || status === 'local') settled.set({ value: resource.value() ?? null, error: undefined });
    else if (status === 'error') settled.set({ value: null, error: resource.error() });
    else if (status === 'idle') settled.set(NOTHING_SETTLED);
  });
  return settled.asReadonly();
}
