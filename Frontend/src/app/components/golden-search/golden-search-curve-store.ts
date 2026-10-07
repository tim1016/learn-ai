import { resource, signal } from '@angular/core';

/** What a curve read answers: its session closes, each asked curve that was kept, and the keys that kept none. */
export interface CurvesRead<C> {
  readonly sessions_ms: readonly number[];
  readonly curves: readonly C[];
  readonly not_recorded: readonly string[];
}

/** A kept curve with the session closes of the read that returned it. */
export interface HeldCurve<C> {
  readonly sessions: readonly number[];
  readonly curve: C;
}

/**
 * The curves of the runs a page has ticked, each read once and kept: a
 * recorded curve never changes. A tick asks only for the curves not held
 * yet. `scope` is what the keys belong to (a study, a search), and a curve is
 * held under it, so two scopes that share keys never share a curve: a
 * walk-forward fold's training and test sweeps hold the same cells. Call it
 * in an injection context.
 */
export function curveStore<C>(options: {
  readonly scope: () => string;
  readonly keys: () => readonly string[];
  readonly read: (scope: string, keys: readonly string[]) => Promise<CurvesRead<C>>;
  readonly keyOf: (curve: C) => string;
}) {
  const kept = signal<ReadonlyMap<string, HeldCurve<C> | null>>(new Map());
  const under = (scope: string, key: string): string => `${scope}\n${key}`;
  const reads = resource({
    // Nothing ticked reads nothing.
    params: () => (options.keys().length === 0 ? undefined : { scope: options.scope(), keys: options.keys() }),
    loader: async ({ params }) => {
      const held = kept();
      const missing = params.keys.filter((key) => !held.has(under(params.scope, key)));
      if (missing.length === 0) return;
      const read = await options.read(params.scope, missing);
      const curves = read.curves.map((curve) => [under(params.scope, options.keyOf(curve)), { sessions: read.sessions_ms, curve }] as const);
      kept.update((before) => new Map([...before, ...curves, ...read.not_recorded.map((key) => [under(params.scope, key), null] as const)]));
    },
  });
  return {
    /** The reads behind the store: their error, whether one is in flight, and a retry. */
    reads,
    /** The key's curve: null when its run kept none, undefined while it has not been read. */
    of: (key: string): HeldCurve<C> | null | undefined => kept().get(under(options.scope(), key)),
  };
}
