import { inject } from '@angular/core';
import { type CanActivateFn, Router } from '@angular/router';

import { HOME_VIEW_QUERY_PARAM, HOME_WALL_VIEW, accountWorkspaceHomeRoute } from './account-workspace';

/**
 * Send a retired workspace tab to Home (PRD #2560): Overview, Bots and
 * Gallery became one Home, and Gallery is its Wall view (`?view=wall`).
 *
 * Built from the route's own clerk and account — inherited params, so it
 * works at either depth: an account's `bots`/`gallery` land on that
 * account's Home, and a lane's account-less `bots`/`gallery` on the lane's
 * Home, which explains in place why it cannot open (FR-096). Nothing else
 * the old URL carried travels, the retired `?lens=` included.
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
