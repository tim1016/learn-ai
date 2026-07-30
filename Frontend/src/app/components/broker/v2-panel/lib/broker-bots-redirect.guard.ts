import { inject } from '@angular/core';
import type { CanActivateFn } from '@angular/router';
import { Router } from '@angular/router';

import { BrokersService } from '../../../../services/brokers.service';

/**
 * Resolves the account id for a broker then issues a UrlTree redirect to the
 * account-scoped bots list: `/brokers/:broker/accounts/:accountId/bots`.
 *
 * On fetch failure redirects to the broker desk (`/brokers/:broker`) so the
 * desk can render account errors natively rather than spinning silently.
 */
export const brokerBotsRedirectGuard: CanActivateFn = async (route) => {
  const brokersService = inject(BrokersService);
  const router = inject(Router);

  const broker = route.paramMap.get('broker') ?? 'alpaca';

  try {
    const account = await brokersService.getAccount(broker);
    return router.createUrlTree(['/brokers', broker, 'accounts', account.account_id, 'bots']);
  } catch {
    return router.createUrlTree(['/brokers', broker]);
  }
};
