import { inject } from '@angular/core';
import type { CanActivateFn, ResolveFn } from '@angular/router';
import { Router } from '@angular/router';

import { BotSurfaceStore, type BotSurfaceBootstrap } from './bot-surface-store.service';

export const botExistsGuard: CanActivateFn = async (route) => {
  const store = inject(BotSurfaceStore);
  const router = inject(Router);
  const instanceId = route.paramMap.get('id');
  if (!instanceId) return router.createUrlTree(['/broker/bots']);
  const bootstrap = await store.bootstrapInstance(instanceId);
  return bootstrap.kind === 'missing'
    ? router.createUrlTree(['/broker/bots'], { queryParams: { missing: instanceId } })
    : true;
};

export const botSurfaceResolver: ResolveFn<BotSurfaceBootstrap> = async (route) => {
  const store = inject(BotSurfaceStore);
  const instanceId = route.paramMap.get('id');
  if (!instanceId) {
    return { kind: 'unreachable', message: 'Control plane unreachable. Bot identity is missing.' };
  }
  const bootstrap = await store.bootstrapInstance(instanceId);
  store.connect(instanceId);
  return bootstrap;
};
