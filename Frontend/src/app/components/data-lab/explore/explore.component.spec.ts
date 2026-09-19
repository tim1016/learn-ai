import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { ExploreComponent } from './explore.component';
import { createDataLabWorkspaceStore, DataLabWorkspaceStore } from '../data-lab-workspace-store';

const WINDOW = {
  startMsUtc: Date.UTC(2026, 3, 15),
  endMsUtc: Date.UTC(2026, 4, 15),
};

const quality = {
  gaps_found: 0,
  duplicates_removed: 0,
  missing_sessions: 0,
  synthetic_bars: 0,
};

function chartResponse() {
  return {
    bars: [],
    indicators: [],
    quality,
    bar_sources: null,
    allowed_timeframes: ['1m', '5m', '1D'],
    estimated_bars_per_timeframe: {},
    recommended_timeframe: '1D',
  };
}

async function renderExplore() {
  const result = await render(ExploreComponent, {
    providers: [
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
    ],
  });
  const http = TestBed.inject(HttpTestingController);
  const store = result.fixture.componentInstance.store;
  return { ...result, http, store };
}

function flushCatalog(http: HttpTestingController): void {
  const req = http.expectOne(`${environment.pythonServiceUrl}/api/dataset/available`);
  expect(req.request.method).toBe('GET');
  req.flush({ success: true, categories: {}, total: 0 });
}

function throwChartRequestMissing(): never {
  throw new Error('expected exactly one chart request');
}

describe('ExploreComponent', () => {
  it('marks the chart stale on edits without issuing any chart HTTP request', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);

    // Scope edit → commit marks stale (PRD §7.3 / FR-003).
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    expect(store.chartStale()).toBe(true);

    // Recipe edit → still stale.
    store.addIndicator('rsi', { length: 14 });
    fixture.detectChanges();
    expect(store.chartStale()).toBe(true);

    // Zero chart requests — and no other surprise requests either.
    http.verify();
  });

  it('issues exactly one chart request on an explicit refresh', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Refresh chart' }));
    // http.match() consumes the matched requests out of the open list (as
    // does expectOne), so count first to prove exactly one chart call —
    // no auto-refetch alongside the explicit refresh — then assert on it.
    const req = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(req.request.method).toBe('POST');
    // The one-path mapper payload: numeric window + derived UTC dates.
    expect(req.request.body['start_ms_utc']).toBe(WINDOW.startMsUtc);
    expect(req.request.body['end_ms_utc']).toBe(WINDOW.endMsUtc);
    expect(req.request.body['ticker']).toBe('SPY');
    // The store's default session is `regular`; the wire vocabulary is `rth`.
    expect(req.request.body['session']).toBe('rth');
    // Stale stays set until the fetch settles — not when it is issued.
    expect(store.chartStale()).toBe(true);
    expect(fixture.componentInstance.chartRefreshing()).toBe(true);
    req.flush(chartResponse());
    await waitFor(() => expect(store.chartStale()).toBe(false));
    await waitFor(() => expect(fixture.componentInstance.chartRefreshing()).toBe(false));

    http.verify();
  });

  it('shows the Out of date badge while stale and keeps the last chart mounted', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Refresh chart' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/chart/data`),
    );
    req.flush(chartResponse());
    await waitFor(() => expect(store.chartStale()).toBe(false));
    expect(screen.queryByText('Out of date')).toBeNull();

    // An edit after a good refresh: badge appears, chart stays mounted.
    store.addIndicator('atr', { length: 14 });
    fixture.detectChanges();
    expect(screen.getAllByText('Out of date').length).toBeGreaterThan(0);
    expect(document.querySelector('app-data-lab-chart')).not.toBeNull();
    http.verify();
  });

  it('loads news lazily only when the headlines section is expanded', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    fixture.detectChanges();

    // Collapsed section never fetches (FR-010).
    http.verify();

    await userEvent.click(screen.getByRole('button', { name: /Headlines for SPY/ }));
    // Match by URL path — the window rides as query params, asserted below.
    const req = await waitFor(() =>
      http.expectOne(
        r => r.method === 'GET' && r.url === `${environment.pythonServiceUrl}/api/news`,
      ),
    );
    expect(req.request.params.get('published_utc_gte')).toBe(
      new Date(WINDOW.startMsUtc).toISOString(),
    );
    expect(req.request.params.get('published_utc_lt')).toBe(
      new Date(WINDOW.endMsUtc).toISOString(),
    );
    req.flush({
      articles: [
        { id: 'n1', title: 'First headline', published_utc_ms: WINDOW.endMsUtc - 1000 },
      ],
      count: 1,
      fetched_at_ms: WINDOW.endMsUtc,
    });
    expect(await screen.findByText('First headline')).toBeTruthy();
    http.verify();
  });

  // ── Auto-load + shell refresh requests (2026-09-13) ─────────
  it('auto-loads the chart on mount when a committed scope exists', async () => {
    const store = createDataLabWorkspaceStore();
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    const result = await render(ExploreComponent, {
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: DataLabWorkspaceStore, useValue: store },
      ],
    });
    const http = TestBed.inject(HttpTestingController);
    flushCatalog(http);
    result.fixture.detectChanges();

    const req = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(req.request.body['ticker']).toBe('SPY');
    req.flush(chartResponse());
    http.verify();
  });

  it('does not auto-load when a restored snapshot renders cached bars instead', async () => {
    const store = createDataLabWorkspaceStore();
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    store.setRestoredChartSnapshot({
      bars: [],
      indicators: [],
      quality,
      allowedTimeframes: ['1D'],
      estimatedBarsPerTimeframe: {},
      recommendedTimeframe: '1D',
      visibleIndicatorIds: [],
      timeframe: '1D',
    });
    const result = await render(ExploreComponent, {
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: DataLabWorkspaceStore, useValue: store },
      ],
    });
    const http = TestBed.inject(HttpTestingController);
    flushCatalog(http);
    result.fixture.detectChanges();

    expect(http.match(`${environment.pythonServiceUrl}/api/chart/data`)).toHaveLength(0);
    http.verify();
  });

  it('fetches when the shell requests a refresh for the committed scope', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    fixture.detectChanges();
    expect(http.match(`${environment.pythonServiceUrl}/api/chart/data`)).toHaveLength(0);

    // The shell's only chart lever: a refresh request through the store.
    store.requestChartRefresh();
    const req = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    req.flush(chartResponse());
    http.verify();
  });

  it('auto-corrects a rejected timeframe and re-fetches once with the recommendation', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW, timespan: 'minute', multiplier: 1 });
    store.commitScope();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Refresh chart' }));
    const first = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(first.request.body['timeframe']).toBe('1m');
    first.flush(
      {
        detail: {
          error_code: 'TIMEFRAME_NOT_ALLOWED',
          detail: "Timeframe '1m' would produce ~90000 bars (max 20000).",
          recommended_timeframe: '1D',
          allowed_timeframes: ['1D'],
        },
      },
      { status: 400, statusText: 'Bad Request' },
    );

    // The parent commits the recommendation and issues exactly one recovery
    // fetch — through the store request, serialized after the settle.
    // (http.match consumed the first request out of the open list, so the
    // recovery fetch is the only request left open.)
    const second = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(second.request.body['timeframe']).toBe('1D');
    second.flush(chartResponse());
    http.verify();
    // Stale clears only when the recovery fetch's settle lands (a failed
    // fetch keeps the badge), so wait for the settle rather than asserting
    // synchronously.
    await waitFor(() => expect(store.chartStale()).toBe(false));
  });

  // ── Review-fix regressions (2026-09-14) ─────────────────────
  it('applies the Edit-scope drawer through the store and requests a refresh', async () => {
    const { http, fixture } = await renderExplore();
    flushCatalog(http);
    fixture.detectChanges();

    // The drawer's own picker state, set as its Apply would hand it over.
    fixture.componentInstance.scopeRange.set({
      symbol: 'SPY',
      from: '2026-04-15',
      to: '2026-05-15',
      resolution: 'minute',
      autoFetch: false,
    });
    fixture.componentInstance.applyScopeDrawer();

    const req = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(req.request.body['ticker']).toBe('SPY');
    // The TO date anchors at its UTC day's end — a half-open reader gets a
    // non-degenerate single-day window, the chart the same trading date.
    expect(req.request.body['end_ms_utc']).toBe(Date.UTC(2026, 4, 15) + 86_400_000 - 1);
    req.flush(chartResponse());
    http.verify();
  });

  it('discards a timeframe rejection that outlived its request\'s scope', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW, timespan: 'minute', multiplier: 5 });
    store.commitScope();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Refresh chart' }));
    const first = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(first.request.body['timeframe']).toBe('5m');

    // Mid-flight: a preset/Apply lands a NEW committed scope at a different
    // timeframe, queuing its own refresh behind the in-flight request.
    store.patchDraft({
      window: { startMsUtc: WINDOW.startMsUtc, endMsUtc: WINDOW.endMsUtc + 86_400_000 },
      timespan: 'minute',
      multiplier: 1,
    });
    store.commitScope();
    store.requestChartRefresh();

    first.flush(
      {
        detail: {
          error_code: 'TIMEFRAME_NOT_ALLOWED',
          detail: "Timeframe '5m' would produce ~90000 bars (max 20000).",
          recommended_timeframe: '30m',
          allowed_timeframes: ['30m', '1D'],
        },
      },
      { status: 400, statusText: 'Bad Request' },
    );

    // The old request's recommendation must not hijack the new scope: the
    // queued refresh re-fetches with the operator's committed 1m timeframe.
    const second = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    expect(second.request.body['timeframe']).toBe('1m');
    expect(second.request.body['end_ms_utc']).toBe(WINDOW.endMsUtc + 86_400_000);
    expect(store.draft().multiplier).toBe(1);
    second.flush(chartResponse());
    http.verify();
  });

  it('keeps the Out of date badge when a fetch fails — nothing settled as current', async () => {
    const { http, store, fixture } = await renderExplore();
    flushCatalog(http);
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Refresh chart' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/chart/data`),
    );
    req.flush(
      { detail: { error_code: 'NO_DATA', detail: 'No data for SPY in this range' } },
      { status: 400, statusText: 'Bad Request' },
    );

    await waitFor(() => expect(fixture.componentInstance.chartRefreshing()).toBe(false));
    // A failed fetch must leave the chart visibly out of date, not reading
    // as current — the stale flag survives the settle.
    expect(store.chartStale()).toBe(true);
    expect(screen.getAllByText('Out of date').length).toBeGreaterThan(0);
    http.verify();
  });

  it('remounting with a queued refresh issues exactly one fetch, not two', async () => {
    // A preset clicked while Explore was unmounted bumps the counter; on
    // return, the mount auto-load fetches the newest committed scope and the
    // historical tick must not replay a duplicate.
    const store = createDataLabWorkspaceStore();
    store.patchDraft({ ticker: 'SPY', window: WINDOW });
    store.commitScope();
    store.requestChartRefresh();
    const result = await render(ExploreComponent, {
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: DataLabWorkspaceStore, useValue: store },
      ],
    });
    const http = TestBed.inject(HttpTestingController);
    flushCatalog(http);
    result.fixture.detectChanges();

    const req = await waitFor(() => {
      const matches = http.match(`${environment.pythonServiceUrl}/api/chart/data`);
      expect(matches).toHaveLength(1);
      return matches[0] ?? throwChartRequestMissing();
    });
    req.flush(chartResponse());
    await waitFor(() => expect(result.fixture.componentInstance.chartRefreshing()).toBe(false));
    // No duplicate request followed the settle — the tick was consumed at
    // construction, not left pending.
    expect(http.match(`${environment.pythonServiceUrl}/api/chart/data`)).toHaveLength(0);
    http.verify();
  });

  it('says so in the Indicators drawer when the catalog fails to load', async () => {
    const { http, fixture } = await renderExplore();
    http
      .expectOne(`${environment.pythonServiceUrl}/api/dataset/available`)
      .flush('boom', { status: 500, statusText: 'Internal Server Error' });
    await fixture.whenStable();

    await userEvent.click(screen.getByRole('button', { name: 'Indicators' }));
    const drawer = screen.getByRole('region', { name: 'Indicators' });

    await waitFor(() =>
      expect(drawer.querySelector('[role="alert"]')?.textContent).toContain(
        'Indicators could not be loaded.',
      ),
    );
    expect(drawer.textContent).not.toContain('No indicators available');
    http.verify();
  });

  it('shows no catalog failure in the Indicators drawer when the catalog loads', async () => {
    const { http } = await renderExplore();
    flushCatalog(http);

    await userEvent.click(screen.getByRole('button', { name: 'Indicators' }));
    const drawer = screen.getByRole('region', { name: 'Indicators' });

    expect(drawer.querySelector('[role="alert"]')).toBeNull();
    expect(drawer.textContent).not.toContain('Indicators could not be loaded.');
    http.verify();
  });
});
