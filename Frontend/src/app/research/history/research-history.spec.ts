import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../environments/environment';
import {
  RESEARCH_HISTORY_PAGE_SIZE,
  ResearchHistoryService,
  ResearchHistoryUnavailableError,
  researchHistoryQuery,
} from './research-history';

const URL = `${environment.pythonServiceUrl}/api/research/history`;

describe('researchHistoryQuery', () => {
  it('treats nothing in the URL as no filter on the first page', () => {
    expect(researchHistoryQuery({})).toEqual({ type: null, symbol: null, status: null, from: null, to: null, page: 1 });
  });

  it('keeps what the list knows and drops the rest, so an old or edited link still opens it', () => {
    expect(
      researchHistoryQuery({ type: 'not-a-kind', status: 'on-fire', symbol: '  ', from: '2026-02-31', to: 'soon', page: '0' }),
    ).toEqual({ type: null, symbol: null, status: null, from: null, to: null, page: 1 });
    expect(researchHistoryQuery({ type: 'monte-carlo', status: 'unreadable', symbol: ' spy ', page: '4' })).toMatchObject({
      type: 'monte-carlo',
      status: 'unreadable',
      symbol: 'SPY',
      page: 4,
    });
  });

  it('drops a range that ends before it starts rather than asking a question with no answer', () => {
    expect(researchHistoryQuery({ from: '2026-03-01', to: '2026-02-01' })).toMatchObject({ from: null, to: null });
    expect(researchHistoryQuery({ from: '2026-03-01' })).toMatchObject({ from: '2026-03-01', to: null });
  });
});

describe('ResearchHistoryService', () => {
  function setup() {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    return { service: TestBed.inject(ResearchHistoryService), http: TestBed.inject(HttpTestingController) };
  }

  it('asks for one bounded page and nothing else', async () => {
    const { service, http } = setup();

    const pending = service.read({ type: null, symbol: null, status: null, from: null, to: null, page: 2 });

    const request = http.expectOne((r) => r.url === URL);
    expect(request.request.method).toBe('GET');
    expect(request.request.params.get('page')).toBe('2');
    expect(request.request.params.get('page_size')).toBe(String(RESEARCH_HISTORY_PAGE_SIZE));
    expect(request.request.params.keys().sort()).toEqual(['page', 'page_size']);
    request.flush({ entries: [], total: 0, page: 2, page_size: 25, partial: false, sources: [], symbols: [] });
    await pending;
    http.verify();
  });

  it('turns the created-between dates into ET-midnight instants, the end day included', async () => {
    const { service, http } = setup();

    const pending = service.read({
      type: 'spec-run',
      symbol: 'SPY',
      status: 'failed',
      from: '2026-01-05',
      to: '2026-03-08',
      page: 1,
    });

    const request = http.expectOne((r) => r.url === URL);
    const params = request.request.params;
    expect(params.get('type')).toBe('spec-run');
    expect(params.get('symbol')).toBe('SPY');
    expect(params.get('status')).toBe('failed');
    // 2026-01-05 00:00 EST is 05:00 UTC; the end is the next ET midnight, 2026-03-09 00:00 EDT (04:00 UTC).
    expect(params.get('created_from_ms')).toBe(String(Date.UTC(2026, 0, 5, 5)));
    expect(params.get('created_before_ms')).toBe(String(Date.UTC(2026, 2, 9, 4)));
    request.flush({}, { status: 500, statusText: 'Internal Server Error' });
    await expect(pending).rejects.toBeTruthy();
  });

  it('turns the 503 that names every store into an error carrying each store’s reason', async () => {
    const { service, http } = setup();
    const sources = [{ type: 'spec-run', label: 'saved spec runs', state: 'unavailable', total: null, unreadable: 0, error: 'down' }];

    const pending = service.read({ type: null, symbol: null, status: null, from: null, to: null, page: 1 });
    http.expectOne((r) => r.url === URL).flush(
      { detail: { code: 'RESEARCH_HISTORY_UNAVAILABLE', message: 'm', sources } },
      { status: 503, statusText: 'Service Unavailable' },
    );

    const error = await pending.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ResearchHistoryUnavailableError);
    expect((error as ResearchHistoryUnavailableError).sources).toEqual(sources);
  });
});
