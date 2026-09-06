import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';

import { DataLakeService } from '../data-lake/data-lake.service';
import type { DataLakeRead, StorageSummaryResponse } from '../data-lake/data-lake.types';
import { TickerCatalogService } from './ticker-catalog.service';

/** 2024-08-06 and 2026-09-04, both 09:30 ET — the anchor a trading date carries. */
const AUG_2024 = 1722951000000;
const SEP_2026 = 1788528600000;

function summary(
  symbols: StorageSummaryResponse['symbols'],
): DataLakeRead<StorageSummaryResponse> {
  return { kind: 'ok', value: { market: 'usa', kinds: [], symbols } };
}

/** Records the `(market, mode)` each `storageSummary` call asked for. */
const asked: (readonly [string, string | undefined])[] = [];

async function catalogFor(read: DataLakeRead<StorageSummaryResponse>): Promise<TickerCatalogService> {
  TestBed.resetTestingModule();
  asked.length = 0;
  TestBed.configureTestingModule({
    providers: [
      {
        provide: DataLakeService,
        useValue: {
          storageSummary: async (market: string, mode?: string) => {
            asked.push([market, mode]);
            return read;
          },
        },
      },
    ],
  });
  const service = TestBed.inject(TickerCatalogService);
  // Reading the signal registers the resource; `tick` runs the effect that
  // starts the loader, and the stub resolves on the next macrotask.
  service.pool();
  TestBed.tick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  TestBed.tick();
  return service;
}

describe('TickerCatalogService', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('offers every symbol the lake holds bars for', async () => {
    const service = await catalogFor(
      summary([
        { symbol: 'GLD', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 1046 },
        { symbol: 'SPY', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 2290 },
      ]),
    );

    expect(service.pool().map((t) => t.symbol)).toEqual(['SPY', 'GLD']);
    expect(service.unavailable()).toBeNull();
  });

  it('labels a known symbol and falls back to the ticker for an unknown one', async () => {
    const service = await catalogFor(
      summary([
        { symbol: 'GLD', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 10 },
        { symbol: 'ZZZZ', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 9 },
      ]),
    );

    const [gld, unknown] = service.pool();
    expect(gld.name).toBe('SPDR Gold Shares');
    expect(gld.exchange).toBe('ARCA');
    // A fresh backfill must be pickable before anyone writes its label.
    expect(unknown.name).toBe('ZZZZ');
    expect(unknown.exchange).toBeUndefined();
  });

  it('resolves the coverage span in ET, not the viewer zone', async () => {
    const service = await catalogFor(
      summary([
        { symbol: 'SPY', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 1 },
      ]),
    );

    expect(service.pool()[0].first).toBe('2024-08-06');
    expect(service.pool()[0].last).toBe('2026-09-04');
  });

  it('drops a catalogued symbol that holds no readable day', async () => {
    const service = await catalogFor(
      summary([
        { symbol: 'SPY', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 1 },
        { symbol: 'EMPTY', first_trading_date_ms: null, last_trading_date_ms: null, artifact_count: 0 },
        { symbol: 'NOSPAN', first_trading_date_ms: null, last_trading_date_ms: null, artifact_count: 4 },
      ]),
    );

    expect(service.pool().map((t) => t.symbol)).toEqual(['SPY']);
  });

  it('keeps only the seed recents the lake actually holds', async () => {
    const service = await catalogFor(
      summary([
        { symbol: 'SPY', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 2 },
        { symbol: 'AAPL', first_trading_date_ms: AUG_2024, last_trading_date_ms: SEP_2026, artifact_count: 1 },
      ]),
    );

    // QQQ is a seed recent, but the lake holds nothing for it.
    expect(service.recent()).toEqual(['SPY', 'AAPL']);
  });

  it('reports an unreachable lake rather than offering a canned list', async () => {
    const service = await catalogFor({
      kind: 'unavailable',
      message: 'The data plane did not answer.',
    });

    expect(service.pool()).toEqual([]);
    expect(service.unavailable()).toBe('The data plane did not answer.');
  });

  // The adjustment mode is a segment of the lake root (#1866) and a backtest
  // resolves `adjusted=True` by default. Asking the catalog for every mode
  // would offer a symbol backfilled only in `raw` — which is what the Data
  // Lake Observatory produces by default — and the run would then refuse it.
  it('asks only for the tree a run actually reads', async () => {
    await catalogFor(summary([]));

    expect(asked).toEqual([['usa', 'polygon_split_adjusted']]);
  });
});
