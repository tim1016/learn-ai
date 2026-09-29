import { inject } from '@angular/core';
import { type CanActivateFn, Router, UrlTree } from '@angular/router';

/** The query parameter the retired Trader/Operator lens switch wrote. */
export const RETIRED_LENS_QUERY_PARAM = 'lens';

/**
 * `tree` without the retired `?lens=` (PRD #2560 D2: each page has one view),
 * or `null` when it carries none. Everything else in the URL — its path, its
 * other query parameters and its fragment — is kept, so an old link lands on
 * the page it always named.
 */
export function withoutRetiredLens(tree: UrlTree): UrlTree | null {
  if (!(RETIRED_LENS_QUERY_PARAM in tree.queryParams)) return null;
  const { [RETIRED_LENS_QUERY_PARAM]: _retired, ...queryParams } = tree.queryParams;
  return new UrlTree(tree.root, queryParams, tree.fragment);
}

/** Drop a retired `?lens=` from any account-workspace URL an old bookmark or
 * link opens, landing on the same page without it. */
export const dropRetiredLensGuard: CanActivateFn = (_route, state) => {
  const router = inject(Router);
  return withoutRetiredLens(router.parseUrl(state.url)) ?? true;
};
