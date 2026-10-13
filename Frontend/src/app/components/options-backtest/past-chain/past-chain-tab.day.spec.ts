import { screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../testing/page-actions';
import { PastChainRefusedError } from './past-chain.service';
import type { ChainDay } from './past-chain.types';
import { chainDay, EXPIRY_MS, summary, tag, TRADING_DATE, TRADING_DATE_MS } from './testing/fixtures';
import { expiriesTable, expiryRow, fakeChainService, pickDate, renderChainTab } from './testing/render-chain';

describe('the Past chain tab, before a date is picked', () => {
  it('asks for a date and reads nothing', async () => {
    const service = fakeChainService(vi);

    await renderChainTab(service);

    expect(screen.getByText(/Pick an underlying and a trading date/)).toBeTruthy();
    expect(service.day).not.toHaveBeenCalled();
    expect(service.expiry).not.toHaveBeenCalled();
  });

  it('starts from the underlying it is given, and says what its date and time boxes mean', async () => {
    await renderChainTab(fakeChainService(vi), 'AAPL');

    expect(screen.getByRole('combobox', { name: /chain underlying/i })).toBeTruthy();
    expect(reads(screen.getByRole('combobox', { name: /chain underlying/i }))).toContain('AAPL');
    expect(screen.getByText(/Until you type a time, the chain is read at the close of the session/)).toBeTruthy();
  });
});

describe('the Past chain tab, started on a date', () => {
  it('reads that day at once, with the date in its field, as if it had been picked', async () => {
    const service = fakeChainService(vi);

    await renderChainTab(service, 'SPY', null, TRADING_DATE);

    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    expect(service.day).toHaveBeenCalledTimes(1);
    expect(service.day.mock.calls[0]?.[0]).toEqual({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS });
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
  });
});

describe('the listing of a picked date', () => {
  it('is read for the underlying and the New York midnight of the date', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);

    pickDate(TRADING_DATE);

    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    expect(service.day).toHaveBeenCalledTimes(1);
    expect(service.day.mock.calls[0]?.[0]).toMatchObject({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS });
  });

  it('lists every expiry in the order served, each date as the New York date', async () => {
    await renderChainTab(fakeChainService(vi));

    pickDate(TRADING_DATE);

    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    const dates = within(expiriesTable())
      .getAllByRole('rowheader')
      .map((header) => reads(header));
    expect(dates).toEqual(['2026-10-09', '2026-10-16', '2026-10-30']);
    for (const stamp of expiriesTable().querySelectorAll('app-timestamp-display')) {
      expect(stamp.getAttribute('data-timestamp-mode')).toBe('date-et');
    }
  });

  it('puts the tag of each expiry in a pill that carries its word', async () => {
    await renderChainTab(fakeChainService(vi));
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    expect(reads(expiryRow('2026-10-16'))).toContain('Monthly');
    expect(reads(expiryRow('2026-10-30'))).toContain('Weekly');
    expect(reads(expiryRow('2026-10-09'))).toContain('Daily');
  });

  it('says Quarterly and Unknown in words too, and shows the evidence for the tag as served', async () => {
    const service = fakeChainService(vi, {
      day: vi.fn(async () =>
        chainDay({
          expiries: [
            summary(EXPIRY_MS['2026-12-18'], tag('quarterly', { evidence: 'the last session of its quarter' })),
            summary(EXPIRY_MS['2026-10-30'], tag('unknown', { evidence: 'the trading date is before 2025-01-01, from when expiries are tagged' })),
          ],
        }),
      ),
    });
    await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    expect(reads(expiryRow('2026-12-18'))).toContain('Quarterly');
    expect(reads(expiryRow('2026-12-18'))).toContain('the last session of its quarter');
    expect(reads(expiryRow('2026-10-30'))).toContain('Unknown');
    expect(reads(expiryRow('2026-10-30'))).toContain('the trading date is before 2025-01-01, from when expiries are tagged');
  });

  it('shows Expires today apart from the series: only the expiry that is the trading date has it', async () => {
    await renderChainTab(fakeChainService(vi));
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    expect(reads(expiryRow('2026-10-09'))).toContain('Expires today');
    expect(reads(expiryRow('2026-10-16'))).not.toContain('Expires today');
    expect(reads(expiryRow('2026-10-30'))).not.toContain('Expires today');
  });

  it('keeps a Monthly on its last day a Monthly that expires today, and not a Daily', async () => {
    const service = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ expiries: [summary(EXPIRY_MS['2026-10-09'], tag('monthly', { expires_today: true, days_to_expiry: 0, evidence: 'the third Friday of its month' }))] })),
    });
    await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    const row = reads(expiryRow('2026-10-09'));

    expect(row).toContain('Monthly');
    expect(row).toContain('Expires today');
    expect(row).not.toContain('Daily');
  });

  it('shows the counts the server gave, as served, and what was left out of them', async () => {
    const service = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ rows_left_out_unusable: 7, rows_left_out_expired: 3, expiries: [summary(EXPIRY_MS['2026-10-16'], tag('monthly'), { calls: 120, standard_calls: 118, puts: 119, standard_puts: 117, tag: tag('monthly', { days_to_expiry: 7 }) })] })),
    });
    await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    const cells = within(expiryRow('2026-10-16')).getAllByRole('cell').map((cell) => reads(cell));

    expect(cells.slice(1, 6)).toEqual(['7', '120', '118', '119', '117']);
    expect(screen.getByText(/7 name no contract a backtest can use/)).toBeTruthy();
    expect(screen.getByText(/3 are contracts that had already expired/)).toBeTruthy();
  });
});

describe('the states of a day that has no expiries to list', () => {
  it('says the date is not a trading day, in the server own words', async () => {
    const service = fakeChainService(vi, {
      day: vi.fn(async () => {
        throw new PastChainRefusedError('OPTIONS_BACKTEST_NOT_A_SESSION', '2026-10-10 is a weekend day: the exchange had no session that day', 400);
      }),
    });
    await renderChainTab(service);

    pickDate('2026-10-10');

    expect(await screen.findByText('Not a trading day.')).toBeTruthy();
    expect(screen.getByText(/2026-10-10 is a weekend day: the exchange had no session that day/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says the listing is not stored, and offers the download', async () => {
    const service = fakeChainService(vi, { day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })) });
    await renderChainTab(service);

    pickDate(TRADING_DATE);

    expect(await screen.findByText('The listing for this date is not stored.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download missing data' })).toBeTruthy();
  });

  it('says the listing is stored and holds nothing a backtest can use, with the rows it left out', async () => {
    const service = fakeChainService(vi, { day: vi.fn(async () => chainDay({ expiries: [], rows_left_out_unusable: 12 })) });
    await renderChainTab(service);

    pickDate(TRADING_DATE);

    expect(await screen.findByText('The listing for this date is stored, but it holds no contract a backtest can use.')).toBeTruthy();
    expect(screen.getByText(/12 name no contract a backtest can use/)).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says the code of a listing state it does not know, in words, and is neither a table nor a listing that holds nothing', async () => {
    const odd = chainDay({ listing_state: 'partial' as ChainDay['listing_state'], expiries: [] });
    await renderChainTab(fakeChainService(vi, { day: vi.fn(async () => odd) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText('This page does not know this state of the listing for this date: Partial.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText(/holds no contract a backtest can use/)).toBeNull();
    expect(screen.queryByText(/is not stored/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says the stored listing cannot be read, with the reason the server gave, and offers no download', async () => {
    const service = fakeChainService(vi, {
      day: vi.fn(async () => chainDay({ listing_state: 'unreadable', listing_problem: 'the listing for SPY on 2026-10-09 cannot be read: the stored file is damaged (OptionListingFileError)', expiries: [] })),
    });
    await renderChainTab(service);

    pickDate(TRADING_DATE);

    expect(await screen.findByText('The stored listing for this date cannot be read.')).toBeTruthy();
    expect(screen.getByText(/the stored file is damaged \(OptionListingFileError\)/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says the catalog cannot be reached, with the server code, and tries again on request', async () => {
    const day = vi
      .fn()
      .mockRejectedValueOnce(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', "The data lake's catalog cannot be reached, so what is stored cannot be read.", 503))
      .mockResolvedValue(chainDay());
    await renderChainTab(fakeChainService(vi, { day }));

    pickDate(TRADING_DATE);

    expect((await screen.findByRole('alert')).textContent).toContain("The data lake's catalog cannot be reached");
    expect(screen.getByText('Options Backtest Lake Catalog Unavailable')).toBeTruthy();
    screen.getByRole('button', { name: 'Try again' }).click();
    expect(await screen.findByRole('table', { name: /Every expiry the listing held/ })).toBeTruthy();
    expect(day).toHaveBeenCalledTimes(2);
  });

  it('says a call that never reached the server could not be read, and offers another try', async () => {
    const service = fakeChainService(vi, { day: vi.fn().mockRejectedValue(new Error('network down')) });
    await renderChainTab(service);

    pickDate(TRADING_DATE);

    expect((await screen.findByRole('alert')).textContent).toContain('The listing could not be read.');
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();
  });
});
