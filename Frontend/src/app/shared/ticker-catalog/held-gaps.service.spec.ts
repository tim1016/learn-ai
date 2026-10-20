import { ApplicationRef, computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { DataLakeService, type CoverageQuery, type CoverageResponse, type CoverageStatus, type DataLakeRead } from '../data-lake';
import { heldBounds, type DataGaps } from '../date-picker';
import { sessionOpenMs } from '../date-picker/testing/fake-trading-calendar';
import { HeldGapsService } from './held-gaps.service';

function coverage(days: Readonly<Record<string, CoverageStatus>>): DataLakeRead<CoverageResponse> {
  return {
    kind: 'ok',
    value: {
      market: 'usa',
      symbol: 'QQQ',
      data_type: 'trade',
      resolution: 'minute',
      provider: 'polygon',
      price_adjustment_mode: 'polygon_split_adjusted',
      days: Object.entries(days).map(([day, status]) => ({ trading_date_ms: sessionOpenMs(day), status, artifact_id: null })),
    },
  };
}

async function gapsFor(answer: (query: CoverageQuery) => DataLakeRead<CoverageResponse>, first: string, last: string) {
  const asked: CoverageQuery[] = [];
  const read = vi.fn(async (query: CoverageQuery) => {
    asked.push(query);
    return answer(query);
  });
  TestBed.configureTestingModule({ providers: [{ provide: DataLakeService, useValue: { coverage: read } }] });
  const service = TestBed.inject(HeldGapsService);
  const bounds = heldBounds('QQQ', first, last);
  const settled = async (): Promise<DataGaps | null> => {
    TestBed.tick();
    await TestBed.inject(ApplicationRef).whenStable();
    return service.gapsFor('polygon_split_adjusted', 'QQQ', bounds, 'minute');
  };
  const before = service.gapsFor('polygon_split_adjusted', 'QQQ', bounds, 'minute');
  const after = await settled();
  return { before, after, asked, service, settled };
}

describe('HeldGapsService', () => {
  it('names the sessions inside the held span whose bars are not complete', async () => {
    const { before, after, asked } = await gapsFor(
      () => coverage({ '2026-09-14': 'complete', '2026-09-15': 'missing', '2026-09-16': 'failed', '2026-09-17': 'fetching', '2026-09-18': 'complete' }),
      '2026-09-14',
      '2026-09-18',
    );

    expect(before).toBeNull();
    // Kept as the instants the route gave, not as date strings: the picker resolves them where it draws them.
    expect(after).toEqual({ unheldMs: new Set(['2026-09-15', '2026-09-16', '2026-09-17'].map(sessionOpenMs)), problem: null });
    expect(asked).toEqual([{ symbol: 'QQQ', startTradingDate: '2026-09-14', endTradingDate: '2026-09-18', priceAdjustmentMode: 'polygon_split_adjusted' }]);
  });

  it('reads a span longer than the endpoint takes in windows it does take, once per span', async () => {
    // 2021-01-04 to 2026-09-18 is more than 5 * 366 days.
    const { asked } = await gapsFor(() => coverage({}), '2021-01-04', '2026-09-18');

    expect(asked.map((query) => [query.startTradingDate, query.endTradingDate])).toEqual([
      ['2021-01-04', '2026-01-07'],
      ['2026-01-08', '2026-09-18'],
    ]);
  });

  it('says why when the gaps could not be read, instead of claiming there are none', async () => {
    const { after } = await gapsFor(() => ({ kind: 'unavailable', message: 'The data plane did not respond.' }), '2026-09-14', '2026-09-18');

    expect(after).toEqual({ unheldMs: new Set(), problem: 'The data plane did not respond.' });
  });

  it.each<[string, string | null, string | null, 'minute' | 'daily']>([
    ['asks nothing while the span has no first or last day', null, null, 'minute'],
    ['tells a daily run nothing: the endpoint reports minute bars, and a daily run reads the daily history', '2026-09-14', '2026-09-18', 'daily'],
  ])('%s', (_name, first, last, resolution) => {
    const read = vi.fn();
    TestBed.configureTestingModule({ providers: [{ provide: DataLakeService, useValue: { coverage: read } }] });

    expect(TestBed.inject(HeldGapsService).gapsFor('raw', 'QQQ', heldBounds('QQQ', first, last), resolution)).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });

  it('keeps the four newest spans: an older one is let go and read afresh when asked for again', async () => {
    const read = vi.fn(async () => coverage({}));
    TestBed.configureTestingModule({ providers: [{ provide: DataLakeService, useValue: { coverage: read } }] });
    const service = TestBed.inject(HeldGapsService);
    const span = heldBounds('X', '2026-09-14', '2026-09-18');
    const symbol = signal('A');
    // Asked from a computed, as the hosts do.
    const gaps = computed(() => service.gapsFor('raw', symbol(), span, 'minute'));
    const settle = async (): Promise<void> => {
      gaps();
      TestBed.tick();
      await TestBed.inject(ApplicationRef).whenStable();
    };

    for (const next of ['A', 'B', 'C', 'D', 'E']) {
      symbol.set(next);
      await settle();
    }
    expect(read).toHaveBeenCalledTimes(5);

    // E is still kept; A was the oldest of five.
    symbol.set('E');
    await settle();
    expect(read).toHaveBeenCalledTimes(5);
    symbol.set('A');
    await settle();
    expect(read).toHaveBeenCalledTimes(6);
    expect(gaps()).toEqual({ unheldMs: new Set(), problem: null });
  });

  it('starts a read over when a re-read is asked for while it is in flight, and keeps the old answer on screen meanwhile', async () => {
    const answers: ((read: DataLakeRead<CoverageResponse>) => void)[] = [];
    const read = vi.fn(() => new Promise<DataLakeRead<CoverageResponse>>((resolve) => answers.push(resolve)));
    TestBed.configureTestingModule({ providers: [{ provide: DataLakeService, useValue: { coverage: read } }] });
    const service = TestBed.inject(HeldGapsService);
    const span = heldBounds('QQQ', '2026-09-14', '2026-09-18');
    const settle = async (): Promise<DataGaps | null> => {
      TestBed.tick();
      await new Promise((resolve) => setTimeout(resolve, 0));
      TestBed.tick();
      return service.gapsFor('raw', 'QQQ', span, 'minute');
    };

    service.gapsFor('raw', 'QQQ', span, 'minute');
    await settle();
    // The holdings are read again before the first answer is back: it may predate a backfill.
    service.reload('raw');
    await settle();
    expect(read).toHaveBeenCalledTimes(2);

    answers[0](coverage({ '2026-09-15': 'missing' }));
    expect(await settle()).toBeNull();
    answers[1](coverage({ '2026-09-15': 'complete' }));
    expect(await settle()).toEqual({ unheldMs: new Set(), problem: null });

    // A later re-read keeps that answer on screen until its own arrives.
    service.reload('raw');
    expect(await settle()).toEqual({ unheldMs: new Set(), problem: null });
    answers[2](coverage({ '2026-09-16': 'failed' }));
    expect(await settle()).toEqual({ unheldMs: new Set([sessionOpenMs('2026-09-16')]), problem: null });
  });

  it("reads a tree's gaps again when its holdings were read again: a filled gap goes, and a failed read is retried", async () => {
    let answer: DataLakeRead<CoverageResponse> = { kind: 'unavailable', message: 'The data plane did not respond.' };
    const { after, asked, service, settled } = await gapsFor(() => answer, '2026-09-14', '2026-09-18');
    expect(after?.problem).toBe('The data plane did not respond.');

    answer = coverage({ '2026-09-15': 'missing' });
    service.reload('polygon_split_adjusted');
    expect(await settled()).toEqual({ unheldMs: new Set([sessionOpenMs('2026-09-15')]), problem: null });

    // A backfill fills the gap; the span's first and last day are the same as before.
    answer = coverage({ '2026-09-15': 'complete' });
    service.reload('raw');
    expect((await settled())?.unheldMs.size).toBe(1);
    service.reload('polygon_split_adjusted');
    expect(await settled()).toEqual({ unheldMs: new Set(), problem: null });
    expect(asked).toHaveLength(3);
  });
});
