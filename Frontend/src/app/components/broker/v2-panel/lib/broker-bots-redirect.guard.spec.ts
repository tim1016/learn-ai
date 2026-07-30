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
import { brokerBotsRedirectGuard } from './broker-bots-redirect.guard';

describe('brokerBotsRedirectGuard', () => {
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

  it('redirects to scoped bots path on successful account fetch', async () => {
    brokersService.getAccount.mockResolvedValue({ account_id: 'PA9' });
    router.createUrlTree.mockReturnValue(successTree);

    const result = await runGuard('alpaca');

    expect(brokersService.getAccount).toHaveBeenCalledWith('alpaca');
    expect(router.createUrlTree).toHaveBeenCalledWith([
      '/brokers', 'alpaca', 'accounts', 'PA9', 'bots',
    ]);
    expect(result).toBe(successTree);
  });

  it('redirects to broker desk on account fetch failure', async () => {
    brokersService.getAccount.mockRejectedValue(new Error('Network error'));
    router.createUrlTree.mockReturnValue(failureTree);

    const result = await runGuard('alpaca');

    expect(router.createUrlTree).toHaveBeenCalledWith(['/brokers', 'alpaca']);
    expect(result).toBe(failureTree);
  });
});

function runGuard(broker: string) {
  const route = {
    paramMap: convertToParamMap({ broker }),
  } as ActivatedRouteSnapshot;
  return TestBed.runInInjectionContext(() =>
    brokerBotsRedirectGuard(route, {} as RouterStateSnapshot),
  );
}
