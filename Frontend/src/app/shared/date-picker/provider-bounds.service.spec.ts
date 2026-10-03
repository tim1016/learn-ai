import { ApplicationRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { DataLakeService, tradingDateToMs, type BackfillDefaults, type DataLakeRead } from '../data-lake';
import { ProviderBoundsService } from './provider-bounds.service';
import { fakeSessions, fakeTradingCalendar } from './testing/fake-trading-calendar';
import { TradingCalendarService } from './trading-calendar.service';

function defaults(floor: string): BackfillDefaults {
  return { market: 'usa', lean_image_digest: null, max_trading_range_days: 1830, max_symbol_length: 10, provider_history_start_ms: tradingDateToMs(floor) ?? 0 };
}

async function boundsAfter(read: DataLakeRead<BackfillDefaults>) {
  let answer: (read: DataLakeRead<BackfillDefaults>) => void = () => undefined;
  const pending = new Promise<DataLakeRead<BackfillDefaults>>((resolve) => (answer = resolve));
  TestBed.configureTestingModule({
    providers: [
      { provide: DataLakeService, useValue: { backfillDefaults: () => pending } },
      // Friday 2026-10-02 at noon: Thursday is the last completed session.
      { provide: TradingCalendarService, useValue: fakeTradingCalendar(fakeSessions('2026-01-01', '2026-12-31'), '2026-10-02') },
    ],
  });
  const service = TestBed.inject(ProviderBoundsService);
  const before = service.bounds();
  TestBed.tick();
  answer(read);
  await TestBed.inject(ApplicationRef).whenStable();
  return { before, after: service.bounds() };
}

describe('ProviderBoundsService', () => {
  it("runs from the provider's oldest day to the last completed session", async () => {
    const { before, after } = await boundsAfter({ kind: 'ok', value: defaults('2021-10-04') });

    // No range can be offered before the floor is known; typed dates stay unchecked.
    expect(before).toMatchObject({ min: null, max: null, unknown: "The provider's history is being read." });
    expect(after).toEqual({
      min: '2021-10-04',
      max: '2026-10-01',
      minReason: 'The provider serves bars from 2021-10-04.',
      maxReason: 'The last completed session is 2026-10-01.',
      unknown: null,
    });
  });

  it('offers no bound it could not read, and says why', async () => {
    const { after } = await boundsAfter({ kind: 'unavailable', message: 'The data plane did not respond.' });

    expect(after).toMatchObject({ min: null, max: null, unknown: "The provider's history could not be read: The data plane did not respond." });
  });
});
