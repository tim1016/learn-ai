import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

import { RETIRED_LENS_QUERY_PARAM, withoutRetiredLens } from './home-redirect.guard';

/** Retire the desk's old `?surface=bots|gallery` bookmark shape, and the
 * retired `?lens=` (PRD #2560 D2).
 *
 * The surface hints became real read-only chooser routes
 * (`/brokers/alpaca/bots`, `/brokers/alpaca/gallery`), so a bookmarked hint
 * URL redirects there and cleans itself up — it never silently degrades to
 * the bare directory the hint used to annotate. Every other query parameter
 * the bookmark carries travels with the redirect, so a co-traveling `deploy`
 * intent survives; a `lens` never does. Any other `surface` value is left
 * alone for the desk to render as-is. */
export const alpacaSurfaceRedirectGuard: CanActivateFn = (route, state) => {
  const router = inject(Router);
  const surface = route.queryParamMap.get('surface');
  if (surface !== 'bots' && surface !== 'gallery') {
    return withoutRetiredLens(router.parseUrl(state.url)) ?? true;
  }
  const queryParams: Record<string, string> = {};
  for (const key of route.queryParamMap.keys) {
    if (key === 'surface' || key === RETIRED_LENS_QUERY_PARAM) continue;
    const value = route.queryParamMap.get(key);
    if (value !== null) queryParams[key] = value;
  }
  return router.createUrlTree(['/brokers', 'alpaca', surface], { queryParams });
};
