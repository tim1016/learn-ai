import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

import { RETIRED_LENS_QUERY_PARAM, withoutRetiredLens } from './retired-lens.guard';

/** Retire the desk's old `?surface=bots|gallery` bookmark shape, and the
 * retired `?lens=` (PRD #2560 D2).
 *
 * The broker-wide Bots and Gallery choosers are gone (ADR 0064 Decision 2):
 * the account list is where an account is chosen, so a bookmarked hint URL
 * goes straight there in one redirect and cleans itself up. Every other query
 * parameter and the fragment travel with it, so a co-traveling `deploy`
 * intent, the `strategy` it names and a `golden_qualification` handoff survive
 * to the list; a `lens` never does. Any other `surface` value is left alone
 * for the desk to render as-is. */
export const alpacaSurfaceRedirectGuard: CanActivateFn = (route, state) => {
  const router = inject(Router);
  const surface = route.queryParamMap.get('surface');
  if (surface !== 'bots' && surface !== 'gallery') {
    return withoutRetiredLens(router.parseUrl(state.url)) ?? true;
  }
  const { surface: _surface, [RETIRED_LENS_QUERY_PARAM]: _lens, ...queryParams } = router.parseUrl(state.url).queryParams;
  return router.createUrlTree(['/brokers', 'alpaca'], {
    queryParams,
    fragment: route.fragment ?? undefined,
  });
};
