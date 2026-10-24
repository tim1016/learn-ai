import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { legacyInstrumentTarget } from '../../markets/instrument-address';

/* Legacy Data Lab URL ingress adapter.
 *
 * Pure resolver for pre-redesign /data-lab and /data-quality URLs. It maps a
 * legacy URL to one of the canonical child routes (or to the instrument place
 * the chart moved to) plus a validated, string-only param bag. Unknown keys are dropped with a warning; invalid
 * values (bad mode, non-UUID sessionId, malformed recipe entries) are dropped
 * or ignored with warnings rather than failing the redirect. Resolved state
 * may populate the workspace but never auto-fetches — that decision lives in
 * the shell, not here.
 *
 * Indicator recipe params are treated opaquely for now (Phase 1): known keys
 * are kept verbatim, everything else is dropped with a warning. */

export type DataLabRoute =
  | '/data-lab/export'
  | '/data-lab/validate'
  | '/data-lab/returns';

/** Where a legacy address ends: a Data Lab child, or the instrument place that now holds the chart. */
export type LegacyDataLabTarget = DataLabRoute | 'instrument';

export interface LegacyDataLabResolution {
  route: LegacyDataLabTarget;
  /** Surviving, validated query params (string values only). */
  params: Record<string, string>;
  /** Human-readable notes about dropped keys / invalid values. */
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

const MODE_TO_ROUTE: Readonly<Record<string, LegacyDataLabTarget>> = {
  explore: 'instrument',
  build: '/data-lab/export',
  export: '/data-lab/export',
  validate: '/data-lab/validate',
};

/** The place a legacy `mode` names, or `undefined` for a value that names none. */
function routeForMode(mode: unknown): LegacyDataLabTarget | undefined {
  return typeof mode === 'string' && Object.hasOwn(MODE_TO_ROUTE, mode) ? MODE_TO_ROUTE[mode] : undefined;
}

/**
 * The redirect for a legacy Data Lab address (`/data-lab`, `/data-quality`,
 * `/data-lab/explore`).
 *
 * It goes straight to the place the address names — `mode` decides, else
 * `fallback` — in the navigation the router is already making. A chart goes to
 * the instrument its `ticker` names, window and session carried over, or to
 * Markets when the address names no instrument: it never opens a default one.
 * Everything else the bookmark carried stays in the URL: the query, which the
 * Data Lab shell reads to populate the workspace and to name what it ignored,
 * and the fragment (`#option-archive-…`). Only a `mode` that names a place is
 * spent, because the path now says it; one that names none stays so the shell
 * can report it.
 */
export function redirectLegacyDataLab(fallback: LegacyDataLabTarget): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const named = routeForMode(queryParams['mode']);
    const { mode: _spent, ...rest } = queryParams;
    const target = named ?? fallback;
    const kept = named === undefined ? queryParams : rest;
    const router = inject(Router);
    if (target === 'instrument') {
      const instrument = legacyInstrumentTarget(kept, 'chart');
      return router.createUrlTree(instrument.commands, {
        queryParams: instrument.queryParams,
        fragment: fragment ?? undefined,
      });
    }
    return router.createUrlTree([target], { queryParams: kept, fragment: fragment ?? undefined });
  };
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isUuid(v: string): boolean {
  return UUID_RE.test(v);
}

/** Resolve a legacy /data-lab or /data-quality URL to its canonical child
 *  route, surviving params, and migration warnings. Pure. */
export function resolveLegacyDataLabUrl(url: string): LegacyDataLabResolution {
  const warnings: string[] = [];
  const params: Record<string, string> = {};

  // The legacy URL may arrive scheme-less (e.g. "/data-lab?mode=build");
  // give the parser a synthetic origin when needed.
  const parsed = new URL(url, 'https://data-lab.invalid');
  const path = parsed.pathname.replace(/\/+$/, '') || '/';
  const query = parsed.searchParams;

  // /data-quality redirects to Validate.
  const isQualityPath = path === '/data-quality';

  // Mode selection — invalid values fall back with a warning. The default
  // destination is the instrument's chart; `sessionId` alone never leaves it.
  let route: LegacyDataLabTarget = 'instrument';
  // A canonical child path keeps its own destination. The /data-quality →
  // /data-lab/validate redirect preserves the query string, so the shell
  // first sees the REDIRECTED URL — keying only on the original legacy
  // paths would drop the bookmark's ticker/range before this runs.
  const canonical = /^\/data-lab\/(export|validate|returns)$/.exec(path);
  if (canonical) route = canonical[0] as DataLabRoute;
  const mode = query.get('mode');
  if (mode !== null) {
    const mapped = routeForMode(mode);
    if (mapped) route = mapped;
    else warnings.push(`Dropped invalid mode "${mode}"`);
  } else if (isQualityPath) {
    route = '/data-lab/validate';
  }

  // sessionId: the chart unless the legacy mode explicitly selects Export or
  // Validate. Non-UUID values are dropped with a warning.
  const sessionId = query.get('sessionId');
  if (sessionId !== null) {
    if (isUuid(sessionId)) params['sessionId'] = sessionId;
    else warnings.push(`Dropped invalid sessionId "${sessionId}" (not a UUID)`);
  }

  // Known scope keys pass through verbatim (values validated later at the
  // boundary); everything else — including indicator recipe/param keys — is
  // dropped with a warning until the bounded recipe schema lands.
  for (const key of new Set(query.keys())) {
    if (key === 'mode' || key === 'sessionId') continue;
    if (!KNOWN_KEYS.has(key)) {
      warnings.push(`Dropped unknown query key "${key}"`);
      continue;
    }
    const value = query.get(key);
    if (value === null || value === '') {
      warnings.push(`Dropped empty query key "${key}"`);
      continue;
    }
    params[key] = value;
  }

  return { route, params, warnings };
}
