import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

/** Retire the desk's old `?surface=bots|gallery` bookmark shape.
 *
 * The surface hints became real read-only chooser routes
 * (`/brokers/alpaca/bots`, `/brokers/alpaca/gallery`), so a bookmarked hint
 * URL redirects there and cleans itself up — it never silently degrades to
 * the bare directory the hint used to annotate. Every other query parameter
 * the bookmark carries travels with the redirect, so a co-traveling `lens`
 * or `deploy` intent survives. Any other `surface` value is left alone for
 * the desk to render as-is. */
export const alpacaSurfaceRedirectGuard: CanActivateFn = (route) => {
  const surface = route.queryParamMap.get('surface');
  if (surface !== 'bots' && surface !== 'gallery') return true;
  const queryParams: Record<string, string> = {};
  for (const key of route.queryParamMap.keys) {
    if (key === 'surface') continue;
    const value = route.queryParamMap.get(key);
    if (value !== null) queryParams[key] = value;
  }
  return inject(Router).createUrlTree(['/brokers', 'alpaca', surface], { queryParams });
};
