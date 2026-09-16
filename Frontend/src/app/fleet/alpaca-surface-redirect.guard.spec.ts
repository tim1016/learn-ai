import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { beforeEach, describe, expect, it } from 'vitest';

import { alpacaSurfaceRedirectGuard } from './alpaca-surface-redirect.guard';

@Component({ template: '' })
class RouteStubComponent {}

describe('alpacaSurfaceRedirectGuard', () => {
  let router: Router;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'desk', canActivate: [alpacaSurfaceRedirectGuard], component: RouteStubComponent },
          { path: 'brokers/alpaca/bots', component: RouteStubComponent },
          { path: 'brokers/alpaca/gallery', component: RouteStubComponent },
        ]),
      ],
    });
    router = TestBed.inject(Router);
  });

  it('redirects the retired surface hint bookmarks to the real chooser routes', async () => {
    await router.navigateByUrl('/desk?surface=bots');
    expect(router.url).toBe('/brokers/alpaca/bots');

    await router.navigateByUrl('/desk?surface=gallery');
    expect(router.url).toBe('/brokers/alpaca/gallery');
  });

  it('carries co-traveling query parameters through the redirect', async () => {
    await router.navigateByUrl('/desk?surface=bots&lens=operator');
    expect(router.url).toBe('/brokers/alpaca/bots?lens=operator');
  });

  it('leaves every other desk URL alone, including unknown surface values', async () => {
    await router.navigateByUrl('/desk?deploy=');
    expect(router.url).toBe('/desk?deploy=');

    await router.navigateByUrl('/desk?surface=nonsense');
    expect(router.url).toBe('/desk?surface=nonsense');
  });
});
