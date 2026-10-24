import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { routes } from '../app.routes';
import { routeTreeHasData } from '../shell/current-url.service';
import type { OptionArchiveHealth } from '../shared/option-archive/option-archive.service';
import { formatTimestampDisplay } from '../shared/timestamp/timestamp-display';
import { ScopedHealthService } from '../shared/scoped-health/scoped-health.service';
import { allHealthy, fakeScopedHealth, healthItem, projection } from '../shared/scoped-health/testing/scoped-health-fixtures';
import { DATA_CHECKS } from './data-checks/data-checks';
import { OPERATIONS_TABS } from './operations-tabs';

/** Opens an address on the app's real routes, recording every request the page makes. */
async function open(url: string) {
  const requests: { method: string; url: string }[] = [];
  TestBed.configureTestingModule({
    providers: [
      // As the app does: a routed page takes the scope its link carried from the query.
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
  return { requests, harness };
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

  it('opens Data checks on the TradingView comparison, naming every check once and marking the open one', async () => {
    await open('/operations/data-checks');

    const links = within(screen.getByRole('navigation', { name: 'Data checks' })).getAllByRole('link');

    expect(TestBed.inject(Router).url).toBe('/operations/data-checks/tradingview');
    expect(links.map((link) => link.textContent?.trim())).toEqual(DATA_CHECKS.map((check) => check.label));
    expect(links.map((link) => link.getAttribute('href'))).toEqual(DATA_CHECKS.map((check) => `/operations/data-checks/${check.id}`));
    expect(links.filter((link) => link.getAttribute('aria-current') === 'page').map((link) => link.textContent?.trim())).toEqual(['TradingView comparison']);
  });

  it('starts the TradingView comparison from the symbol an old link carried', async () => {
    await open('/data-quality?ticker=aapl&from=2026-04-15&to=2026-05-15');

    expect(TestBed.inject(Router).url).toBe('/operations/data-checks/tradingview?ticker=aapl&from=2026-04-15&to=2026-05-15');
    expect(screen.getByText("Open AAPL's Options tab").getAttribute('href')).toBe('/markets/AAPL/options/chain?source=archive');
  });

  it('shows a chip per health source in its header, each from the data plane\'s own word for it', async () => {
    const failedBackup = healthItem({
      kind: 'lake_backup',
      severity: 'critical',
      link: { tab: 'backups', query: {}, fragment: 'option-archive-backup' },
    });
    const health = fakeScopedHealth(projection(...allHealthy().items.filter((item) => item.scope.data_kind !== 'lake_backup'), failedBackup));
    TestBed.overrideProvider(ScopedHealthService, { useValue: { read: health.read } });

    await open('/operations/option-archive');

    // The header also draws the menu trail as a list, so wait on the chips' own list.
    const chips = await within(await screen.findByRole('list', { name: 'Data health' })).findAllByRole('listitem', {}, { timeout: 2000 });
    expect(chips.map((chip) => [...chip.querySelectorAll('.health-chips__chip > span')].map((part) => part.textContent?.trim()).join(' '))).toEqual([
      'Lake OK',
      'Option archive OK',
      'Backups Failing',
      'IBKR feed OK',
    ]);
    expect(within(chips[2]).getByRole('link').getAttribute('href')).toBe('/operations/backups#option-archive-backup');
    expect(health.read).toHaveBeenCalledWith({});
  });

  it('reads the archive status again each time its tabs open, and dates what it shows', async () => {
    const firstReadMs = 1_792_558_800_000;
    const laterReadMs = firstReadMs + 3_600_000;
    const status = (generatedMs: number): OptionArchiveHealth => ({
      enabled: true,
      generated_ms: generatedMs,
      underlyings: [],
      backup: { severity: 'healthy', reason: 'Generation checked.' },
      test_restore: { severity: 'healthy', reason: 'Restored and decoded.' },
    });
    const { harness } = await open('/operations/backups');
    const http = TestBed.inject(HttpTestingController);
    await vi.waitFor(() => http.expectOne('/api/option-archive/status').flush(status(firstReadMs)));
    expect((await screen.findByText(/Status read/)).textContent).toContain(formatTimestampDisplay(firstReadMs, { mode: 'local' }));

    await harness.navigateByUrl('/operations/option-archive');

    // The Option archive tab asked again: it does not present the Backups visit's read as current.
    await vi.waitFor(() => http.expectOne('/api/option-archive/status').flush(status(laterReadMs)));
    await vi.waitFor(() =>
      expect(screen.getByText(/Status read/).textContent).toContain(formatTimestampDisplay(laterReadMs, { mode: 'local' })),
    );
  });

  it('fails in place on a tab it does not have, rather than opening the Lake', async () => {
    await open('/operations/nothing');

    const router = TestBed.inject(Router);
    expect(router.url).toBe('/operations/nothing');
    expect(routeTreeHasData(router.routerState.snapshot.root, 'notFound')).toBe(true);
  });

  // Opening Operations or any tab only reads: a backfill, a backup, a restore
  // or a fixture run starts from an explicit control, never from a visit (PRD
  // #3490 R29).
  it.each([
    ['lake', '/api/data-lake/run-readiness'],
    ['option-archive', '/api/option-archive/status'],
    ['backups', '/api/option-archive/status'],
    ['math-checks', '/api/golden-fixtures'],
    ['data-checks/tradingview', '/api/tickers/catalog'],
    ['data-checks/divergence', '/research/data-divergence/matrix/15m'],
    ['data-checks/pre-flight', '/api/tickers/catalog'],
  ])('starts nothing when the %s tab opens: every request it makes is a read', async (tab, evidence) => {
    const { requests } = await open(`/operations/${tab}`);

    // The check can fire: the tab did reach for its evidence, and only read it.
    await vi.waitFor(() => expect(requests.some((request) => request.url.endsWith(evidence))).toBe(true));

    expect(requests.filter((request) => request.method !== 'GET')).toEqual([]);
  });
});
