import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  Router,
  RouterStateSnapshot,
  UrlTree,
  convertToParamMap,
} from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../../services/brokers.service';
import {
  brokerBotsRedirectGuard,
  brokerGalleryRedirectGuard,
} from './broker-bots-redirect.guard';

describe('broker account surface redirect guards', () => {
  const successTree = new UrlTree();
  const failureTree = new UrlTree();
  const router = {
    createUrlTree: vi.fn(),
  };
  const brokersService = {
    getAccount: vi.fn<(broker: string) => Promise<{ account_id: string } & Record<string, unknown>>>(),
  };

  beforeEach(() => {
    brokersService.getAccount.mockReset();
    router.createUrlTree.mockReset();

    TestBed.configureTestingModule({
      providers: [
        { provide: BrokersService, useValue: brokersService },
        { provide: Router, useValue: router },
      ],
    });
  });

  it.each([
    ['bots', brokerBotsRedirectGuard],
    ['gallery', brokerGalleryRedirectGuard],
  ] as const)('redirects the unscoped %s route after a successful account fetch', async (surface, guard) => {
    brokersService.getAccount.mockResolvedValue({ account_id: 'PA9' });
    router.createUrlTree.mockReturnValue(successTree);

    const result = await runGuard(guard, 'alpaca');

    expect(brokersService.getAccount).toHaveBeenCalledWith('alpaca');
    expect(router.createUrlTree).toHaveBeenCalledWith([
      '/brokers', 'alpaca', 'accounts', 'PA9', surface,
    ]);
    expect(result).toBe(successTree);
  });

  it('redirects to broker desk on account fetch failure', async () => {
    brokersService.getAccount.mockRejectedValue(new Error('Network error'));
    router.createUrlTree.mockReturnValue(failureTree);

    const result = await runGuard(brokerBotsRedirectGuard, 'alpaca');

    expect(router.createUrlTree).toHaveBeenCalledWith(['/brokers', 'alpaca']);
    expect(result).toBe(failureTree);
  });
});

function runGuard(guard: typeof brokerBotsRedirectGuard, broker: string) {
  const route = {
    paramMap: convertToParamMap({ broker }),
  } as ActivatedRouteSnapshot;
  return TestBed.runInInjectionContext(() =>
    guard(route, {} as RouterStateSnapshot),
  );
}
