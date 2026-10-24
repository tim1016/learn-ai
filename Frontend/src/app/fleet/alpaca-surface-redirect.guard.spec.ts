import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { NavigationEnd, provideRouter, Router } from '@angular/router';
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
          { path: 'brokers/alpaca', canActivate: [alpacaSurfaceRedirectGuard], component: RouteStubComponent },
        ]),
      ],
    });
    router = TestBed.inject(Router);
  });

  async function landed(url: string): Promise<string[]> {
    const visited: string[] = [];
    router.events.subscribe((event) => {
      if (event instanceof NavigationEnd) visited.push(event.urlAfterRedirects);
    });
    await router.navigateByUrl(url);
    return visited;
  }

  it.each(['bots', 'gallery'])(
    'sends the retired ?surface=%s bookmark straight to the account list, never through a chooser page',
    async (surface) => {
      expect(await landed(`/brokers/alpaca?surface=${surface}`)).toEqual(['/brokers/alpaca']);
    },
  );

  it.each([
    [
      'carries the deploy intent, its strategy, a qualification handoff and the fragment through the redirect, never the retired lens',
      '/brokers/alpaca?surface=bots&deploy=&strategy=momentum&golden_qualification=abc&lens=operator#start',
      '/brokers/alpaca?deploy=&strategy=momentum&golden_qualification=abc#start',
    ],
    ['drops a retired ?lens= from the list URL it otherwise leaves alone', '/brokers/alpaca?lens=operator&deploy=', '/brokers/alpaca?deploy='],
  ])('%s', async (_name, url, expected) => {
    const visited = await landed(url);

    expect(visited).toEqual([expected]);
  });

  it('leaves every other list URL alone, including unknown surface values', async () => {
    expect(await landed('/brokers/alpaca?deploy=')).toEqual(['/brokers/alpaca?deploy=']);
    expect(await landed('/brokers/alpaca?surface=nonsense')).toEqual(['/brokers/alpaca?surface=nonsense']);
  });
});
