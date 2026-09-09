import { provideZonelessChangeDetection } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { NewsPageComponent } from './news-page.component';

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

async function renderPage() {
  const rendered = await render(NewsPageComponent, {
    providers: [provideZonelessChangeDetection(), provideHttpClient(), provideHttpClientTesting()],
  });
  const http = TestBed.inject(HttpTestingController);
  return { ...rendered, http };
}

describe('NewsPageComponent', () => {
  it('fetches SPY by default, newest first', async () => {
    const { http } = await renderPage();

    const req = http.expectOne((r) => r.url === NEWS_URL);

    expect(req.request.params.get('ticker')).toBe('SPY');
    expect(req.request.params.get('order')).toBe('desc');
    expect(req.request.params.get('sort')).toBe('published_utc');
    expect(req.request.params.get('limit')).toBe('50');
    req.flush(responseBody());
  });

  it('renders the article with its vendor sentiment', async () => {
    const { http } = await renderPage();
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());

    expect(await screen.findByText('SPY climbs on strong data')).toBeTruthy();
    expect(screen.getByText('Example Wire')).toBeTruthy();
    expect(screen.getByText(/SPY: positive/)).toBeTruthy();
    expect(screen.getByText('Strong print.')).toBeTruthy();
  });

  it('labels sentiment as vendor-asserted rather than derived', async () => {
    const { http } = await renderPage();
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());

    expect(await screen.findByText(/vendor_asserted/)).toBeTruthy();
    // Page-level caveat, naming the point-in-time hazard.
    expect(screen.getByText(/backfilled sentiment is not a point-in-time input/)).toBeTruthy();
    // Per-card note, reachable by keyboard rather than a title tooltip.
    expect(screen.getByText(/not validated by us/)).toBeTruthy();
  });

  it('omits blank optional parameters from the request', async () => {
    const { http } = await renderPage();

    const req = http.expectOne((r) => r.url === NEWS_URL);

    expect(req.request.params.has('ticker_gte')).toBe(false);
    expect(req.request.params.has('published_utc')).toBe(false);
    req.flush(responseBody());
  });

  it('does not refetch while the user edits — only on submit', async () => {
    const { http } = await renderPage();
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());

    const tickerInput = screen.getByLabelText('Ticker');
    await userEvent.clear(tickerInput);
    await userEvent.type(tickerInput, 'NVDA');

    // Typing must not spend the 5-per-minute upstream budget.
    http.verify();

    await userEvent.click(screen.getByRole('button', { name: /fetch news/i }));

    const second = http.expectOne((r) => r.url === NEWS_URL);
    expect(second.request.params.get('ticker')).toBe('NVDA');
    second.flush(responseBody());
  });

  it('reports an upstream failure instead of rendering an empty list', async () => {
    const { http } = await renderPage();

    http
      .expectOne((r) => r.url === NEWS_URL)
      .flush({ detail: 'Upstream news fetch failed' }, { status: 502, statusText: 'Bad Gateway' });

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText('Could not fetch news.')).toBeTruthy();
  });

  it('says so plainly when nothing matched', async () => {
    const { http } = await renderPage();

    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody({ articles: [], count: 0 }));

    expect(await screen.findByText('No articles matched this query.')).toBeTruthy();
  });
});
