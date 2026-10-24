import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { routes } from '../app.routes';
import { routeTreeHasData } from '../shell/current-url.service';
import { OPERATIONS_TABS } from './operations-tabs';

/** Opens an address on the app's real routes, recording every request the page makes. */
async function open(url: string) {
  const requests: { method: string; url: string }[] = [];
  TestBed.configureTestingModule({
    providers: [
      // As the app does: a placeholder tab takes its name from the route's data.
      provideRouter(routes, withComponentInputBinding()),
      provideHttpClient(
        withInterceptors([
          (request, next) => {
            requests.push({ method: request.method, url: request.url });
            return next(request);
          },
        ]),
      ),
      provideHttpClientTesting(),
    ],
  });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  harness.detectChanges();
  return { requests };
}

describe('the Operations place', () => {
  it('opens on the Lake and names every tab once, marking the open one', async () => {
    await open('/operations');

    const links = within(screen.getByRole('navigation', { name: 'Operations' })).getAllByRole('link');

    expect(TestBed.inject(Router).url).toBe('/operations/lake');
    expect(links.map((link) => link.textContent?.trim())).toEqual(OPERATIONS_TABS.map((tab) => tab.label));
    expect(links.map((link) => link.getAttribute('href'))).toEqual(OPERATIONS_TABS.map((tab) => `/operations/${tab.id}`));
    expect(links.filter((link) => link.getAttribute('aria-current') === 'page').map((link) => link.textContent?.trim())).toEqual(['Lake']);
    expect(screen.getByRole('heading', { name: 'Operations', level: 1 })).toBeTruthy();
  });

  it('opens the Pipeline Docs from the Lake in a new tab, so the lake view stays as it is, and offers no document on a tab that has none', async () => {
    await open('/operations/lake');
    const link = screen.getByRole('link', { name: 'How this works' });
    expect(link.getAttribute('href')).toBe('/data-quality-docs');
    expect(link.getAttribute('target')).toBe('_blank');

    TestBed.resetTestingModule();
    await open('/operations/backups');
    expect(screen.queryByRole('link', { name: 'How this works' })).toBeNull();
  });

  it.each([
    ['math-checks', 'Math checks are not in Operations yet.', [['Open Golden Fixtures', '/golden-fixtures']]],
    [
      'data-checks',
      'Data checks are not in Operations yet.',
      [
        ['Open CSV compare', '/data-lab/validate'],
        ['Open Data divergence', '/research-lab/inspect/data-divergence'],
        ['Open Strategy pre-flight', '/research-lab/inspect/pre-flight'],
      ],
    ],
  ])('says the %s tab arrives in the next step and points to where its content is today', async (tab, happened, where) => {
    await open(`/operations/${tab}`);

    expect(screen.getByText(happened)).toBeTruthy();
    expect(screen.getByText('They arrive in the next step of this redesign.')).toBeTruthy();
    for (const [label, href] of where) {
      expect(screen.getByRole('link', { name: label }).getAttribute('href')).toBe(href);
    }
  });

  it('fails in place on a tab it does not have, rather than opening the Lake', async () => {
    await open('/operations/nothing');

    const router = TestBed.inject(Router);
    expect(router.url).toBe('/operations/nothing');
    expect(routeTreeHasData(router.routerState.snapshot.root, 'notFound')).toBe(true);
  });

  // Opening Operations or any tab only reads: a backfill, a backup or a
  // restore starts from an explicit control, never from a visit (PRD #3490 R29).
  it.each([
    ['lake', '/api/data-lake/run-readiness'],
    ['option-archive', '/api/option-archive/status'],
    ['backups', '/api/option-archive/status'],
    ['math-checks', null],
    ['data-checks', null],
  ])('starts nothing when the %s tab opens: every request it makes is a read', async (tab, evidence) => {
    const { requests } = await open(`/operations/${tab}`);

    if (evidence !== null) {
      // The check can fire: the tab did reach for its evidence, and only read it.
      await vi.waitFor(() => expect(requests.some((request) => request.url.endsWith(evidence))).toBe(true));
    }

    expect(requests.filter((request) => request.method !== 'GET')).toEqual([]);
  });
});
