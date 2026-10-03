import { Injectable, Injector, inject, resource, untracked, type ResourceRef } from '@angular/core';

import { DataLakeService, MAX_TRADING_RANGE_DAYS, type PriceAdjustmentMode } from '../data-lake';
import type { DataGaps, DateBounds } from '../date-picker';
import { etIsoDate } from '../date/et-midnight';
import { addDays, type IsoDate } from '../date/iso-date';

/**
 * The sessions a lake tree lacks inside a symbol's held span.
 *
 * The catalog's first and last held day are extrema, not coverage: a session
 * between them may have no bars. The coverage endpoint knows which, one row
 * per scheduled session, so this asks it once per symbol, tree and span and
 * keeps the answer for every picker on the page.
 */
@Injectable({ providedIn: 'root' })
export class HeldGapsService {
  private readonly lake = inject(DataLakeService);
  private readonly injector = inject(Injector);
  private readonly reads = new Map<string, ResourceRef<DataGaps | undefined>>();

  /**
   * The gaps inside ``bounds`` for ``symbol`` in ``mode``'s tree; null while
   * they are being read, and when the span has no first or last day to read
   * between. Safe to call from a ``computed``: the read behind a span is
   * created once and shared.
   */
  gapsFor(mode: PriceAdjustmentMode, symbol: string, bounds: DateBounds): DataGaps | null {
    const { min, max } = bounds;
    if (min === null || max === null) return null;
    const key = `${mode}|${symbol}|${min}|${max}`;
    let read = this.reads.get(key);
    if (read === undefined) {
      // `resource()` installs an effect, which a reactive context forbids.
      read = untracked(() => resource({ loader: () => this.load(mode, symbol, min, max), injector: this.injector }));
      this.reads.set(key, read);
    }
    return read.value() ?? null;
  }

  /** Reads the span in windows the coverage endpoint accepts; a session is held when its artifact is complete. */
  private async load(mode: PriceAdjustmentMode, symbol: string, from: IsoDate, to: IsoDate): Promise<DataGaps> {
    const unheld = new Set<IsoDate>();
    for (let start = from; start <= to; start = addDays(start, MAX_TRADING_RANGE_DAYS)) {
      const last = addDays(start, MAX_TRADING_RANGE_DAYS - 1);
      const read = await this.lake.coverage({ symbol, startTradingDate: start, endTradingDate: last < to ? last : to, priceAdjustmentMode: mode });
      if (read.kind !== 'ok') return { unheld: new Set(), problem: read.message };
      for (const day of read.value.days) {
        if (day.status !== 'complete') unheld.add(etIsoDate(day.trading_date_ms));
      }
    }
    return { unheld, problem: null };
  }
}
