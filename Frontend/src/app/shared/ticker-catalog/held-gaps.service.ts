import { Injectable, Injector, inject, linkedSignal, resource, signal, untracked, type ResourceRef, type Signal, type WritableSignal } from '@angular/core';

import { DataLakeService, MAX_TRADING_RANGE_DAYS, type PriceAdjustmentMode } from '../data-lake';
import type { DataGaps, DateBounds } from '../date-picker';
import { addDays, type IsoDate } from '../date/iso-date';

/** The spans whose gaps are kept, and read again with the lake's holdings. A page shows one or two. */
const KEPT_READS = 4;

/** One span's read, and its last answer, which stays on screen while a newer one is fetched. */
interface GapRead {
  readonly read: ResourceRef<DataGaps | undefined>;
  readonly answer: Signal<DataGaps | undefined>;
}

/**
 * The sessions a lake tree lacks inside a symbol's held span.
 *
 * The catalog's first and last held day are extrema, not coverage: a session
 * between them may have no bars. The coverage endpoint knows which, one row
 * per scheduled session, so this asks it once per symbol, tree and span and
 * keeps the answer for every picker on the page.
 *
 * The endpoint reports the minute bars. A daily run reads the symbol's daily
 * history instead, which this does not check, so a daily configuration is
 * told nothing rather than something that may not be true of its run.
 */
@Injectable({ providedIn: 'root' })
export class HeldGapsService {
  private readonly lake = inject(DataLakeService);
  private readonly injector = inject(Injector);
  private readonly reads = new Map<string, GapRead>();
  /** How many times each tree's holdings have been read again; every read of that tree's gaps follows it. */
  private readonly rereads = new Map<PriceAdjustmentMode, WritableSignal<number>>();

  /**
   * The gaps inside ``bounds`` for ``symbol`` in ``mode``'s tree; null while
   * they are being read, when the span has no first or last day to read
   * between, and for a run that does not read minute bars. Safe to call from
   * a ``computed``: the read behind a span is created once and shared.
   */
  gapsFor(mode: PriceAdjustmentMode, symbol: string, bounds: DateBounds, resolution: 'minute' | 'daily'): DataGaps | null {
    const { min, max } = bounds;
    if (resolution !== 'minute' || min === null || max === null) return null;
    const key = `${mode}|${symbol}|${min}|${max}`;
    let held = this.reads.get(key);
    if (held === undefined) {
      const rereads = this.rereadsOf(mode);
      // `resource()` installs an effect, which a reactive context forbids.
      held = untracked(() => {
        // The count is the read's parameter, so a re-read asked for while one is in flight starts it over instead of being dropped.
        const read = resource({ params: () => rereads(), loader: () => this.load(mode, symbol, min, max), injector: this.injector });
        const answer = linkedSignal<DataGaps | undefined, DataGaps | undefined>({ source: read.value, computation: (now, before) => now ?? before?.value });
        return { read, answer };
      });
      this.reads.set(key, held);
      this.letGoOfTheOldest();
    }
    return held.answer() ?? null;
  }

  /** Keeps the newest spans only, so the tab neither re-reads nor holds on to every symbol it has shown. */
  private letGoOfTheOldest(): void {
    if (this.reads.size <= KEPT_READS) return;
    const [oldest, held] = this.reads.entries().next().value as [string, GapRead];
    this.reads.delete(oldest);
    // Destroying a resource writes its state, which a reactive context forbids.
    untracked(() => held.read.destroy());
  }

  /**
   * Reads ``mode``'s gaps again, because its holdings were just read again:
   * a backfill may have filled a gap without moving the span's first or last
   * day. The answer on screen stays until the new one arrives, and a read
   * that failed is tried again.
   */
  reload(mode: PriceAdjustmentMode): void {
    this.rereadsOf(mode).update((count) => count + 1);
  }

  private rereadsOf(mode: PriceAdjustmentMode): WritableSignal<number> {
    let rereads = this.rereads.get(mode);
    if (rereads === undefined) {
      rereads = signal(0);
      this.rereads.set(mode, rereads);
    }
    return rereads;
  }

  /** Reads the span in windows the coverage endpoint accepts; a session is held when its artifact is complete. */
  private async load(mode: PriceAdjustmentMode, symbol: string, from: IsoDate, to: IsoDate): Promise<DataGaps> {
    const unheldMs = new Set<number>();
    for (let start = from; start <= to; start = addDays(start, MAX_TRADING_RANGE_DAYS)) {
      const last = addDays(start, MAX_TRADING_RANGE_DAYS - 1);
      const read = await this.lake.coverage({ symbol, startTradingDate: start, endTradingDate: last < to ? last : to, priceAdjustmentMode: mode });
      if (read.kind !== 'ok') return { unheldMs: new Set(), problem: read.message };
      for (const day of read.value.days) {
        if (day.status !== 'complete') unheldMs.add(day.trading_date_ms);
      }
    }
    return { unheldMs, problem: null };
  }
}
