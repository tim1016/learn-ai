import type { RedirectFunction } from '@angular/router';

import { redirectKeepingState } from '../shell/route-redirects';
import { HOME_VIEW_QUERY_PARAM, HOME_WALL_VIEW, accountWorkspaceHomeRoute } from './account-workspace';
import { RETIRED_LENS_QUERY_PARAM } from './retired-lens.guard';

/**
 * Send a retired workspace tab to Home (PRD #2560): Overview, Bots and
 * Gallery became one Home, and Gallery is its Wall view (`?view=wall`).
 *
 * Built from the route's own clerk and account — inherited params, so it
 * works at either depth: an account's `bots`/`gallery` land on that
 * account's Home, and a lane's account-less `bots`/`gallery` on the lane's
 * Home, which explains in place why it cannot open (FR-096).
 *
 * A redirect, not a guard, so the old URL reaches Home in the one navigation
 * the router is already making. Its query and fragment travel with it; the
 * retired `?lens=` does not.
 */
export function homeRedirect(view: 'list' | 'wall'): RedirectFunction {
  return redirectKeepingState(
    (params) =>
      accountWorkspaceHomeRoute({
        broker: 'alpaca',
        clerkId: String(params['clerkId'] ?? ''),
        accountId: params['accountId'] === undefined ? null : String(params['accountId']),
      }),
    {
      drop: [RETIRED_LENS_QUERY_PARAM],
      set: view === 'wall' ? () => ({ [HOME_VIEW_QUERY_PARAM]: HOME_WALL_VIEW }) : undefined,
    },
  );
}
