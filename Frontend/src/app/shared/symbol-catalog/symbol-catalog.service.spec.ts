import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';

import { fakeTickerCatalog, provideFakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { FakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import {
  fakeAlpacaAssetCatalog,
  provideFakeAlpacaAssetCatalog,
  type FakeAlpacaAssetCatalog,
} from './testing/fake-symbol-catalog';
import { joinCatalog, SymbolCatalogService } from './symbol-catalog.service';
import type { AlpacaSymbolEntry } from './alpaca-asset-catalog.service';

function vendor(overrides: Partial<AlpacaSymbolEntry> = {}): AlpacaSymbolEntry {
  return {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    asset_class: 'us_equity',
    exchange: 'NASDAQ',
    status: 'active',
    tradable: true,
    ...overrides,
  };
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
      [vendor({ symbol: 'OLD', status: 'inactive', tradable: false })],
    );

    expect(joined[0]).toMatchObject({ symbol: 'OLD', lastHeld: '2020-01-31', delisted: true });
  });

  it('never offers a delisted vendor row the lake does not hold', () => {
    const joined = joinCatalog(
      [SPY],
      [vendor({ symbol: 'OLD', status: 'inactive', tradable: false })],
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
  let alpaca: FakeAlpacaAssetCatalog;

  beforeEach(() => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog([SPY]);
    alpaca = fakeAlpacaAssetCatalog();
    TestBed.configureTestingModule({
      providers: [provideFakeTickerCatalog(catalog), provideFakeAlpacaAssetCatalog(alpaca)],
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

  it('reloads both sources', () => {
    const service = TestBed.inject(SymbolCatalogService);
    service.viewFor('polygon_split_adjusted').reload();

    expect(catalog.view.reloadCount).toBe(1);
    expect(alpaca.reloadCount).toBe(1);
  });

  it('caches one view per mode, like the lake catalog it wraps', () => {
    const service = TestBed.inject(SymbolCatalogService);

    expect(service.viewFor('raw')).toBe(service.viewFor('raw'));
    expect(service.viewFor('raw')).not.toBe(service.viewFor('polygon_split_adjusted'));
  });
});
