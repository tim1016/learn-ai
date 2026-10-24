import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

/* Legacy Data Lab URL ingress.
 *
 * The router owns which page a legacy address opens: `redirectLegacyDataLab`
 * sends `/data-lab` and `/data-quality` straight to the child their `mode`
 * names, and on a canonical child the path wins. What is left for the shell is
 * the workspace state the bookmark carried, which `resolveLegacyDataLabUrl`
 * validates into a string-only param bag, and the notes on what it ignored:
 * unknown keys, a `mode` the path already answers, invalid values (bad mode,
 * non-UUID sessionId, empty scope). Ignored keys stay in the URL, which is why
 * they are reported as ignored rather than dropped. Resolved state may
 * populate the workspace but never auto-fetches — that decision lives in the
 * shell, not here.
 *
 * Indicator recipe params are treated opaquely for now (Phase 1): known keys
 * are kept verbatim, everything else is ignored with a note. */

export type DataLabRoute =
  | '/data-lab/explore'
  | '/data-lab/export'
  | '/data-lab/validate'
  | '/data-lab/returns';

export interface LegacyDataLabResolution {
  /** Surviving, validated query params (string values only). */
  params: Record<string, string>;
  /** Human-readable notes about ignored keys / invalid values. */
  warnings: string[];
}

/** Query keys the pre-implementation inventory preserved as aliases. */
const KNOWN_KEYS: ReadonlySet<string> = new Set([
  'ticker',
  'from',
  'to',
  'trading-session',
  'sessionId',
  'mode',
]);

const MODE_TO_ROUTE: Readonly<Record<string, DataLabRoute>> = {
  explore: '/data-lab/explore',
  build: '/data-lab/export',
  export: '/data-lab/export',
  validate: '/data-lab/validate',
};

/** The child a legacy `mode` names, or `undefined` for a value that names none. */
function routeForMode(mode: unknown): DataLabRoute | undefined {
  return typeof mode === 'string' && Object.hasOwn(MODE_TO_ROUTE, mode) ? MODE_TO_ROUTE[mode] : undefined;
}

/**
 * The redirect for a legacy Data Lab address (`/data-lab`, `/data-quality`).
 *
 * It goes straight to the child the address names — `mode` decides, else
 * `fallback` — in the navigation the router is already making. Everything
 * else the bookmark carried stays in the URL: the query, which the shell
 * reads to populate the workspace and to name what it ignored, and the
 * fragment (`#option-archive-…`). Only a `mode` that names a child is spent,
 * because the path now says it; one that names none stays so the shell can
 * report it.
 */
export function redirectLegacyDataLab(fallback: DataLabRoute): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const named = routeForMode(queryParams['mode']);
    const { mode: _spent, ...rest } = queryParams;
    return inject(Router).createUrlTree([named ?? fallback], {
      queryParams: named === undefined ? queryParams : rest,
      fragment: fragment ?? undefined,
    });
  };
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isUuid(v: string): boolean {
  return UUID_RE.test(v);
}

/** Resolve a legacy /data-lab or /data-quality URL to the workspace params it
 *  carries and the notes on what it ignored. Pure. */
export function resolveLegacyDataLabUrl(url: string): LegacyDataLabResolution {
  const warnings: string[] = [];
  const params: Record<string, string> = {};

  // The legacy URL may arrive scheme-less (e.g. "/data-lab?mode=build");
  // give the parser a synthetic origin when needed.
  const query = new URL(url, 'https://data-lab.invalid').searchParams;

  // The redirect spends a `mode` that names a child. One that reaches the
  // shell is either not a mode, or sits on a child whose path already decided.
  const mode = query.get('mode');
  if (mode !== null) {
    warnings.push(
      routeForMode(mode)
        ? `Ignored mode "${mode}": the address already names the page`
        : `Ignored invalid mode "${mode}"`,
    );
  }

  // Non-UUID sessionId values are ignored with a note.
  const sessionId = query.get('sessionId');
  if (sessionId !== null) {
    if (isUuid(sessionId)) params['sessionId'] = sessionId;
    else warnings.push(`Ignored invalid sessionId "${sessionId}" (not a UUID)`);
  }

  // Known scope keys pass through verbatim (values validated later at the
  // boundary); everything else — including indicator recipe/param keys — is
  // ignored with a note until the bounded recipe schema lands.
  for (const key of new Set(query.keys())) {
    if (key === 'mode' || key === 'sessionId') continue;
    if (!KNOWN_KEYS.has(key)) {
      warnings.push(`Ignored unknown query key "${key}"`);
      continue;
    }
    const value = query.get(key);
    if (value === null || value === '') {
      warnings.push(`Ignored empty query key "${key}"`);
      continue;
    }
    params[key] = value;
  }

  return { params, warnings };
}
