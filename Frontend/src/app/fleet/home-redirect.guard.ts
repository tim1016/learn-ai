import { inject } from '@angular/core';
import { type CanActivateFn, Router, UrlTree } from '@angular/router';

import { HOME_VIEW_QUERY_PARAM, HOME_WALL_VIEW, accountWorkspaceHomeRoute } from './account-workspace';

/**
 * Send a retired workspace tab to Home (PRD #2560): Overview, Bots and
 * Gallery became one Home, and Gallery is its Wall view (`?view=wall`).
 *
 * Built from the route's own clerk and account — inherited params, so it
 * works at either depth: an account's `bots`/`gallery` land on that
 * account's Home, and a lane's account-less `bots`/`gallery` on the lane's
 * Home, which explains in place why it cannot open (FR-096). Nothing else
 * the old URL carried travels: `?lens=` is retired with the lens.
 */
export function homeRedirectGuard(view: 'list' | 'wall'): CanActivateFn {
  return (route) =>
    inject(Router).createUrlTree(
      [
        ...accountWorkspaceHomeRoute({
          broker: 'alpaca',
          clerkId: route.paramMap.get('clerkId') ?? '',
          accountId: route.paramMap.get('accountId'),
        }),
      ],
      { queryParams: view === 'wall' ? { [HOME_VIEW_QUERY_PARAM]: HOME_WALL_VIEW } : {} },
    );
}

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
