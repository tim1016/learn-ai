import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  Router,
  RouterStateSnapshot,
  UrlTree,
  convertToParamMap,
} from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BotSurfaceStore, type BotSurfaceBootstrap } from './bot-surface-store.service';
import { botExistsGuard, botSurfaceResolver } from './bot-surface-routing';

describe('bot surface route bootstrap', () => {
  const store = {
    bootstrapInstance: vi.fn<(id: string) => Promise<BotSurfaceBootstrap>>(),
    connect: vi.fn<(id: string) => void>(),
  };
  const missingTree = new UrlTree();
  const router = {
    createUrlTree: vi.fn(() => missingTree),
  };

  beforeEach(() => {
    store.bootstrapInstance.mockReset();
    store.connect.mockReset();
    router.createUrlTree.mockClear();
    TestBed.configureTestingModule({
      providers: [
        { provide: BotSurfaceStore, useValue: store },
        { provide: Router, useValue: router },
      ],
    });
  });

  it.each([404, 410] as const)('rejects a bot proven absent with HTTP %s', async (status) => {
    store.bootstrapInstance.mockResolvedValue({ kind: 'missing', status });

    const result = await runGuard('sid-missing');

    expect(result).toBe(missingTree);
    expect(router.createUrlTree).toHaveBeenCalledWith(['/broker/bots'], {
      queryParams: { missing: 'sid-missing' },
    });
  });

  it('soft-fails transport outage so the unreachable page can render', async () => {
    store.bootstrapInstance.mockResolvedValue({
      kind: 'unreachable',
      message: 'Control plane unreachable.',
    });

    expect(await runGuard('sid-unknown')).toBe(true);
    expect(router.createUrlTree).not.toHaveBeenCalled();
  });

  it('reuses bootstrap in the resolver and starts the route-scoped stream', async () => {
    const bootstrap: BotSurfaceBootstrap = {
      kind: 'unreachable',
      message: 'Control plane unreachable.',
    };
    store.bootstrapInstance.mockResolvedValue(bootstrap);

    const result = await TestBed.runInInjectionContext(() =>
      botSurfaceResolver(route('sid-x'), {} as RouterStateSnapshot),
    );

    expect(result).toBe(bootstrap);
    expect(store.connect).toHaveBeenCalledWith('sid-x');
  });
});

function runGuard(id: string) {
  return TestBed.runInInjectionContext(() =>
    botExistsGuard(route(id), {} as RouterStateSnapshot),
  );
}

function route(id: string): ActivatedRouteSnapshot {
  return { paramMap: convertToParamMap({ id }) } as ActivatedRouteSnapshot;
}
