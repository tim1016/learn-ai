import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { JobsService, type JobStreamEvent } from '../services/jobs.service';
import { DataLakeService, type BackfillDefaults, type SymbolCoverageSpan } from '../shared/data-lake';
import { CoverageGateController } from '../shared/symbol-catalog/coverage-gate.controller';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../shared/symbol-catalog/testing/fake-symbol-catalog';
import type { VendorSymbolEntry } from '../shared/symbol-catalog/vendor-catalog.service';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../shared/ticker-catalog/testing/fake-ticker-catalog';
import { InstrumentAdmissionService } from './instrument-admission.service';
import { InstrumentContextService } from './instrument-context.service';

/*
 * T04 against the real shared gate: the place is one more caller of
 * `EnsureCoverageService`, so what it may and may not do to another caller's
 * fill, and what an old completion may do to it, is the contract proven here.
 * Only the job stream and the lake's own read are faked.
 */

const MODE = 'polygon_split_adjusted';
const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'digest@sha256:deadbeef',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
  provider_history_start_ms: 1632398400000,
};
const AUG_2024 = 1722951000000;
const SEP_2026 = 1788528600000;

const vendor = (symbol: string): VendorSymbolEntry => ({ symbol, name: `${symbol} Inc.`, exchange: 'XNAS', asset_class: 'us_equity', status: 'active' });

const span = (symbol: string): SymbolCoverageSpan => ({
  symbol,
  first_trading_date_ms: AUG_2024,
  last_trading_date_ms: SEP_2026,
  artifact_count: 4,
});

function configure(startingSymbol: string) {
  const handlers = new Map<string, (event: JobStreamEvent) => void>();
  const started: { payload: Record<string, unknown> }[] = [];
  const cancelled: string[] = [];
  const lake: { symbols: SymbolCoverageSpan[] } = { symbols: [span('SPY')] };
  const symbol = signal(startingSymbol);
  TestBed.configureTestingModule({
    providers: [
      provideFakeTickerCatalog(fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR', firstHeld: '2024-05-20', lastHeld: '2026-09-04' }])),
      provideFakeVendorCatalog(fakeVendorCatalog([vendor('SPY'), vendor('NVDA'), vendor('AMD')])),
      {
        provide: JobsService,
        useValue: {
          startJob: async (_type: string, payload: Record<string, unknown>) => {
            started.push({ payload });
            const id = `job-${started.length}`;
            handlers.set(id, () => undefined);
            return id;
          },
          onEvent: (id: string, handler: (event: JobStreamEvent) => void) => {
            handlers.set(id, handler);
            return () => handlers.delete(id);
          },
          cancelJob: async (id: string) => {
            cancelled.push(id);
          },
        },
      },
      {
        provide: DataLakeService,
        useValue: {
          backfillDefaults: async () => ({ kind: 'ok', value: DEFAULTS }),
          storageSummary: async () => ({ kind: 'ok', value: { market: 'usa', kinds: [], symbols: lake.symbols } }),
        },
      },
      { provide: InstrumentContextService, useValue: { symbol } },
      InstrumentAdmissionService,
    ],
  });
  const admission = TestBed.inject(InstrumentAdmissionService);
  return {
    admission,
    symbol,
    started,
    cancelled,
    complete: async (job: string, covered: readonly string[]) => {
      lake.symbols = [span('SPY'), ...covered.map(span)];
      handlers.get(job)?.({ type: 'job.completed' });
      await settle();
    },
  };
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  TestBed.tick();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('InstrumentAdmissionService', () => {
  it('opens a held instrument at once, and starts no fill', async () => {
    const { admission, started } = configure('SPY');
    await settle();

    expect(admission.state()).toMatchObject({ kind: 'ready', row: { symbol: 'SPY' } });
    expect(started).toEqual([]);
  });

  it('fills a listed instrument the lake does not hold, and opens it when the lake confirms the bars', async () => {
    const { admission, started, complete } = configure('NVDA');
    await settle();

    expect(admission.state()).toMatchObject({ kind: 'filling', gate: { symbol: 'NVDA', phase: 'backfilling' } });
    expect(started).toHaveLength(1);
    expect(started[0].payload['spec']).toMatchObject({ symbols: ['NVDA'], price_adjustment_mode: MODE });

    await complete('job-1', ['NVDA']);

    expect(admission.state()).toMatchObject({ kind: 'ready', row: { symbol: 'NVDA' } });
    // The catalog's own read has not caught up yet: it must not start a second fill.
    expect(started).toHaveLength(1);
  });

  it('is not an instrument at all when nothing lists it, and fills nothing', async () => {
    const { admission, started } = configure('ZZZZ');
    await settle();

    expect(admission.state().kind).toBe('unlisted');
    expect(started).toEqual([]);
  });

  describe('with another caller waiting on the same fill (T04)', () => {
    it('leaving does not cancel the other caller\'s fill, which still completes for it', async () => {
      const { admission, symbol, started, cancelled, complete } = configure('NVDA');
      await settle();
      const other = TestBed.runInInjectionContext(() => new CoverageGateController(() => MODE));
      const committed = vi.fn();
      other.admit({ symbol: 'NVDA', mode: MODE, lakeDark: () => null, commit: committed });
      await settle();
      // Two callers, one run.
      expect(started).toHaveLength(1);

      // The place moves to a held instrument and lets go of NVDA's fill.
      symbol.set('SPY');
      await settle();
      expect(admission.state()).toMatchObject({ kind: 'ready', row: { symbol: 'SPY' } });
      expect(cancelled).toEqual([]);

      await complete('job-1', ['NVDA']);

      expect(committed).toHaveBeenCalledExactlyOnceWith('NVDA');
      // The place left it: the completion opens nothing there.
      expect(admission.state()).toMatchObject({ kind: 'ready', row: { symbol: 'SPY' } });
    });

    it('stops the fill when it was the last caller waiting', async () => {
      const { symbol, cancelled } = configure('NVDA');
      await settle();

      symbol.set('SPY');
      await settle();

      expect(cancelled).toEqual(['job-1']);
    });
  });

  describe('when the place moves on while a fill runs', () => {
    it('lets a completion for the old instrument open nothing, and opens the new one when it is confirmed', async () => {
      const { admission, symbol, started, cancelled, complete } = configure('NVDA');
      await settle();

      symbol.set('AMD');
      await settle();
      expect(admission.state()).toMatchObject({ kind: 'filling', gate: { symbol: 'AMD' } });
      expect(started).toHaveLength(2);
      expect(cancelled).toEqual(['job-1']);

      // NVDA's fill reports done after the place is on AMD.
      await complete('job-1', ['NVDA']);
      expect(admission.state()).toMatchObject({ kind: 'filling', gate: { symbol: 'AMD' } });

      await complete('job-2', ['AMD']);
      expect(admission.state()).toMatchObject({ kind: 'ready', row: { symbol: 'AMD' } });
    });

    it('asks for the fill again when the place comes back to an instrument whose fill it had let go', async () => {
      const { admission, symbol, started } = configure('NVDA');
      await settle();
      symbol.set('AMD');
      await settle();

      symbol.set('NVDA');
      await settle();

      expect(admission.state()).toMatchObject({ kind: 'filling', gate: { symbol: 'NVDA' } });
      expect(started).toHaveLength(3);
    });
  });
});
