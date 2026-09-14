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
    expect(store.chartStale()).toBe(false);
  });
});
