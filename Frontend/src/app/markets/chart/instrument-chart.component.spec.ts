import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import { createDataLabWorkspaceStore, DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { provideFakeDateWorld } from '../../shared/date-picker/testing/fake-trading-calendar';
import { InstrumentContextService } from '../instrument-context.service';
import { InstrumentChartComponent } from './instrument-chart.component';

const CHART_URL = `${environment.pythonServiceUrl}/api/chart/data`;

const WINDOW = {
  startMsUtc: Date.UTC(2026, 3, 15),
  endMsUtc: Date.UTC(2026, 4, 15) + 86_400_000 - 1,
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

/** A store that has committed SPY over the window, the way the place's context does from its address. */
function committedStore() {
  const store = createDataLabWorkspaceStore();
  store.patchDraft({ ticker: 'SPY', window: WINDOW });
  store.commitScope();
  return store;
}

/** The place's context as the chart reads it: the settings the address names, and the commit a rejected timeframe uses. */
function fakeContext(settings: { adjustment?: string; forwardFill?: boolean } = {}) {
  return {
    commitTimeframe: vi.fn(() => true),
    shown: signal({ adjustment: 'polygon_split_adjusted', forwardFill: true, ...settings }),
  };
}

function providersFor(store: DataLabWorkspaceStore, context = fakeContext()) {
  return [
    provideRouter([]),
    ...provideFakeDateWorld(),
    provideHttpClient(),
    provideHttpClientTesting(),
    { provide: DataLabWorkspaceStore, useValue: store },
    { provide: InstrumentContextService, useValue: context },
  ];
}

async function renderChart(store = committedStore(), context = fakeContext()) {
  const result = await render(InstrumentChartComponent, { providers: providersFor(store, context) });
  const http = TestBed.inject(HttpTestingController);
  return { ...result, http, store, context };
}

function flushCatalog(http: HttpTestingController): void {
  http.expectOne(`${environment.pythonServiceUrl}/api/dataset/available`).flush({ success: true, categories: {}, total: 0 });
}

/** Exactly the open chart requests; the test names how many it expects. */
async function chartRequests(http: HttpTestingController, count: number): Promise<TestRequest[]> {
  return waitFor(() => {
    const matches = http.match(CHART_URL);
    expect(matches).toHaveLength(count);
    return matches;
  });
}

describe('InstrumentChartComponent', () => {
  it('loads the committed context once, with its numeric window, and asks for nothing else', async () => {
    const { http } = await renderChart();
    flushCatalog(http);

    const [request] = await chartRequests(http, 1);
    expect(request.request.method).toBe('POST');
    // The one-path mapper payload: numeric window + derived UTC dates.
    expect(request.request.body['ticker']).toBe('SPY');
    expect(request.request.body['start_ms_utc']).toBe(WINDOW.startMsUtc);
    expect(request.request.body['end_ms_utc']).toBe(WINDOW.endMsUtc);
    // The store's default session is `regular`; the wire vocabulary is `rth`.
    expect(request.request.body['session']).toBe('rth');
    request.flush(chartResponse());
    http.verify();
  });

  it('does nothing when the same context is committed again', async () => {
    const { http, store, fixture } = await renderChart();
    flushCatalog(http);
    const [first] = await chartRequests(http, 1);
    first.flush(chartResponse());
    await waitFor(() => expect(store.chartStale()).toBe(false));

    // The same scope, committed again, as a repeated emission of the pickers would.
    store.patchDraft({ ticker: 'SPY', window: { ...WINDOW } });
    store.commitScope();
    fixture.detectChanges();
    await new Promise((resolve) => setTimeout(resolve));

    http.verify();
  });

  it('loads a distinct committed window once and ends the request it replaces', async () => {
    const { http, store, fixture } = await renderChart();
    flushCatalog(http);
    const [first] = await chartRequests(http, 1);

    store.patchDraft({ window: { startMsUtc: WINDOW.startMsUtc, endMsUtc: WINDOW.endMsUtc + 86_400_000 } });
    store.commitScope();
    fixture.detectChanges();

    const [second] = await chartRequests(http, 1);
    expect(first.cancelled).toBe(true);
    expect(second.request.body['end_ms_utc']).toBe(WINDOW.endMsUtc + 86_400_000);
    second.flush(chartResponse());
    http.verify();
  });

  it('loads again when the indicator recipe changes, since the recipe is part of the request', async () => {
    const { http, store, fixture } = await renderChart();
    flushCatalog(http);
    const [first] = await chartRequests(http, 1);
    first.flush(chartResponse());
    await waitFor(() => expect(store.chartStale()).toBe(false));

    store.addIndicator('atr', { length: 14 });
    fixture.detectChanges();

    const [second] = await chartRequests(http, 1);
    expect(second.request.body['indicators']).toContainEqual({ name: 'atr', params: { length: 14 } });
    second.flush(chartResponse());
    http.verify();
  });

  it('shows the answer it already holds, without asking again, when it is reopened with the context unchanged', async () => {
    const store = committedStore();
    const first = await renderChart(store);
    flushCatalog(first.http);
    const [request] = await chartRequests(first.http, 1);
    request.flush({ ...chartResponse(), quality: { ...quality, gaps_found: 2 } });
    await waitFor(() => expect(store.chartStale()).toBe(false));
    first.fixture.destroy();
    TestBed.resetTestingModule();

    const second = await renderChart(store);
    flushCatalog(second.http);
    await new Promise((resolve) => setTimeout(resolve));

    expect(second.http.match(CHART_URL)).toHaveLength(0);
    expect(await screen.findByText(/Quality issues found: 2 total/)).toBeTruthy();
    second.http.verify();
  });

  it('leaves no chart behind a failed load: the error stands and nothing reads as current', async () => {
    const { http, store, fixture } = await renderChart();
    flushCatalog(http);
    const [request] = await chartRequests(http, 1);
    request.flush(
      { detail: { error_code: 'NO_DATA', detail: 'No data for SPY in this range' } },
      { status: 400, statusText: 'Bad Request' },
    );

    await waitFor(() => expect(screen.getByText('No data for SPY in this range')).toBeTruthy());
    expect(store.chartStale()).toBe(true);
    expect(fixture.componentInstance.loading()).toBe(false);
    http.verify();
  });

  it('commits the server\'s recommended timeframe once when a timeframe is rejected, and no second time', async () => {
    const { http, context, fixture } = await renderChart();
    flushCatalog(http);
    const [first] = await chartRequests(http, 1);
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
    await waitFor(() => expect(context.commitTimeframe).toHaveBeenCalledExactlyOnceWith('1d'));

    // A second rejection means something else is wrong: the chart's own message stands.
    fixture.componentInstance.onChartTimeframeRejected({ requested: '1d', recommended: '1D' });
    expect(context.commitTimeframe).toHaveBeenCalledTimes(1);
    http.verify();
  });

  it('does not commit a recommendation this place does not offer', async () => {
    const { http, context } = await renderChart();
    flushCatalog(http);
    const [first] = await chartRequests(http, 1);
    first.flush(
      { detail: { error_code: 'TIMEFRAME_NOT_ALLOWED', detail: 'Too many bars.', recommended_timeframe: '1W', allowed_timeframes: ['1W'] } },
      { status: 400, statusText: 'Bad Request' },
    );

    await waitFor(() => expect(screen.getByText('Too many bars.')).toBeTruthy());
    expect(context.commitTimeframe).not.toHaveBeenCalled();
  });

  it('loads the headlines only when the section is opened, for the committed window', async () => {
    const { http } = await renderChart();
    flushCatalog(http);
    const [first] = await chartRequests(http, 1);
    first.flush(chartResponse());
    http.verify();

    await userEvent.click(screen.getByRole('button', { name: /Headlines for SPY/ }));
    const news = await waitFor(() => http.expectOne((r) => r.method === 'GET' && r.url === `${environment.pythonServiceUrl}/api/news`));
    expect(news.request.params.get('ticker')).toBe('SPY');
    expect(news.request.params.get('published_utc_gte')).toBe(new Date(WINDOW.startMsUtc).toISOString());
    expect(news.request.params.get('published_utc_lt')).toBe(new Date(WINDOW.endMsUtc).toISOString());
    news.flush({
      articles: [{ id: 'n1', title: 'First headline', published_utc_ms: WINDOW.endMsUtc - 1000 }],
      count: 1,
      vendor: 'polygon',
      fetched_at_ms: WINDOW.endMsUtc,
    });

    expect(await screen.findByText('First headline')).toBeTruthy();
    // The headlines say where they came from, and that they are not the chart's bars.
    expect(screen.getByText(/Polygon news, read/)).toBeTruthy();
    expect(screen.getByText(/not part of the bars above/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'View all' }).getAttribute('href')).toBe('/markets/SPY/chart/news');
  });

  it('says so in the Indicators drawer when the catalog fails to load', async () => {
    const { http, fixture } = await renderChart();
    http
      .expectOne(`${environment.pythonServiceUrl}/api/dataset/available`)
      .flush('boom', { status: 500, statusText: 'Internal Server Error' });
    (await chartRequests(http, 1))[0].flush(chartResponse());
    await fixture.whenStable();

    await userEvent.click(screen.getByRole('button', { name: 'Indicators' }));
    const drawer = screen.getByRole('region', { name: 'Indicators' });

    await waitFor(() =>
      expect(drawer.querySelector('[role="alert"]')?.textContent).toContain('Indicators could not be loaded.'),
    );
    expect(drawer.textContent).not.toContain('No indicators available');
  });

  it('shows no catalog failure in the Indicators drawer when the catalog loads', async () => {
    const { http } = await renderChart();
    flushCatalog(http);
    (await chartRequests(http, 1))[0].flush(chartResponse());

    await userEvent.click(screen.getByRole('button', { name: 'Indicators' }));
    const drawer = screen.getByRole('region', { name: 'Indicators' });

    // flushCatalog() serves an empty catalog, so the picker's neutral state shows.
    expect(drawer.textContent).toContain('No indicators available');
    expect(drawer.querySelector('[role="alert"]')).toBeNull();
  });

  it('says when the context makes it read raw prices or unfilled minute gaps', async () => {
    const { http } = await renderChart(committedStore(), fakeContext({ adjustment: 'raw', forwardFill: false }));
    flushCatalog(http);
    (await chartRequests(http, 1))[0].flush(chartResponse());

    expect(await screen.findByText('This chart reads raw prices, not adjusted for splits and minute gaps that are not forward-filled.')).toBeTruthy();
  });

  it('says nothing about its price adjustment or forward fill when the context holds the defaults', async () => {
    const { http } = await renderChart();
    flushCatalog(http);
    (await chartRequests(http, 1))[0].flush(chartResponse());

    expect(screen.queryByText(/This chart reads/)).toBeNull();
  });
});
