import { ApplicationRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { DataLakeService, type CoverageQuery, type CoverageResponse, type CoverageStatus, type DataLakeRead } from '../data-lake';
import { heldBounds, type DataGaps } from '../date-picker';
import { etMidnightMs } from '../date/et-midnight';
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
      // Session open, 09:30 ET.
      days: Object.entries(days).map(([day, status]) => ({ trading_date_ms: etMidnightMs(day) + 9.5 * 60 * 60 * 1000, status, artifact_id: null })),
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
  const before = service.gapsFor('polygon_split_adjusted', 'QQQ', bounds);
  TestBed.tick();
  await TestBed.inject(ApplicationRef).whenStable();
  const after: DataGaps | null = service.gapsFor('polygon_split_adjusted', 'QQQ', bounds);
  return { before, after, asked };
}

describe('HeldGapsService', () => {
  it('names the sessions inside the held span whose bars are not complete', async () => {
    const { before, after, asked } = await gapsFor(
      () => coverage({ '2026-09-14': 'complete', '2026-09-15': 'missing', '2026-09-16': 'failed', '2026-09-17': 'fetching', '2026-09-18': 'complete' }),
      '2026-09-14',
      '2026-09-18',
    );

    expect(before).toBeNull();
    expect(after).toEqual({ unheld: new Set(['2026-09-15', '2026-09-16', '2026-09-17']), problem: null });
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

    expect(after).toEqual({ unheld: new Set(), problem: 'The data plane did not respond.' });
  });

  it('asks nothing while the span has no first or last day', () => {
    const read = vi.fn();
    TestBed.configureTestingModule({ providers: [{ provide: DataLakeService, useValue: { coverage: read } }] });

    expect(TestBed.inject(HeldGapsService).gapsFor('raw', 'QQQ', heldBounds('QQQ', null, null))).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });
});
