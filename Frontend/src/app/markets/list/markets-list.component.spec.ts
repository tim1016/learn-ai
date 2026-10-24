import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import type { Provider } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Router, provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { InstrumentCardComponent } from '../../shared/ticker-range-picker/parts/instrument-card.component';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../shared/ticker-catalog/testing/fake-ticker-catalog';
import type { VendorSymbolEntry } from '../../shared/symbol-catalog/vendor-catalog.service';
import type { TickerOption } from '../../shared/ticker-range-picker';
import { MarketsListComponent } from './markets-list.component';

const held = (symbol: string): TickerOption => ({
  symbol,
  name: `${symbol} Inc.`,
  firstHeld: '2024-01-02',
  lastHeld: '2026-09-18',
});

const vendor = (symbol: string, status: 'active' | 'inactive' = 'active'): VendorSymbolEntry => ({
  symbol,
  name: `${symbol} Inc.`,
  exchange: 'XNAS',
  asset_class: 'us_equity',
  status,
});

async function renderMarkets(
  world = fakePickerWorld([held('SPY'), held('QQQ')], [vendor('SPY'), vendor('QQQ'), vendor('NVDA'), vendor('AMD')]),
  inputs: { then?: string } = {},
  overrides: Provider[] = [],
) {
  const result = await render(MarketsListComponent, {
    inputs,
    providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting(), ...world.providers, ...overrides],
  });
  const http = TestBed.inject(HttpTestingController);
  const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  return { ...result, world, http, navigate };
}

/** The shared card's own pick, as a click on a row of its dropdown makes it. */
function pick(fixture: { debugElement: { query: (predicate: ReturnType<typeof By.directive>) => { componentInstance: unknown } } }, option: TickerOption): void {
  (fixture.debugElement.query(By.directive(InstrumentCardComponent)).componentInstance as InstrumentCardComponent).pickTicker(option);
}

describe('MarketsListComponent', () => {
  it('lists the instruments the lake holds, titled by its menu label, each leading to its Chart', async () => {
    await renderMarkets();

    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Markets');
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((row) => row.querySelector('a')?.getAttribute('href'))).toEqual(['/markets/SPY/chart', '/markets/QQQ/chart']);
    expect(screen.queryByText('NVDA Inc.'), 'an unheld symbol is searched for, not listed').toBeNull();
  });

  it('lists a held instrument with no quote to show, with its held span said to be a span, and keeps a delisted one', async () => {
    await renderMarkets(fakePickerWorld([held('SPY'), held('XLNX')], [vendor('SPY'), vendor('XLNX', 'inactive')]));

    expect(screen.getByText('XLNX Inc.')).toBeTruthy();
    expect(screen.getByText('delisted')).toBeTruthy();
    expect(screen.getAllByText('2024-01-02 to 2026-09-18')).toHaveLength(2);
    expect(screen.getByText(/not a promise that every session between them is held/)).toBeTruthy();
  });

  it('opens the tab it was asked to, and says which tab that is, as no instrument was named', async () => {
    await renderMarkets(undefined, { then: 'options' });

    expect(screen.getAllByRole('row')[1].querySelector('a')?.getAttribute('href')).toBe('/markets/SPY/options');
    expect(screen.getByText('Pick an instrument. It opens on its Options tab.')).toBeTruthy();
  });

  it('opens Chart when it was asked for a tab this place does not route', async () => {
    await renderMarkets(undefined, { then: 'nonsense' });

    expect(screen.getAllByRole('row')[1].querySelector('a')?.getAttribute('href')).toBe('/markets/SPY/chart');
    expect(screen.queryByText(/It opens on its/)).toBeNull();
  });

  describe('a page of instruments (T22)', () => {
    it('starts no fill, no request and no subscription for any row', async () => {
      const rows = Array.from({ length: 40 }, (_, index) => held(`S${String.fromCharCode(65 + (index % 26))}${String.fromCharCode(65 + Math.floor(index / 26))}`));
      const { world, http } = await renderMarkets(fakePickerWorld(rows, rows.map((row) => vendor(row.symbol))));

      expect(screen.getAllByRole('row')).toHaveLength(41);
      expect(world.coverage.ensureCalls).toEqual([]);
      expect(world.coverage.refusals).toEqual([]);
      // Nothing was asked of the network by the page or by any row.
      http.verify();
    });

    it('draws a bounded number of rows and says so, leaving search to reach the rest', async () => {
      const many = Array.from({ length: 230 }, (_, index) => held(`T${index.toString(36).toUpperCase().padStart(3, 'A')}`));
      await renderMarkets(fakePickerWorld(many, many.map((row) => vendor(row.symbol))));

      expect(screen.getAllByRole('row')).toHaveLength(201);
      expect(screen.getByText('Showing the first 200 of 230 held instruments. Search reaches the rest.')).toBeTruthy();
    });
  });

  describe('searching (T04)', () => {
    it('fills a symbol the lake does not hold, then opens it, and only then', async () => {
      const { fixture, world, navigate } = await renderMarkets();
      world.coverage.holdNext = true;

      pick(fixture, { symbol: 'NVDA', name: 'NVDA Inc.' });
      expect(world.coverage.ensureCalls).toEqual([{ symbol: 'NVDA', mode: 'polygon_split_adjusted' }]);
      expect(navigate).not.toHaveBeenCalled();

      world.coverage.held[0].resolve(true);
      await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'NVDA', 'chart']));
    });

    it('never opens the first symbol when a second was picked while it filled, and opens the second when it is confirmed', async () => {
      const { fixture, world, navigate } = await renderMarkets();
      world.coverage.holdNext = true;
      pick(fixture, { symbol: 'NVDA', name: 'NVDA Inc.' });
      world.coverage.holdNext = true;
      pick(fixture, { symbol: 'AMD', name: 'AMD Inc.' });
      const [nvda, amd] = world.coverage.held;

      expect(nvda.cancelCalls).toBe(1);
      nvda.resolve(true);
      await new Promise((resolve) => setTimeout(resolve));
      expect(navigate).not.toHaveBeenCalled();

      amd.resolve(true);
      await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'AMD', 'chart']));
    });

    it('opens a held symbol at once, without a fill', async () => {
      const { fixture, world, navigate } = await renderMarkets();

      pick(fixture, { symbol: 'QQQ', name: 'QQQ Inc.', firstHeld: '2024-01-02', lastHeld: '2026-09-18' });

      expect(world.coverage.ensureCalls).toEqual([]);
      expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'QQQ', 'chart']);
    });

    it('does not open a symbol whose fill failed', async () => {
      const { fixture, world, navigate } = await renderMarkets();
      world.coverage.ensureResults.push(false);

      pick(fixture, { symbol: 'NVDA', name: 'NVDA Inc.' });
      await new Promise((resolve) => setTimeout(resolve));

      expect(navigate).not.toHaveBeenCalled();
    });
  });

  describe('when a catalog cannot answer', () => {
    it('keeps listing the held instruments and says the listing is degraded, with a way to read it again', async () => {
      const world = fakePickerWorld([held('SPY')], [vendor('SPY')]);
      world.vendor.unavailable.set('The vendor catalog is down.');
      await renderMarkets(world);

      expect(screen.getByText(/search offers only what the lake holds/)).toBeTruthy();
      expect(screen.getByText('The vendor catalog is down.')).toBeTruthy();
      expect(screen.getAllByRole('row')).toHaveLength(2);
      await userEvent.click(screen.getByRole('button', { name: 'Read the listing again' }));
      expect(world.vendor.reloadCount).toBe(1);
    });

    it('does not claim the lake holds nothing when the lake could not be read, and offers to read it again', async () => {
      const catalog = fakeTickerCatalog([]);
      catalog.view.unavailable.set('The lake could not be read.');
      await renderMarkets(fakePickerWorld([], []), {}, [provideFakeTickerCatalog(catalog)]);

      expect(screen.getByText('The instruments the lake holds could not be listed.')).toBeTruthy();
      expect(screen.getByText('The lake could not be read.')).toBeTruthy();
      expect(screen.queryByText(/holds no instruments yet/)).toBeNull();
      await userEvent.click(screen.getByRole('button', { name: 'Read the lake again' }));
      expect(catalog.view.reloadCount).toBe(1);
    });

    it('says the lake holds no instruments only when it answered that it holds none', async () => {
      await renderMarkets(fakePickerWorld([], []));

      expect(screen.getByText('The lake holds no instruments yet.')).toBeTruthy();
    });
  });
});
