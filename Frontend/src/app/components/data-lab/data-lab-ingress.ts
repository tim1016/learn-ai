/* Legacy Data Lab URL ingress adapter (PRD §14).
 *
 * Pure resolver for pre-redesign /data-lab and /data-quality URLs. It maps a
 * legacy URL to one of the three canonical child routes plus a validated,
 * string-only param bag. Unknown keys are dropped with a warning; invalid
 * values (bad mode, non-UUID sessionId, malformed recipe entries) are dropped
 * or ignored with warnings rather than failing the redirect. Resolved state
 * may populate the workspace but never auto-fetches — that decision lives in
 * the shell, not here.
 *
 * Indicator recipe params are treated opaquely for now (Phase 1): known keys
 * are kept verbatim, everything else is dropped with a warning. */

export type DataLabRoute = '/data-lab/explore' | '/data-lab/export' | '/data-lab/validate';

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

  // /data-quality redirects to Validate (PRD §7.1 / §14).
  const isQualityPath = path === '/data-quality';

  // Mode selection — invalid values fall back with a warning. The default
  // destination is Explore; `sessionId` alone never leaves it (PRD §14).
  let route: DataLabRoute = '/data-lab/explore';
  // A canonical child path keeps its own destination. The /data-quality →
  // /data-lab/validate redirect preserves the query string, so the shell
  // first sees the REDIRECTED URL — keying only on the original legacy
  // paths would drop the bookmark's ticker/range before this runs.
  const canonical = /^\/data-lab\/(explore|export|validate)$/.exec(path);
  if (canonical) route = canonical[0] as DataLabRoute;
  const mode = query.get('mode');
  if (mode !== null) {
    const mapped = MODE_TO_ROUTE[mode];
    if (mapped) route = mapped;
    else warnings.push(`Dropped invalid mode "${mode}"`);
  } else if (isQualityPath) {
    route = '/data-lab/validate';
  }

  // sessionId: Explore unless the legacy mode explicitly selects Export or
  // Validate (PRD §14). Non-UUID values are dropped with a warning.
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
