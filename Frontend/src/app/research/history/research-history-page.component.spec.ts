import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { provideFakeDateWorld } from '../../shared/date-picker/testing/fake-trading-calendar';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { environment } from '../../../environments/environment';
import { ResearchHistoryPageComponent } from './research-history-page.component';
import {
  ResearchHistoryService,
  ResearchHistoryUnavailableError,
  type ResearchHistoryEntry,
  type ResearchHistoryPage,
  type ResearchHistoryQuery,
  type ResearchHistorySource,
} from './research-history';

const ANY: ResearchHistoryQuery = { type: null, symbol: null, status: null, from: null, to: null, page: 1 };
const RUN = 'a'.repeat(32);

function entry(overrides: Partial<ResearchHistoryEntry> = {}): ResearchHistoryEntry {
  return {
    type: 'spec-run',
    native_id: RUN,
    question: 'How did "spy_ema" trade SPY?',
    symbols: ['SPY'],
    window: { start_ms: 1_704_171_600_000, end_ms: 1_735_621_200_000 },
    status: 'completed',
    status_detail: null,
    created_at_ms: 1_790_000_000_000,
    completed_at_ms: 1_790_000_005_000,
    provenance: 'data SPY|15|1|2|files:abc; engine 0.1.0',
    headline: null,
    parent: null,
    ...overrides,
  };
}

function source(overrides: Partial<ResearchHistorySource> = {}): ResearchHistorySource {
  return { type: 'spec-run', label: 'saved spec runs', state: 'read', total: 1, unreadable: 0, error: null, ...overrides };
}

function page(overrides: Partial<ResearchHistoryPage> = {}): ResearchHistoryPage {
  return {
    entries: [entry()],
    total: 1,
    page: 1,
    page_size: 25,
    partial: false,
    sources: [source()],
    symbols: ['QQQ', 'SPY'],
    ...overrides,
  };
}

async function renderHistory(
  answer: ResearchHistoryPage | Error,
  inputs: Record<string, string> = {},
) {
  const read = vi.fn<(query: ResearchHistoryQuery) => Promise<ResearchHistoryPage>>();
  if (answer instanceof Error) read.mockRejectedValue(answer);
  else read.mockResolvedValue(answer);
  const view = await render(ResearchHistoryPageComponent, {
    inputs,
    providers: [
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      // The symbol filter is the shared symbol picker over a closed list the page owns.
      provideFakeVendorCatalog(fakeVendorCatalog()),
      provideFakeTickerCatalog(fakeTickerCatalog()),
      provideFakeDateWorld(),
      { provide: ResearchHistoryService, useValue: { read } },
    ],
  });
  await view.fixture.whenStable();
  view.fixture.detectChanges();
  const navigate = vi.spyOn(view.fixture.debugElement.injector.get(Router), 'navigate').mockResolvedValue(true);
  return { ...view, read, navigate };
}

/** The page-level state block, apart from the filters’ own words (“Failed”, “Partial” are also statuses). */
function stateBlock(): ReturnType<typeof within> {
  return within(document.querySelector('app-workspace-state-block') as HTMLElement);
}

describe('ResearchHistoryPageComponent', () => {
  it('reads with no filter pre-selected and lists the entries as served, each with the facts its store recorded', async () => {
    const { read } = await renderHistory(page());

    expect(read).toHaveBeenCalledWith(ANY);
    const row = screen.getByRole('link', { name: 'How did "spy_ema" trade SPY?' }).closest('tr') as HTMLElement;
    expect(within(row).getByText('Rule sandbox run')).toBeTruthy();
    expect(within(row).getByText('SPY')).toBeTruthy();
    expect(within(row).getByText('Completed')).toBeTruthy();
    expect(within(row).getByText(RUN)).toBeTruthy();
    expect(within(row).getByText('data SPY|15|1|2|files:abc; engine 0.1.0')).toBeTruthy();
    // A window is a pair of dates in ET, never shifted by the viewer's zone.
    expect(row.querySelectorAll('app-timestamp-display[data-timestamp-mode="date-et"]').length).toBe(2);
  });

  it('opens each entry at its own store’s detail by type and id, so equal ids of two kinds are two links', async () => {
    await renderHistory(
      page({
        entries: [
          entry({ type: 'signal-report', native_id: '7', question: 'Signal 7' }),
          entry({ type: 'walk-forward', native_id: '7', question: 'Walk-forward 7' }),
          entry({ type: 'monte-carlo', native_id: RUN, question: 'MC', parent: { type: 'spec-run', native_id: 'b'.repeat(32) } }),
          entry({ type: 'baseline', native_id: 'c'.repeat(32), question: 'Baseline' }),
        ],
        total: 4,
      }),
    );

    expect(screen.getByRole('link', { name: 'Signal 7' }).getAttribute('href')).toBe('/research/history/signal-report/7');
    expect(screen.getByRole('link', { name: 'Walk-forward 7' }).getAttribute('href')).toBe('/research/history/walk-forward/7');
    expect(screen.getByRole('link', { name: 'MC' }).getAttribute('href')).toBe(`/research/history/monte-carlo/${RUN}`);
    expect(screen.getByRole('link', { name: 'Baseline' }).getAttribute('href')).toBe(`/research/history/baseline/${'c'.repeat(32)}`);
    expect(screen.getByRole('link', { name: 'Rule sandbox run bbbbbbbb' }).getAttribute('href')).toBe(
      `/research/history/spec-run/${'b'.repeat(32)}`,
    );
  });

  it('shows a feature study as the headline its store kept, with no link to a detail that does not exist', async () => {
    await renderHistory(
      page({
        entries: [
          entry({
            type: 'feature-experiment',
            native_id: '5',
            question: 'Does rsi_14 predict the next move of SPY?',
            headline: 'Mean IC 0.1235, t-stat 2.50; did not pass validation; 48,750 bars',
            provenance: null,
            completed_at_ms: null,
          }),
        ],
      }),
    );

    expect(screen.queryByRole('link', { name: /rsi_14/ })).toBeNull();
    expect(screen.getByText('Does rsi_14 predict the next move of SPY?')).toBeTruthy();
    expect(screen.getByText('Mean IC 0.1235, t-stat 2.50; did not pass validation; 48,750 bars')).toBeTruthy();
    expect(screen.getByText('Headline only: no more of it was saved.')).toBeTruthy();
  });

  it('reads “not recorded” for every fact the store did not record, and invents none', async () => {
    await renderHistory(
      page({ entries: [entry({ symbols: [], window: null, provenance: null, completed_at_ms: null })] }),
    );

    const row = screen.getByRole('link', { name: /spy_ema/ }).closest('tr') as HTMLElement;
    // Instrument or universe, window, finish time and data: four facts, four “not recorded”.
    expect(within(row).getAllByText('not recorded').length).toBe(4);
  });

  it('shows failed, partial and cancelled runs with their status and what part failed', async () => {
    await renderHistory(
      page({
        entries: [
          entry({ native_id: '1'.repeat(32), status: 'failed', status_detail: 'no bars in the window', question: 'F' }),
          entry({ native_id: '2'.repeat(32), status: 'partial', status_detail: '1 of 3 folds failed', question: 'P' }),
          entry({ native_id: '3'.repeat(32), status: 'cancelled', question: 'C' }),
        ],
        total: 3,
      }),
    );

    const rowOf = (name: string) => screen.getByRole('link', { name }).closest('tr') as HTMLElement;
    expect(within(rowOf('F')).getByText('Failed')).toBeTruthy();
    expect(within(rowOf('F')).getByText('no bars in the window')).toBeTruthy();
    expect(within(rowOf('P')).getByText('Partial')).toBeTruthy();
    expect(within(rowOf('P')).getByText('1 of 3 folds failed')).toBeTruthy();
    expect(within(rowOf('C')).getByText('Cancelled')).toBeTruthy();
  });

  it('names a store that could not be read and what is missing, rather than showing the rest as the whole history', async () => {
    await renderHistory(
      page({
        partial: true,
        sources: [
          source(),
          source({
            type: 'walk-forward',
            label: 'saved walk-forward runs',
            state: 'unavailable',
            total: null,
            error: 'The saved walk-forward runs could not be read: its files could not be read.',
          }),
          source({ type: 'baseline', label: 'saved baseline runs', total: 2, unreadable: 3 }),
        ],
      }),
    );

    expect(stateBlock().getByText('Partial')).toBeTruthy();
    expect(stateBlock().getByText(/The saved walk-forward runs could not be read: its files could not be read\./)).toBeTruthy();
    expect(stateBlock().getByText(/3 of the saved baseline runs could not be read and are missing from this list\./)).toBeTruthy();
    expect(screen.queryByText('Nothing has been saved yet.')).toBeNull();
  });

  it('with an empty total from stores that could be read in part, never says nothing has been saved', async () => {
    await renderHistory(
      page({
        entries: [],
        total: 0,
        partial: true,
        sources: [source({ total: 0 }), source({ type: 'walk-forward', state: 'unavailable', total: null, error: 'The saved walk-forward runs could not be read: it did not answer in time.' })],
      }),
    );

    expect(screen.queryByText('Nothing has been saved yet.')).toBeNull();
    expect(screen.getByText(/did not answer in time/)).toBeTruthy();
  });

  it('says nothing has been saved yet only when every store was read and held nothing', async () => {
    await renderHistory(page({ entries: [], total: 0, sources: [source({ total: 0 })] }));

    expect(screen.getByText('Nothing has been saved yet.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('shows no list at all, with each store’s reason, when every store failed, and tries again on request', async () => {
    const refusal = new ResearchHistoryUnavailableError([
      source({ state: 'unavailable', total: null, error: 'The saved spec runs could not be read: its files could not be read.' }),
      source({ type: 'signal-report', label: 'saved signal reports', state: 'unavailable', total: null, error: 'The saved signal reports could not be read: the database could not be reached or queried.' }),
    ]);
    const { read } = await renderHistory(refusal);

    expect(stateBlock().getByText('Unavailable')).toBeTruthy();
    expect(stateBlock().getByText(/the database could not be reached or queried/)).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText('Nothing has been saved yet.')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('says the data plane did not answer when the failure names no store', async () => {
    await renderHistory(new Error('network'));

    expect(stateBlock().getByText('Failed')).toBeTruthy();
    expect(stateBlock().getByText('The data plane did not answer.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('reads the URL’s filters and page, and a value it does not know is no filter', async () => {
    const { read } = await renderHistory(page(), {
      type: 'walk-forward',
      symbol: ' qqq ',
      status: 'partial',
      from: '2026-01-02',
      to: '2026-02-03',
      page: '3',
    });

    expect(read).toHaveBeenCalledWith({
      type: 'walk-forward',
      symbol: 'QQQ',
      status: 'partial',
      from: '2026-01-02',
      to: '2026-02-03',
      page: 3,
    });
  });

  it('a changed filter goes to the URL and starts again from the first page', async () => {
    const { navigate } = await renderHistory(page(), { page: '2' });

    const kind = screen.getByLabelText('Kind') as HTMLSelectElement;
    kind.value = 'baseline';
    fireEvent.change(kind);
    const status = screen.getByLabelText('Status') as HTMLSelectElement;
    status.value = 'failed';
    fireEvent.change(status);

    expect(navigate).toHaveBeenNthCalledWith(1, [], expect.objectContaining({ queryParams: { type: 'baseline', page: null }, queryParamsHandling: 'merge' }));
    expect(navigate).toHaveBeenNthCalledWith(2, [], expect.objectContaining({ queryParams: { status: 'failed', page: null } }));
  });

  it('pages through the count the server stated, and Newer is off on the first page', async () => {
    const { navigate } = await renderHistory(page({ total: 60, entries: [entry()] }));

    expect(screen.getByRole('status').textContent).toBe('Page 1 of 3 · 60 records');
    expect((screen.getByRole('button', { name: 'Newer' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { page: '2' } }));
  });

  it('a page past the end says so with the true count, and offers the last page', async () => {
    const { navigate } = await renderHistory(page({ total: 60, entries: [], page: 9 }), { page: '9' });

    expect(screen.getByRole('status').textContent).toBe('Page 9 is past the end: 60 records, 3 pages.');
    fireEvent.click(screen.getByRole('button', { name: 'Last page' }));
    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { page: '3' } }));
  });

  it('says no record matches when a filter is set and the stores hold none for it', async () => {
    await renderHistory(page({ entries: [], total: 0 }), { status: 'cancelled' });

    expect(screen.getByRole('status').textContent).toBe('No saved record matches these filters.');
  });

  it('offers the symbols the stores hold for the symbol filter, a closed list that gates nothing on the lake', async () => {
    await renderHistory(page({ symbols: ['QQQ', 'SPY'] }));

    expect(screen.getByRole('combobox', { name: 'Symbol or universe member' })).toBeTruthy();
  });
});

@Component({ template: 'a stored detail', standalone: true })
class StoredDetailComponent {}

describe('ResearchHistoryPageComponent, over the real service', () => {
  it('only ever reads: listing asks for one page, and opening an entry asks for nothing to be run', async () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter(
          [
            { path: 'research/history', component: ResearchHistoryPageComponent },
            { path: 'research/history/spec-run/:id', component: StoredDetailComponent },
          ],
          withComponentInputBinding(),
        ),
        provideHttpClient(),
        provideHttpClientTesting(),
        provideFakeVendorCatalog(fakeVendorCatalog()),
        provideFakeTickerCatalog(fakeTickerCatalog()),
        provideFakeDateWorld(),
      ],
    });
    const http = TestBed.inject(HttpTestingController);
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl('/research/history?type=spec-run&page=1');

    const listed = http.expectOne((r) => r.url === `${environment.pythonServiceUrl}/api/research/history`);
    expect(listed.request.method).toBe('GET');
    expect(listed.request.params.get('type')).toBe('spec-run');
    listed.flush(page());
    await harness.fixture.whenStable();
    harness.detectChanges();

    (screen.getByRole('link', { name: 'How did "spy_ema" trade SPY?' }) as HTMLAnchorElement).click();
    await harness.fixture.whenStable();

    expect(TestBed.inject(Router).url).toBe(`/research/history/spec-run/${RUN}`);
    harness.detectChanges();
    expect(harness.routeNativeElement?.textContent).toContain('a stored detail');
    // Opening the detail asked the research API for nothing: no run started, no second read of the list. (The shared
    // pickers read the jobs snapshot on start-up, and only read it.)
    const rest = http.match(() => true);
    expect(rest.every((request) => request.request.method === 'GET')).toBe(true);
    expect(rest.filter((request) => request.request.url.includes('/api/research'))).toEqual([]);
  });
});
