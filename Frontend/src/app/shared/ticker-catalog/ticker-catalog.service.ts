import { Injectable, computed, inject, resource, type Signal } from '@angular/core';

import { etIsoDate } from '../date/et-midnight';
import { DataLakeService } from '../data-lake/data-lake.service';
import type { StorageSummaryResponse, SymbolCoverageSpan } from '../data-lake/data-lake.types';
import type { TickerOption } from '../ticker-range-picker';
import { SEED_RECENT_TICKERS, TICKER_LABELS } from './ticker-labels';

/**
 * The instruments a run can actually be configured against.
 *
 * The lake is the sole market-data store (#1893 retired the policy store), so
 * "what can I backtest" has exactly one honest answer: what the lake holds.
 * This service asks it — `/api/data-lake/storage-summary` already reports every
 * catalogued symbol with its coverage span — and hands the pickers a pool that
 * cannot drift from reality the way the hardcoded list it replaces had.
 *
 * On a failed read the pool is empty and `unavailable()` carries the reason.
 * That is deliberate: falling back to a canned list would offer instruments we
 * cannot prove we hold bars for, which is the exact failure being fixed. A
 * silent wrong list is worse than a visible outage.
 */
@Injectable({ providedIn: 'root' })
export class TickerCatalogService {
  private readonly lake = inject(DataLakeService);

  private readonly summary = resource({
    loader: () => this.lake.storageSummary(),
  });

  /** Instruments the lake holds bars for, deepest history first. */
  readonly pool: Signal<readonly TickerOption[]> = computed(() => {
    const read = this.summary.value();
    return read?.kind === 'ok' ? toPool(read.value) : [];
  });

  /** Seed recents, less any symbol the lake turns out not to hold. */
  readonly recent: Signal<readonly string[]> = computed(() => {
    const held = new Set(this.pool().map((t) => t.symbol));
    return SEED_RECENT_TICKERS.filter((symbol) => held.has(symbol));
  });

  readonly loading: Signal<boolean> = computed(() => this.summary.isLoading());

  /** Why the catalog is empty, or `null` when the lake answered. */
  readonly unavailable: Signal<string | null> = computed(() => {
    const read = this.summary.value();
    if (read === undefined || read.kind === 'ok') return null;
    return read.message;
  });

  reload(): void {
    this.summary.reload();
  }
}

/**
 * Coverage spans → picker options.
 *
 * Ordered by artifact count descending, which surfaces the deeply-backfilled
 * instruments (SPY, DIA, GLD) above ones holding a handful of days — the
 * closest thing the catalog reports to "most useful for research". Symbol
 * order breaks ties so the list is stable across reads.
 */
function toPool(summary: StorageSummaryResponse): readonly TickerOption[] {
  return summary.symbols
    .filter(isRunnable)
    .slice()
    .sort((a, b) => b.artifact_count - a.artifact_count || a.symbol.localeCompare(b.symbol))
    .map(toOption);
}

/**
 * A catalogued symbol with no coverage span, or no artifacts, holds nothing a
 * run could read — the row exists but the bars do not.
 */
function isRunnable(span: SymbolCoverageSpan): boolean {
  return span.artifact_count > 0 && span.last_trading_date_ms !== null;
}

function toOption(span: SymbolCoverageSpan): TickerOption {
  const label = TICKER_LABELS[span.symbol];
  return {
    symbol: span.symbol,
    // An unlabelled symbol is shown by its own ticker rather than blank: the
    // lake decides membership, so a new backfill must be pickable before
    // anyone gets around to writing its display name.
    name: label?.name ?? span.symbol,
    exchange: label?.exchange,
    // Coverage bounds are ET-anchored trading dates on the wire
    // (temporal-rigor.md); rendering them in the viewer's zone would drift a
    // day west of UTC, so both go through the ET resolver.
    first: span.first_trading_date_ms === null ? null : etIsoDate(span.first_trading_date_ms),
    last: span.last_trading_date_ms === null ? null : etIsoDate(span.last_trading_date_ms),
  };
}
