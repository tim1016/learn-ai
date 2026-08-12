import { inject } from '@angular/core';
import type { CanActivateFn } from '@angular/router';
import { Router } from '@angular/router';

import { BrokersService } from '../../../../services/brokers.service';

type AccountScopedSurface = 'bots' | 'gallery';

/**
 * Resolves a broker's account id before sending an unscoped broker surface to
 * its account-scoped counterpart. On fetch failure, return to the broker desk
 * so it can render the account error instead of leaving a route in limbo.
 */
function brokerAccountSurfaceRedirectGuard(surface: AccountScopedSurface): CanActivateFn {
  return async (route) => {
    const brokersService = inject(BrokersService);
    const router = inject(Router);
    const broker = route.paramMap.get('broker') ?? 'alpaca';

    try {
      const account = await brokersService.getAccount(broker);
      return router.createUrlTree(['/brokers', broker, 'accounts', account.account_id, surface]);
    } catch {
      return router.createUrlTree(['/brokers', broker]);
    }
  };
}

export const brokerBotsRedirectGuard = brokerAccountSurfaceRedirectGuard('bots');
export const brokerGalleryRedirectGuard = brokerAccountSurfaceRedirectGuard('gallery');
