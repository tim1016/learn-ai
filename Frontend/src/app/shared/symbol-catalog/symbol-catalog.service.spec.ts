import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';

import { fakeTickerCatalog, provideFakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { FakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import {
  fakeVendorCatalog,
  provideFakeVendorCatalog,
  type FakeVendorCatalog,
} from './testing/fake-symbol-catalog';
import { joinCatalog, SymbolCatalogService } from './symbol-catalog.service';
import type { VendorSymbolEntry } from './vendor-catalog.service';

function vendor(
  // `asset_class`/`status` are Literal-typed on the wire; the string-typed
  // override here exists only so boundary tests can hand a row the catalog
  // must refuse.
  overrides:
    & Partial<Omit<VendorSymbolEntry, 'asset_class' | 'status'>>
    & Partial<Record<'asset_class' | 'status', string>> = {},
): VendorSymbolEntry {
  return {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    asset_class: 'us_equity',
    exchange: 'NASDAQ',
    status: 'active',

    ...overrides,
  } as VendorSymbolEntry;
}

const SPY: TickerOption = {
  symbol: 'SPY',
  name: 'SPDR S&P 500 ETF',
  exchange: 'ARCA',
  firstHeld: '2024-05-20',
  lastHeld: '2025-04-30',
};

describe('joinCatalog', () => {
  it('keeps the lake ranking and appends vendor-only actives alphabetically', () => {
    const joined = joinCatalog(
      [SPY, { symbol: 'QQQ', name: 'Invesco QQQ', lastHeld: '2025-04-30' }],
      [vendor({ symbol: 'ZZZ' }), vendor({ symbol: 'AAPL' })],
    );

    expect(joined.map((row) => row.symbol)).toEqual(['SPY', 'QQQ', 'AAPL', 'ZZZ']);
    // Vendor-only rows carry no coverage: the lake has never held them.
    expect(joined[2]).toMatchObject({ symbol: 'AAPL', firstHeld: null, lastHeld: null, delisted: false });
  });

  it('badges a held symbol the vendor has delisted instead of dropping it', () => {
    const joined = joinCatalog(
      [{ symbol: 'OLD', name: 'Old Corp', lastHeld: '2020-01-31' }],
      [vendor({ symbol: 'OLD', status: 'inactive', })],
    );

    expect(joined[0]).toMatchObject({ symbol: 'OLD', lastHeld: '2020-01-31', delisted: true });
  });

  it('drops a lake row the vendor classifies outside the backfillable universe', () => {
    // A legacy import the lake holds but the vendor marks as a crypto pair
    // can never be covered by the gate — bars or no bars, it is not
    // offerable. The wire's `asset_class` Literal makes such a row
    // unreachable from today's catalog; this pins the client-side boundary
    // for the day the walk widens.
    const joined = joinCatalog(
      [{ symbol: 'BTCUSD', name: 'Bitcoin', lastHeld: '2026-01-31' }, SPY],
      [vendor({ symbol: 'BTCUSD', asset_class: 'crypto', exchange: null })],
    );

    expect(joined.map((row) => row.symbol)).toEqual(['SPY']);
  });

  it('never offers a delisted vendor row the lake does not hold', () => {
    const joined = joinCatalog(
      [SPY],
      [vendor({ symbol: 'OLD', status: 'inactive', })],
    );

    expect(joined.map((row) => row.symbol)).toEqual(['SPY']);
  });

  it('offers no vendor class the lake could never backfill', () => {
    const joined = joinCatalog(
      [],
      [vendor({ symbol: 'BTCUSD', asset_class: 'crypto', exchange: null })],
    );

    expect(joined).toEqual([]);
  });

  it('with no vendor answer at all, the lake answer stands alone', () => {
    const joined = joinCatalog([SPY], null);

    expect(joined).toEqual([{ ...SPY, delisted: false }]);
  });

  it('falls back to the ticker when the vendor names nothing', () => {
    const joined = joinCatalog([], [vendor({ symbol: 'ZZZ', name: null })]);

    expect(joined[0].name).toBe('ZZZ');
  });
});

describe('SymbolCatalogService', () => {
  let catalog: FakeTickerCatalog;
  let alpaca: FakeVendorCatalog;

  beforeEach(() => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog([SPY]);
    alpaca = fakeVendorCatalog();
    TestBed.configureTestingModule({
      providers: [provideFakeTickerCatalog(catalog), provideFakeVendorCatalog(alpaca)],
    });
  });

  it('joins the lake view with the vendor catalog for the requested tree', () => {
    alpaca.entries.set([vendor({ symbol: 'TSLA' })]);
    const service = TestBed.inject(SymbolCatalogService);
    const view = service.viewFor('polygon_split_adjusted');

    expect(view.pool().map((row) => row.symbol)).toEqual(['SPY', 'TSLA']);
    expect(view.recent()).toEqual(catalog.view.recent());
  });

  it('degrades visibly — vendor dark keeps the lake pool and flags it', () => {
    alpaca.entries.set(null);
    alpaca.unavailable.set('catalog down');
    const service = TestBed.inject(SymbolCatalogService);
    const view = service.viewFor('polygon_split_adjusted');

    expect(view.pool().map((row) => row.symbol)).toEqual(['SPY']);
    expect(view.degraded()).toBe(true);
    // A degraded view is not an unavailable one: the pool is honest.
    expect(view.unavailable()).toBeNull();
  });

  it('is unavailable only when the lake itself failed', () => {
    catalog.view.unavailable.set('The data lake is unreachable.');
    alpaca.unavailable.set('catalog down');
    const service = TestBed.inject(SymbolCatalogService);
    const view = service.viewFor('polygon_split_adjusted');

    expect(view.unavailable()).toBe('The data lake is unreachable.');
    expect(view.degraded()).toBe(true);
  });

  it('reloads only the lake on an ordinary refresh; the vendor retries separately', () => {
    const service = TestBed.inject(SymbolCatalogService);
    const view = service.viewFor('polygon_split_adjusted');

    // A dropdown-open refresh costs no vendor traffic: the catalog is read
    // once per tab and re-fetched only through the degraded banner's Retry.
    view.reload();
    expect(catalog.view.reloadCount).toBe(1);
    expect(alpaca.reloadCount).toBe(0);

    view.retryVendor();
    expect(catalog.view.reloadCount).toBe(1);
    expect(alpaca.reloadCount).toBe(1);
  });

  it('caches one view per mode, like the lake catalog it wraps', () => {
    const service = TestBed.inject(SymbolCatalogService);

    expect(service.viewFor('raw')).toBe(service.viewFor('raw'));
    expect(service.viewFor('raw')).not.toBe(service.viewFor('polygon_split_adjusted'));
  });
});
