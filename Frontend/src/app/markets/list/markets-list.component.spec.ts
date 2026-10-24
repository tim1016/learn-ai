import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import type { Provider } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Router, provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import { InstrumentCardComponent } from '../../shared/ticker-range-picker/parts/instrument-card.component';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../shared/ticker-catalog/testing/fake-ticker-catalog';
import type { VendorSymbolEntry } from '../../shared/symbol-catalog/vendor-catalog.service';
import type { TickerOption } from '../../shared/ticker-range-picker';
import { formatTimestampDisplay } from '../../shared/timestamp';
import { type InstrumentFacts } from '../facts/instrument-facts.service';
import { OBSERVED_MS, archive, facts, quote } from '../facts/testing/facts-fixtures';
import { MarketsListComponent, PAGE_SIZE } from './markets-list.component';

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

const FACTS_URL = `${environment.pythonServiceUrl}/api/markets/instrument-facts`;

/** What the data plane answers for the symbols a request names. */
function answer(symbols: readonly string[], make: (symbol: string) => InstrumentFacts = (symbol) => facts(symbol)) {
  return { generated_ms: OBSERVED_MS, closes_adjustment: 'polygon_split_adjusted', facts: symbols.map(make) };
}

/** Every request a spec's rows asked the data plane for; the catalogs here are fakes and ask nothing. */
function asked(http: HttpTestingController) {
  return http.match((request) => request.url.startsWith(`${environment.pythonServiceUrl}/api/`));
}

const symbolsOf = (request: { request: { params: { get: (name: string) => string | null } } }) =>
  (request.request.params.get('symbols') ?? '').split(',');

/** `query` is the address's own query: what an old chain address leaves on `/markets` (`then=options&view=pricing&expiry=…`). */
async function renderMarkets(
  world = fakePickerWorld([held('SPY'), held('QQQ')], [vendor('SPY'), vendor('QQQ'), vendor('NVDA'), vendor('AMD')]),
  query = '',
  overrides: Provider[] = [],
) {
  const result = await render(MarketsListComponent, {
    providers: [provideRouter([{ path: '**', children: [] }]), provideHttpClient(), provideHttpClientTesting(), ...world.providers, ...overrides],
  });
  const router = TestBed.inject(Router);
  await router.navigateByUrl(query === '' ? '/' : `/?${query}`);
  result.fixture.detectChanges();
  const http = TestBed.inject(HttpTestingController);
  const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
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
    await renderMarkets(undefined, 'then=options');

    expect(screen.getAllByRole('row')[1].querySelector('a')?.getAttribute('href')).toBe('/markets/SPY/options');
    expect(screen.getByText('Pick an instrument. It opens on its Options tab.')).toBeTruthy();
  });

  it('opens the view an old address asked for and hands the pick every key it carried, from a row and from a search', async () => {
    const { fixture, world, navigate } = await renderMarkets(undefined, 'then=options&view=pricing&expiry=4070984400000&source=archive&date=2026-10-09');

    expect(screen.getByText('Pick an instrument. It opens on its Options tab, Pricing view.')).toBeTruthy();
    expect(screen.getAllByRole('row')[1].querySelector('a')?.getAttribute('href')).toBe(
      '/markets/SPY/options/pricing?expiry=4070984400000&source=archive&date=2026-10-09',
    );

    pick(fixture, { symbol: 'QQQ', name: 'QQQ Inc.', firstHeld: '2024-01-02', lastHeld: '2026-09-18' });

    expect(world.coverage.ensureCalls).toEqual([]);
    expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'QQQ', 'options', 'pricing'], {
      queryParams: { expiry: '4070984400000', source: 'archive', date: '2026-10-09' },
    });
  });

  it('opens the Options tab on its own when the view asked for is one it does not route, and still keeps the other keys', async () => {
    await renderMarkets(undefined, 'then=options&view=nonsense&expiry=4070984400000');

    expect(screen.getByText('Pick an instrument. It opens on its Options tab.')).toBeTruthy();
    expect(screen.getAllByRole('row')[1].querySelector('a')?.getAttribute('href')).toBe('/markets/SPY/options?expiry=4070984400000');
  });

  it('opens Chart when it was asked for a tab this place does not route', async () => {
    await renderMarkets(undefined, 'then=nonsense');

    expect(screen.getAllByRole('row')[1].querySelector('a')?.getAttribute('href')).toBe('/markets/SPY/chart');
    expect(screen.queryByText(/It opens on its/)).toBeNull();
  });

  describe('a page of instruments (T22)', () => {
    const forty = Array.from({ length: 40 }, (_, index) => held(`S${String.fromCharCode(65 + (index % 26))}${String.fromCharCode(65 + Math.floor(index / 26))}`));
    const fortyWorld = () => fakePickerWorld(forty, forty.map((row) => vendor(row.symbol)));

    it('draws a page of rows and asks the data plane once, for those rows, and starts no fill and no subscription', async () => {
      const subscriptions = { EventSource: vi.fn(), WebSocket: vi.fn() };
      vi.stubGlobal('EventSource', subscriptions.EventSource);
      vi.stubGlobal('WebSocket', subscriptions.WebSocket);
      try {
        const { world, http } = await renderMarkets(fortyWorld());

        expect(screen.getAllByRole('row')).toHaveLength(PAGE_SIZE + 1);
        const requests = asked(http);
        expect(requests.map((request) => request.request.url)).toEqual([FACTS_URL]);
        expect(symbolsOf(requests[0])).toEqual(forty.slice(0, PAGE_SIZE).map((row) => row.symbol));
        expect(requests[0].request.method).toBe('GET');
        expect(world.coverage.ensureCalls).toEqual([]);
        expect(world.coverage.refusals).toEqual([]);
        expect(subscriptions.EventSource).not.toHaveBeenCalled();
        expect(subscriptions.WebSocket).not.toHaveBeenCalled();
        http.verify();
      } finally {
        vi.unstubAllGlobals();
      }
    });

    it('asks for the next rows only when they are drawn, and never again for the rows already shown', async () => {
      const { http } = await renderMarkets(fortyWorld());
      const [first] = asked(http);
      first.flush(answer(symbolsOf(first)));

      await userEvent.click(screen.getByRole('button', { name: `Show ${PAGE_SIZE} more` }));

      expect(screen.getAllByRole('row')).toHaveLength(2 * PAGE_SIZE + 1);
      const more = asked(http);
      expect(more).toHaveLength(1);
      expect(symbolsOf(more[0])).toEqual(forty.slice(PAGE_SIZE).map((row) => row.symbol));
      expect(screen.queryByRole('button', { name: /Show .* more/ })).toBeNull();
      more[0].flush(answer(symbolsOf(more[0])));
      http.verify();
    });

    it('draws a bounded page, says how many are held, and leaves search to reach any of them', async () => {
      const many = Array.from({ length: 230 }, (_, index) => held(`T${index.toString(36).toUpperCase().padStart(3, 'A')}`));
      await renderMarkets(fakePickerWorld(many, many.map((row) => vendor(row.symbol))));

      expect(screen.getAllByRole('row')).toHaveLength(PAGE_SIZE + 1);
      expect(screen.getByText(/Showing 20 of 230 held instruments\. Search reaches any of them\./)).toBeTruthy();
    });
  });

  describe('each row\'s facts (PRD #3490 R08)', () => {
    it('states each row\'s price with its basis and as-of time, its day move and its options archive, with the held span said to be a span', async () => {
      const { http } = await renderMarkets();
      const [request] = asked(http);
      request.flush(answer(symbolsOf(request), (symbol) =>
        symbol === 'SPY'
          ? facts('SPY', quote({ basis: 'live_quote', not_used: [] }), archive({ kind: 'complete', complete_through_session_ms: OBSERVED_MS, missing_sessions: 0 }))
          : facts('QQQ', quote({ price: 612.4, basis: 'last_close' })),
      ));

      const [spy, qqq] = screen.getAllByRole('row').slice(1);
      expect(await within(spy).findByText('$759.97')).toBeTruthy();
      expect(within(spy).getByText(/Live quote, as of/).textContent).toContain(formatTimestampDisplay(OBSERVED_MS, { mode: 'local' }));
      expect(within(spy).getByText(/Options archive: every required day stored/)).toBeTruthy();
      expect(within(qqq).getByText('$612.40')).toBeTruthy();
      expect(within(qqq).getByText(/Last completed close, as of/)).toBeTruthy();
      expect(within(qqq).queryByText(/Options archive/), 'an underlying the archive does not track states nothing').toBeNull();
      expect(within(spy).getByText('2024-01-02 to 2026-09-18')).toBeTruthy();
    });

    it('says unavailable in a row whose price could not be read, keeps every row listed and openable, and reads only those again', async () => {
      const { http } = await renderMarkets();
      const [request] = asked(http);
      request.flush({ detail: 'The trading calendar could not name the last completed session.' }, { status: 503, statusText: 'Service Unavailable' });

      expect(await screen.findAllByText('Price unavailable')).toHaveLength(2);
      const rows = screen.getAllByRole('row').slice(1);
      expect(rows.map((row) => row.querySelector('a')?.getAttribute('href'))).toEqual(['/markets/SPY/chart', '/markets/QQQ/chart']);
      expect(screen.getByText(/Some prices could not be read/)).toBeTruthy();

      await userEvent.click(screen.getByRole('button', { name: 'Read them again' }));
      const again = asked(http);
      expect(again).toHaveLength(1);
      expect(symbolsOf(again[0])).toEqual(['SPY', 'QQQ']);
      again[0].flush(answer(symbolsOf(again[0])));
      await vi.waitFor(() => expect(screen.getAllByText('$759.97')).toHaveLength(2));
      expect(screen.queryByText(/Some prices could not be read/)).toBeNull();
    });

    it('draws a price still being read as being read, never as a figure', async () => {
      await renderMarkets();

      expect(screen.getAllByText('Reading the price…')).toHaveLength(2);
      expect(screen.queryByText(/^\$/)).toBeNull();
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
      await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'NVDA', 'chart'], { queryParams: {} }));
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
      await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'AMD', 'chart'], { queryParams: {} }));
    });

    it('opens a held symbol at once, without a fill', async () => {
      const { fixture, world, navigate } = await renderMarkets();

      pick(fixture, { symbol: 'QQQ', name: 'QQQ Inc.', firstHeld: '2024-01-02', lastHeld: '2026-09-18' });

      expect(world.coverage.ensureCalls).toEqual([]);
      expect(navigate).toHaveBeenCalledExactlyOnceWith(['/markets', 'QQQ', 'chart'], { queryParams: {} });
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
      await renderMarkets(fakePickerWorld([], []), '', [provideFakeTickerCatalog(catalog)]);

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
