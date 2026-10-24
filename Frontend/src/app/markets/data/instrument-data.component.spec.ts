import { signal } from '@angular/core';
import { screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { BackfillDefaults } from '../../shared/data-lake';
import type { BackfillJobRun, BackfillRunState } from '../../shared/data-lake/backfill-job-runner';
import { dataReport, dataSession } from '../../shared/data-lake/testing/instrument-data-report';
import { TradeBackfillRunner, type TradeBackfillEnd } from '../../shared/data-lake/trade-backfill-runner';
import type { TradeBackfillWindow } from '../../shared/data-lake/trade-backfill-spec';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../shared/ticker-catalog/testing/fake-ticker-catalog';
import {
  INSTRUMENT_DATA_URL,
  WINDOW_QUERY,
  openPlace,
  type OpenedPlace,
} from '../testing/instrument-place';

/** The backfill job's runner, standing in: it records the window it was asked for and ends when the test says. */
function fakeRunner() {
  const windows: TradeBackfillWindow[] = [];
  const state = signal<BackfillRunState>({
    kind: 'running',
    started: true,
    progress: { current: 1, total: 4, unit: 'days', message: null },
  });
  const cancel = vi.fn(async () => undefined);
  let finish: (end: TradeBackfillEnd) => void = () => undefined;
  const run = vi.fn(
    async (
      select: (defaults: BackfillDefaults) => TradeBackfillWindow | { kind: 'failed'; code: string; message: string } | null,
      accepted: (job: BackfillJobRun) => boolean | Promise<boolean>,
    ): Promise<TradeBackfillEnd> => {
      const window = select({
        market: 'usa',
        lean_image_digest: 'sha256:test',
        max_trading_range_days: 1830,
        max_symbol_length: 5,
        provider_history_start_ms: 0,
      });
      if (window !== null && !('kind' in window)) windows.push(window);
      await accepted({
        jobId: 'job-1',
        state: state.asReadonly(),
        terminal: new Promise(() => undefined),
        detach: vi.fn(),
        cancel,
      });
      return new Promise<TradeBackfillEnd>((resolve) => {
        finish = resolve;
      });
    },
  );
  return { provider: { provide: TradeBackfillRunner, useValue: { run } }, run, windows, cancel, end: (end: TradeBackfillEnd) => finish(end) };
}

const READ_URL = (request: { url: string }) => request.url === INSTRUMENT_DATA_URL;

/** Flushes the one read of the lake the tab opens with. */
async function flushRead(opened: OpenedPlace, report: ReturnType<typeof dataReport>) {
  const request = await waitFor(() => opened.http.expectOne(READ_URL));
  request.flush(report);
  return request;
}

/** Four sessions: the lake lacks two, holds one, and one the provider no longer serves. */
function gappedReport() {
  return dataReport(
    [
      dataSession(0, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
      dataSession(1, 'ready'),
      dataSession(2, 'missing', { fetchable: true }),
      dataSession(3, 'needs_rebuild', { fetchable: true, reason: 'adjustment_record_missing' }),
    ],
    { fill_window: { start_trading_date_ms: Date.UTC(2026, 3, 17, 12), end_trading_date_ms: Date.UTC(2026, 3, 20, 12) } },
  );
}

describe('an instrument\'s Data tab', () => {
  it('reads the lake for the committed window and adjustment and fetches nothing: no job, no fill', async () => {
    const runner = fakeRunner();
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);

    const read = await flushRead(opened, gappedReport());

    expect(read.request.method).toBe('GET');
    expect(read.request.params.get('symbol')).toBe('SPY');
    expect(read.request.params.get('start_trading_date_ms')).toBe(String(Date.UTC(2026, 3, 15, 12)));
    expect(read.request.params.get('end_trading_date_ms')).toBe(String(Date.UTC(2026, 4, 15, 12)));
    expect(read.request.params.get('price_adjustment_mode')).toBe('polygon_split_adjusted');
    expect(await screen.findByText('A run cannot read 3 of the 4 finished sessions in this window.')).toBeTruthy();
    expect(runner.run, 'opening the tab fills nothing').not.toHaveBeenCalled();
    opened.http.expectNone((request) => request.url.includes('/api/jobs'));
    opened.http.verify();
  });

  it('shows each gap with its dates, its cause, and what a Fill would do, before anything fills it', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`);
    await flushRead(opened, gappedReport());

    const gaps = await screen.findByRole('region', { name: 'Gaps in the window' });
    const rows = within(gaps).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('2026-04-15');
    expect(rows[0].textContent).toContain('older than the provider serves, so it can no longer be fetched');
    expect(rows[1].textContent).toContain('A Fill fetches it');
    expect(rows[2].textContent).toContain('its adjustment record is missing');
    expect(screen.getByRole('button', { name: 'Fill 2 sessions' })).toBeTruthy();
  });

  it('does not promise that a Fill leaves a no-bars session alone when it lies between two the Fill fetches', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`);
    await flushRead(
      opened,
      dataReport(
        [
          dataSession(0, 'missing', { fetchable: true }),
          dataSession(1, 'failed', { reason: 'provider_no_data', blocked_by: 'provider_no_data' }),
          dataSession(2, 'missing', { fetchable: true }),
        ],
        { fill_window: { start_trading_date_ms: Date.UTC(2026, 3, 15, 12), end_trading_date_ms: Date.UTC(2026, 3, 17, 12) } },
      ),
    );

    const gaps = await screen.findByRole('region', { name: 'Gaps in the window' });
    expect(within(gaps).getByText(/asks the provider again if it lies inside the span the Fill covers/)).toBeTruthy();
    expect(screen.getByText(/A session in between that failed, or that drew no bars from the provider, is asked for again/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Fill 2 sessions' })).toBeTruthy();
    expect(screen.queryByText(/A Fill does not ask again/)).toBeNull();
  });

  it('says nothing is left to fetch, and offers no Fill, when only the provider\'s history is out of reach', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`);
    await flushRead(
      opened,
      dataReport([dataSession(0, 'missing', { blocked_by: 'before_provider_history' }), dataSession(1, 'ready')]),
    );

    const fill = (await screen.findByRole('button', { name: 'Nothing to fill' })) as HTMLButtonElement;
    expect(fill.disabled).toBe(true);
    expect(screen.getByText('There is no session here a Fill could fetch.')).toBeTruthy();
    expect(screen.getByText(/1 are older than the provider serves and can no longer be fetched/)).toBeTruthy();
  });

  it('refuses a window Python refuses, with its reason, and shows no gap or Fill for it', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`);

    const request = await waitFor(() => opened.http.expectOne(READ_URL));
    request.flush(
      { detail: { reason: 'range_too_large', message: 'range is 2000 days; max is 1830' } },
      { status: 422, statusText: 'Unprocessable Entity' },
    );

    expect(await screen.findByText(/range is 2000 days; max is 1830/)).toBeTruthy();
    expect(screen.getByText(/never cut short/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Fill/ })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Gaps in the window' })).toBeNull();
  });

  it('says the whole-day files do not depend on the timeframe or the session the context names', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}&timeframe=5m&session=extended`);
    await flushRead(opened, dataReport([dataSession(0, 'ready')]));

    expect(
      await screen.findByText('The lake stores whole-day minute bars, so the 5m timeframe and the Extended hours session above does not change what is held.'),
    ).toBeTruthy();
  });

  it('reads the other tree once when it is picked, and not again for the same pick', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`);
    await flushRead(opened, dataReport([dataSession(0, 'ready')]));

    await userEvent.selectOptions(await screen.findByRole('combobox', { name: 'Price adjustment' }), 'raw');
    const read = await waitFor(() => opened.http.expectOne(READ_URL));
    expect(read.request.params.get('price_adjustment_mode')).toBe('raw');
    read.flush(dataReport([dataSession(0, 'ready')], { price_adjustment_mode: 'raw' }));
    await waitFor(() => expect(opened.router.url).toContain('adjustment=raw'));

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Price adjustment' }), 'raw');
    opened.harness.fixture.detectChanges();
    opened.http.expectNone(READ_URL);
  });

  it('keeps the tree in the place\'s address, so a reload reads it, and takes the key out when the default is picked again', async () => {
    const reloaded = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}&adjustment=raw`);

    const raw = await waitFor(() => reloaded.http.expectOne(READ_URL));
    expect(raw.request.params.get('price_adjustment_mode')).toBe('raw');
    raw.flush(dataReport([dataSession(0, 'ready')], { price_adjustment_mode: 'raw' }));
    expect((await screen.findByRole<HTMLSelectElement>('combobox', { name: 'Price adjustment' })).value).toBe('raw');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Price adjustment' }), 'polygon_split_adjusted');
    const split = await waitFor(() => reloaded.http.expectOne(READ_URL));
    expect(split.request.params.get('price_adjustment_mode')).toBe('polygon_split_adjusted');
    split.flush(dataReport([dataSession(0, 'ready')]));
    await waitFor(() => expect(reloaded.router.url).toBe(`/markets/SPY/data?${WINDOW_QUERY}&timeframe=1d&session=regular`));
    expect(reloaded.router.url).not.toContain('adjustment');
  });

  it('says in place that an adjustment this place does not offer cannot be used, and reads nothing for it', async () => {
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}&adjustment=dividend`);

    expect(await screen.findByText(/The price adjustment "dividend" is not one this place offers/)).toBeTruthy();
    opened.http.expectNone(READ_URL);

    // Committing a window writes a context the place offers, so the refused value does not outlive the pick.
    opened.context().commitWindow({ from: '2026-04-20', to: '2026-05-20' });
    await waitFor(() => expect(opened.router.url).not.toContain('dividend'));
    const read = await waitFor(() => opened.http.expectOne(READ_URL));
    expect(read.request.params.get('price_adjustment_mode')).toBe('polygon_split_adjusted');
  });

  describe('Fill', () => {
    it('asks the backfill job for exactly the span Python named, in the tree read, once however often it is pressed', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(opened, gappedReport());

      const fill = await screen.findByRole('button', { name: 'Fill 2 sessions' });
      await userEvent.click(fill);
      await userEvent.click(fill);

      await waitFor(() => expect(runner.run).toHaveBeenCalledTimes(1));
      expect(runner.windows).toEqual([
        {
          symbol: 'SPY',
          price_adjustment_mode: 'polygon_split_adjusted',
          start_trading_date_ms: Date.UTC(2026, 3, 17, 12),
          end_trading_date_ms: Date.UTC(2026, 3, 20, 12),
        },
      ]);
      expect(await screen.findByText('Filling 2 sessions of SPY.')).toBeTruthy();
      expect(screen.getByText('1 of 4 days of the span')).toBeTruthy();
      expect((screen.getByRole('button', { name: 'Fill 2 sessions' }) as HTMLButtonElement).disabled).toBe(true);
      expect((screen.getByRole('button', { name: 'Check again' }) as HTMLButtonElement).disabled).toBe(true);
    });

    it('reports what it added, what it reused and what is still missing, from the lake read again, and marks what it wrote as new evidence', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(opened, gappedReport());
      const marked = vi.spyOn(opened.store(), 'markChartStale');

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 2 sessions' }));
      await waitFor(() => expect(runner.run).toHaveBeenCalled());
      runner.end({ kind: 'completed', failure: null });

      // The lake is read again: two sessions arrived, one held session stays, one is still beyond the provider.
      await flushRead(
        opened,
        dataReport(
          [
            dataSession(0, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
            dataSession(1, 'ready'),
            dataSession(2, 'ready'),
            dataSession(3, 'ready'),
          ],
        ),
      );

      expect(await screen.findByText('Fill finished: 2 added, 1 already held, 1 still missing.')).toBeTruthy();
      expect(screen.getByText(/1: Not in the lake, and older than the provider serves/)).toBeTruthy();
      expect(marked, 'a chart or study held from before the Fill no longer answers the lake').toHaveBeenCalled();
      expect(screen.queryByText(/none missing/), 'it never reads as complete while a session is missing').toBeNull();
    });

    it('calls a fill that closed every gap complete, and only then', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(
        opened,
        dataReport([dataSession(0, 'ready'), dataSession(1, 'missing', { fetchable: true })], {
          fill_window: { start_trading_date_ms: Date.UTC(2026, 3, 16, 12), end_trading_date_ms: Date.UTC(2026, 3, 16, 12) },
        }),
      );

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 1 session' }));
      await waitFor(() => expect(runner.run).toHaveBeenCalled());
      runner.end({ kind: 'completed', failure: null });
      await flushRead(opened, dataReport([dataSession(0, 'ready'), dataSession(1, 'ready')]));

      expect(await screen.findByText(/Fill finished: 1 added, 1 already held, none missing/)).toBeTruthy();
    });

    it('never reads a failed fill as complete, and says what the lake holds after it', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(opened, gappedReport());

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 2 sessions' }));
      await waitFor(() => expect(runner.run).toHaveBeenCalled());
      runner.end({ kind: 'failed', code: 'provider_rate_limited', message: 'Too many requests' });
      await flushRead(opened, gappedReport());

      expect(await screen.findByText(/The fill stopped before it finished: 0 added, 1 already held, 3 still missing\./)).toBeTruthy();
      expect(screen.getByText(/Provider Rate Limited: Too many requests/)).toBeTruthy();
      expect(screen.queryByText(/Fill finished/)).toBeNull();
    });

    it('says a cancelled fill was cancelled, and keeps what it wrote', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(opened, gappedReport());

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 2 sessions' }));
      await userEvent.click(await screen.findByRole('button', { name: 'Cancel the fill' }));
      expect(runner.cancel).toHaveBeenCalledTimes(1);
      runner.end({ kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' });
      await flushRead(opened, gappedReport());

      expect(await screen.findByText(/The fill was cancelled: /)).toBeTruthy();
    });

    it.each([
      ['completes', { kind: 'completed', failure: null } as const],
      ['fails', { kind: 'failed', code: 'provider_rate_limited', message: 'Too many requests' } as const],
      ['is cancelled', { kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' } as const],
    ])('reads the lake catalog again when a fill %s, because the pickers\' held spans come from what it wrote', async (_name, end) => {
      const runner = fakeRunner();
      const catalog = fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' }]);
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider, provideFakeTickerCatalog(catalog)]);
      await flushRead(opened, gappedReport());
      const reloads = () => catalog.viewFor('polygon_split_adjusted').reloadCount;
      const before = reloads();

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 2 sessions' }));
      await waitFor(() => expect(runner.run).toHaveBeenCalled());
      expect.soft(reloads(), 'nothing is re-read while the job runs').toBe(before);
      runner.end(end);
      await flushRead(opened, gappedReport());
      await screen.findByText(/The fill (was cancelled|stopped before it finished)|Fill finished/);

      expect(reloads()).toBe(before + 1);
    });

    it('says so when the server could not be told to stop the fill, keeps Fill shut while the job may be writing, and lets the person try again', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(opened, gappedReport());
      runner.cancel.mockRejectedValueOnce(new Error('Network error'));

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 2 sessions' }));
      await userEvent.click(await screen.findByRole('button', { name: 'Cancel the fill' }));

      expect(await screen.findByText(/The server could not be told to stop it, so it may still be running\. Network error/)).toBeTruthy();
      expect.soft(screen.getByText('Filling 2 sessions of SPY.'), 'the job is still what is running').toBeTruthy();
      expect.soft((screen.getByRole('button', { name: 'Fill 2 sessions' }) as HTMLButtonElement).disabled).toBe(true);
      expect.soft(runner.run).toHaveBeenCalledTimes(1);

      await userEvent.click(screen.getByRole('button', { name: 'Cancel the fill' }));
      expect(runner.cancel, 'the cancel is offered again').toHaveBeenCalledTimes(2);
      runner.end({ kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' });
      await flushRead(opened, gappedReport());
      expect(await screen.findByText(/The fill was cancelled: /)).toBeTruthy();
    });

    it('says so when the lake cannot be read again after the job, instead of guessing what it added', async () => {
      const runner = fakeRunner();
      const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, [runner.provider]);
      await flushRead(opened, gappedReport());

      await userEvent.click(await screen.findByRole('button', { name: 'Fill 2 sessions' }));
      await waitFor(() => expect(runner.run).toHaveBeenCalled());
      runner.end({ kind: 'completed', failure: null });
      const read = await waitFor(() => opened.http.expectOne(READ_URL));
      read.flush({ detail: 'down' }, { status: 503, statusText: 'Service Unavailable' });

      expect(await screen.findByText('The fill ended, but the lake could not be read again, so what it added is not known.')).toBeTruthy();
    });
  });
});
