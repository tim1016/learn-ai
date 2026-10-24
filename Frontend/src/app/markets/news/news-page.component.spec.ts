import { provideZonelessChangeDetection, signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { provideRouter } from '@angular/router';

import { environment } from '../../../environments/environment';
import type { InstrumentContext } from '../instrument-context';
import { InstrumentContextService } from '../instrument-context.service';
import { NewsPageComponent } from './news-page.component';

/** The place's context as the page reads it: the instrument, and the window committed for it. */
function placeContext(symbol: string, context: InstrumentContext | null = null) {
  return { symbol: signal(symbol), context: signal(context) };
}

const COMMITTED: InstrumentContext = {
  window: { from: '2026-04-15', to: '2026-05-15' },
  timeframe: '1d',
  session: 'regular',
  adjustment: 'polygon_split_adjusted',
  forwardFill: true,
};

const NEWS_URL = `${environment.pythonServiceUrl}/api/news`;

function responseBody(overrides: Record<string, unknown> = {}) {
  return {
    articles: [
      {
        id: 'a1',
        title: 'SPY climbs on strong data',
        description: 'A description.',
        author: 'A. Reporter',
        article_url: 'https://example.test/a',
        amp_url: null,
        image_url: null,
        published_utc_ms: 1788528600000,
        tickers: ['SPY', 'DIA'],
        keywords: ['markets'],
        publisher: { name: 'Example Wire', homepage_url: null, logo_url: null, favicon_url: null },
        insights: [{ ticker: 'SPY', sentiment: 'positive', sentiment_reasoning: 'Strong print.' }],
      },
    ],
    count: 1,
    vendor: 'polygon',
    vendor_endpoint: '/v2/reference/news',
    sentiment_provenance: 'vendor_asserted',
    fetched_at_ms: 1788528600000,
    ...overrides,
  };
}

async function renderPage(place = placeContext('SPY')) {
  const rendered = await render(NewsPageComponent, {
    providers: [
      provideZonelessChangeDetection(),
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: InstrumentContextService, useValue: place },
    ],
  });
  const http = TestBed.inject(HttpTestingController);
  return { ...rendered, http };
}

describe('NewsPageComponent', () => {
  it('fetches the instrument the address names, newest first, and omits blank optional parameters from the request', async () => {
    const { http } = await renderPage(placeContext('QQQ'));

    const req = http.expectOne((r) => r.url === NEWS_URL);

    expect.soft(req.request.params.get('ticker'), 'fetches the instrument in the address').toBe('QQQ');
    expect.soft(req.request.params.get('order'), 'fetches newest first: order').toBe('desc');
    expect.soft(req.request.params.get('sort'), 'fetches newest first: sort').toBe('published_utc');
    expect.soft(req.request.params.get('limit'), 'fetches 50 articles by default').toBe('50');
    expect.soft(req.request.params.has('ticker_gte'), 'omits the blank ticker_gte parameter').toBe(false);
    expect.soft(req.request.params.has('published_utc'), 'omits the blank published_utc parameter').toBe(false);
    req.flush(responseBody());
  });

  it('renders the article with its vendor sentiment, labelled vendor-asserted rather than derived', async () => {
    const { http } = await renderPage();
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());

    expect.soft(await screen.findByText('SPY climbs on strong data'), 'renders the article title').toBeTruthy();
    expect.soft(screen.getByText('Example Wire'), 'renders the publisher').toBeTruthy();
    expect.soft(screen.getByText(/SPY: positive/), 'renders the vendor sentiment').toBeTruthy();
    expect.soft(screen.getByText('Strong print.'), 'renders the sentiment reasoning').toBeTruthy();

    expect.soft(await screen.findByText(/vendor_asserted/), 'labels sentiment as vendor-asserted').toBeTruthy();
    // Page-level caveat, naming the point-in-time hazard.
    expect.soft(screen.getByText(/backfilled sentiment is not a point-in-time input/), 'page-level caveat names the point-in-time hazard').toBeTruthy();
    // Per-card note, reachable by keyboard rather than a title tooltip.
    expect.soft(screen.getByText(/not validated by us/), 'per-card note says the sentiment is not validated').toBeTruthy();
  });

  it('asks for the headlines of the window the chart\'s were read for, and for no window when none is committed', async () => {
    const withWindow = await renderPage(placeContext('SPY', COMMITTED));
    const req = withWindow.http.expectOne((r) => r.url === NEWS_URL);
    expect.soft(req.request.params.get('published_utc_gte'), 'starts at the window\'s first instant').toBe(new Date(Date.UTC(2026, 3, 15)).toISOString());
    expect.soft(req.request.params.get('published_utc_lt'), 'ends after the window\'s last day').toBe(new Date(Date.UTC(2026, 4, 15) + 86_399_999).toISOString());
    req.flush(responseBody());
    TestBed.resetTestingModule();

    const without = await renderPage(placeContext('SPY'));
    const open = without.http.expectOne((r) => r.url === NEWS_URL);
    expect.soft(open.request.params.has('published_utc_gte')).toBe(false);
    expect.soft(open.request.params.has('published_utc_lt')).toBe(false);
    open.flush(responseBody());
  });

  it('does not refetch while the user edits — only on submit, and the instrument stays the address\'s', async () => {
    const { http, fixture } = await renderPage();
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());
    await fixture.whenStable();
    fixture.detectChanges();

    // An edit lands in the draft without spending the 5-per-minute budget.
    await userEvent.type(screen.getByRole('textbox', { name: 'Published ≥' }), '2026-01-02');
    http.verify();

    await userEvent.click(screen.getByRole('button', { name: /fetch news/i }));

    const second = http.expectOne((r) => r.url === NEWS_URL);
    expect(second.request.params.get('ticker')).toBe('SPY');
    expect(second.request.params.get('published_utc_gte')).toBe('2026-01-02');
    second.flush(responseBody());
  });

  it('asks for the other instrument\'s headlines when the address moves to it, never showing the first one\'s under it', async () => {
    const place = placeContext('SPY');
    const { http, fixture } = await renderPage(place);
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());
    expect(await screen.findByText('SPY climbs on strong data')).toBeTruthy();

    place.symbol.set('QQQ');
    TestBed.tick();
    const req = http.expectOne((r) => r.url === NEWS_URL);
    expect(req.request.params.get('ticker')).toBe('QQQ');
    fixture.detectChanges();

    expect(screen.queryByText('SPY climbs on strong data')).toBeNull();
    req.flush(responseBody({ articles: [] }));
  });

  it('reports an upstream failure instead of rendering an empty list', async () => {
    const { http } = await renderPage();

    http
      .expectOne((r) => r.url === NEWS_URL)
      .flush({ detail: 'Upstream news fetch failed' }, { status: 502, statusText: 'Bad Gateway' });

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText('Could not fetch news.')).toBeTruthy();
  });
});
