import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { operationsTabRoute, type OperationsTab } from '../../operations/operations-tabs';

/* Legacy Data Lab URL ingress adapter.
 *
 * Pure resolver for pre-redesign /data-lab and /data-quality URLs. It maps a
 * legacy URL to one of the canonical child routes plus a validated,
 * string-only param bag. Unknown keys are dropped with a warning; invalid
 * values (bad mode, non-UUID sessionId, malformed recipe entries) are dropped
 * or ignored with warnings rather than failing the redirect. Resolved state
 * may populate the workspace but never auto-fetches — that decision lives in
 * the shell, not here.
 *
 * Indicator recipe params are treated opaquely for now (Phase 1): known keys
 * are kept verbatim, everything else is dropped with a warning. */

export type DataLabRoute =
  | '/data-lab/explore'
  | '/data-lab/export'
  | '/data-lab/validate'
  | '/data-lab/returns';

export interface LegacyDataLabResolution {
  route: DataLabRoute;
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
 * The Operations tab a retired archive-card anchor belongs to. The card sat on
 * top of every Data Lab tab with one anchor per underlying (`#option-archive-SPY`)
 * and one each for the lake backup and the weekly restore check; those moved to
 * Operations, so an old link carrying one goes there.
 */
function archiveTabFor(fragment: string | null | undefined): OperationsTab | null {
  if (typeof fragment !== 'string' || !/^option-archive(-|$)/.test(fragment)) return null;
  return fragment === 'option-archive-backup' || fragment === 'option-archive-restore' ? 'backups' : 'option-archive';
}

/**
 * The redirect for a legacy Data Lab address (`/data-lab`, `/data-quality`).
 *
 * It goes straight to the child the address names — `mode` decides, else
 * `fallback` — in the navigation the router is already making. Everything
 * else the bookmark carried stays in the URL: the query, which the shell
 * reads to populate the workspace and to name what it ignored, and the
 * fragment. Only a `mode` that names a child is spent, because the path now
 * says it; one that names none stays so the shell can report it.
 *
 * An archive-card anchor (`#option-archive-…`) names a card that now lives in
 * Operations, so such an address leaves Data Lab for that tab, query and
 * fragment intact.
 */
export function redirectLegacyDataLab(fallback: DataLabRoute): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const archiveTab = archiveTabFor(fragment);
    if (archiveTab !== null) {
      return inject(Router).createUrlTree([...operationsTabRoute(archiveTab)], { queryParams, fragment: fragment ?? undefined });
    }
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
  // destination is Explore; `sessionId` alone never leaves it.
  let route: DataLabRoute = '/data-lab/explore';
  // A canonical child path keeps its own destination. The /data-quality →
  // /data-lab/validate redirect preserves the query string, so the shell
  // first sees the REDIRECTED URL — keying only on the original legacy
  // paths would drop the bookmark's ticker/range before this runs.
  const canonical = /^\/data-lab\/(explore|export|validate|returns)$/.exec(path);
  if (canonical) route = canonical[0] as DataLabRoute;
  const mode = query.get('mode');
  if (mode !== null) {
    const mapped = routeForMode(mode);
    if (mapped) route = mapped;
    else warnings.push(`Dropped invalid mode "${mode}"`);
  } else if (isQualityPath) {
    route = '/data-lab/validate';
  }

  // sessionId: Explore unless the legacy mode explicitly selects Export or
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
