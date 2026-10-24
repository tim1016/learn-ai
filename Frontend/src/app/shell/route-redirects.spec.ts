import { TestBed } from '@angular/core/testing';
import { NavigationEnd, Router, provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it } from 'vitest';

import { redirectKeepingState } from './route-redirects';

describe('redirectKeepingState', () => {
  let router: Router;
  let ended: string[];

  function configure(redirect: ReturnType<typeof redirectKeepingState>) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'old/:id', redirectTo: redirect, pathMatch: 'full' },
          { path: 'new', children: [] },
          { path: 'new/:id', children: [] },
        ]),
      ],
    });
    router = TestBed.inject(Router);
    ended = [];
    router.events.subscribe((event) => {
      if (event instanceof NavigationEnd) ended.push(event.urlAfterRedirects);
    });
  }

  beforeEach(() => configure(redirectKeepingState(['/new'])));

  it('lands on its destination in one navigation with the query and fragment it was opened with', async () => {
    await router.navigateByUrl('/old/7?a=1&b=2#section');

    expect(ended).toEqual(['/new?a=1&b=2#section']);
  });

  it('builds its destination from the route\'s own params', async () => {
    configure(redirectKeepingState((params) => ['/new', String(params['id'])]));

    await router.navigateByUrl('/old/7?a=1');

    expect(router.url).toBe('/new/7?a=1');
  });

  it('leaves out the keys it is told to drop and keeps the rest', async () => {
    configure(redirectKeepingState(['/new'], { drop: ['lens'] }));

    await router.navigateByUrl('/old/7?lens=operator&deploy=1');

    expect(router.url).toBe('/new?deploy=1');
  });

  it('lets a key it sets win over the same key in the URL', async () => {
    configure(redirectKeepingState(['/new'], { set: (params) => ({ run: String(params['id']) }) }));

    await router.navigateByUrl('/old/7?run=9&tab=history');

    expect(router.url).toBe('/new?run=7&tab=history');
  });
});
