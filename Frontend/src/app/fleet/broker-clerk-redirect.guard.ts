import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

import { FleetDirectoryService } from './fleet-directory.service';
import { laneIsReady } from './fleet-directory.types';

/** Redirect an unscoped broker route to its clerk-scoped canonical URL.
 *
 * The lane is resolved from the fleet directory by the account the old URL
 * names. Compatibility has no implicit lane: unknown, retired, inaccessible,
 * wrong-provider, and cold-load failures render the compatibility URL's
 * explicit failure state instead of silently retargeting an operator. */
export function brokerClerkRedirectGuard(
  suffix: '' | `/bots` | `/bots/:sid` | `/gallery`,
): CanActivateFn {
  return async (route) => {
    const fleet = inject(FleetDirectoryService);
    const router = inject(Router);
    const broker = route.paramMap.get('broker');
    const accountId = route.paramMap.get('accountId');
    const sid = route.paramMap.get('sid');
    try {
      await fleet.ensureLoaded();
    } catch (error: unknown) {
      // `FleetDirectoryService` retains the failure for the in-place error
      // surface. Allowing that surface to render is the deliberate fail-closed
      // outcome; the rejected lookup never becomes a different lane.
      void error;
      return true;
    }

    // Canonical clerk routes exist only for the supported Alpaca product. A
    // compatibility link without its original account cannot choose a lane.
    if (broker !== 'alpaca' || accountId === null) {
      return true;
    }
    const lane = fleet.laneForAccount(broker, accountId);
    if (lane === undefined || !laneIsReady(lane)) {
      return true;
    }
    const segments: string[] = ['/brokers', broker, 'clerks', lane.clerk_id];
    if (accountId !== null) {
      segments.push('accounts', accountId);
    }
    if (suffix === '/bots' || suffix === '/bots/:sid') {
      segments.push('bots');
      if (suffix === '/bots/:sid' && sid !== null) {
        segments.push(sid);
      }
    } else if (suffix === '/gallery') {
      segments.push('gallery');
    }
    return router.createUrlTree(segments);
  };
}
