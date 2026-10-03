import { effect, untracked, type Signal } from '@angular/core';

/**
 * Reads a step's charts again when `progress` changes (#2821). A reload keeps
 * what is drawn while it reads, where a change of params would blank it. The
 * first read starts with the progress at creation, and a reload asked for
 * while one is in flight is dropped, so a change during any read waits for it
 * to settle and then catches up. `ready` holds the reload back while there is
 * nothing to read. Call it in an injection context.
 */
export function reloadOnProgress(progress: () => unknown, charts: { readonly isLoading: Signal<boolean>; reload(): boolean }, ready: () => boolean = () => true): void {
  let seen: { readonly value: unknown } | null = null;
  effect(() => {
    const value = progress();
    const loading = charts.isLoading();
    if (seen === null) {
      seen = { value };
      return;
    }
    if (loading || Object.is(value, seen.value) || !ready()) return;
    seen = { value };
    untracked(() => charts.reload());
  });
}
