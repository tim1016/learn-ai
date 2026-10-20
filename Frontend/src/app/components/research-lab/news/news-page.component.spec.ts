import { provideZonelessChangeDetection } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { NewsPageComponent } from './news-page.component';
import { By } from '@angular/platform-browser';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';

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
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(),
      provideHttpClientTesting(),
      // The ticker filter's picker must not issue real catalog reads.
      ...fakePickerWorld().providers,
    ],
  });
  const http = TestBed.inject(HttpTestingController);
  return { ...rendered, http };
}

describe('NewsPageComponent', () => {
  it('fetches SPY by default, newest first, and omits blank optional parameters from the request', async () => {
    const { http } = await renderPage();

    const req = http.expectOne((r) => r.url === NEWS_URL);

    expect.soft(req.request.params.get('ticker'), 'fetches SPY by default').toBe('SPY');
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

  it('does not refetch while the user edits — only on submit', async () => {
    const { http, fixture } = await renderPage();
    http.expectOne((r) => r.url === NEWS_URL).flush(responseBody());
    await fixture.whenStable();
    fixture.detectChanges();

    // The ticker field is the shared picker now: an edit is a pick, and it
    // must land in the draft without spending the 5-per-minute budget.
    const picker = fixture.debugElement.query(
      By.directive(SymbolPickerComponent),
    ).componentInstance as SymbolPickerComponent;
    picker.symbol.set('NVDA');
    fixture.detectChanges();

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
});
