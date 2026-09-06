import { Injectable, computed, inject, resource, signal, type Signal } from '@angular/core';

import { etIsoDate } from '../date/et-midnight';
import { DataLakeService } from '../data-lake/data-lake.service';
import type {
  PriceAdjustmentMode,
  StorageSummaryResponse,
  SymbolCoverageSpan,
} from '../data-lake/data-lake.types';
import type { TickerOption } from '../ticker-range-picker';
import type { TickerCatalog } from './ticker-catalog';
import { SEED_RECENT_TICKERS, TICKER_LABELS } from './ticker-labels';

/**
 * The tree a run reads unless it says otherwise.
 *
 * A backtest resolves its roots with `adjusted=true` by default
 * (`engine.py::_policy_adjusted`), which is the `polygon_split_adjusted`
 * segment of the lake root (#1866). It is only a default: Strategy Lab sends
 * `adjusted: false` when the engine is `both`, and the picker on that page
 * says so through `useMode`.
 */
const DEFAULT_ADJUSTMENT_MODE: PriceAdjustmentMode = 'polygon_split_adjusted';

/**
 * The instruments a run can actually be configured against.
 *
 * The lake is the sole market-data store (#1893 retired the policy store), so
 * "what can I backtest" has exactly one honest answer: what the lake holds.
 * This service asks it — `/api/data-lake/storage-summary` reports every
 * catalogued symbol with the first and last day it holds — and hands the
 * pickers a pool that cannot drift from reality the way the hardcoded list it
 * replaces had.
 *
 * The pool is per adjustment mode, because coverage is: the mode is a segment
 * of the lake root, so a symbol backfilled only in `raw` has no bars at all
 * for a split-adjusted reader. `useMode` follows the run's data policy so the
 * picker does not offer what that run would refuse. One picker is mounted at a
 * time, so a single active mode suffices; a second picker asking for another
 * mode repoints this one, which is why `useMode` is a request from the page
 * that owns the data policy rather than a per-component input.
 *
 * On a failed read the pool is empty and `unavailable()` carries the reason.
 * That is deliberate: falling back to a canned list would offer instruments we
 * cannot prove we hold bars for, which is the exact failure being fixed. A
 * silent wrong list is worse than a visible outage.
 */
@Injectable({ providedIn: 'root' })
export class TickerCatalogService implements TickerCatalog {
  private readonly lake = inject(DataLakeService);
  private readonly requestedMode = signal<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);

  private readonly summary = resource({
    params: () => this.requestedMode(),
    loader: ({ params }) => this.lake.storageSummary('usa', params),
  });

  readonly mode: Signal<PriceAdjustmentMode> = this.requestedMode.asReadonly();

  readonly pool: Signal<readonly TickerOption[]> = computed(() => {
    const read = this.summary.value();
    return read?.kind === 'ok' ? toPool(read.value) : [];
  });

  readonly recent: Signal<readonly string[]> = computed(() => {
    const held = new Set(this.pool().map((t) => t.symbol));
    return SEED_RECENT_TICKERS.filter((symbol) => held.has(symbol));
  });

  readonly loading: Signal<boolean> = computed(() => this.summary.isLoading());

  readonly unavailable: Signal<string | null> = computed(() => {
    // A read in flight has no verdict yet. Without this the previous failure
    // outranks the loading state, so a retry shows the operator nothing until
    // it resolves and the button looks dead.
    if (this.summary.isLoading()) return null;
    const read = this.summary.value();
    if (read === undefined || read.kind === 'ok') return null;
    return read.message;
  });

  useMode(mode: PriceAdjustmentMode): void {
    this.requestedMode.set(mode);
  }

  reload(): void {
    this.summary.reload();
  }
}

/**
 * Coverage spans → picker options.
 *
 * Ordered by artifact count descending, which surfaces the deeply-backfilled
 * instruments above ones holding a handful of days. That count is a row count,
 * not a day count — a symbol carries a row per data type, so SPY's 1147 rows
 * span 576 days — which is why it ranks the list and is never rendered as a
 * number of days. Symbol order breaks ties so the list is stable across reads.
 */
function toPool(summary: StorageSummaryResponse): readonly TickerOption[] {
  return summary.symbols
    .filter(isRunnable)
    .slice()
    .sort((a, b) => b.artifact_count - a.artifact_count || a.symbol.localeCompare(b.symbol))
    .map(toOption);
}

/**
 * A catalogued symbol with no span, or no artifacts, holds nothing a run could
 * read — the row exists but the bars do not.
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
    // The first and last day held — a span, and emphatically not a claim that
    // every session between them is present. `SymbolCoverageSpan` is MIN/MAX
    // over trading dates and says nothing about the days in between: NVDA
    // spans 2024-06 to 2026-08 holding 69 scattered days, none of them in
    // 2025. Named `firstHeld`/`lastHeld` so no consumer reads density into it.
    //
    // Both are ET-anchored trading dates on the wire (temporal-rigor.md);
    // rendering them in the viewer's zone would drift a day west of UTC, so
    // they go through the ET resolver.
    firstHeld: span.first_trading_date_ms === null ? null : etIsoDate(span.first_trading_date_ms),
    lastHeld: span.last_trading_date_ms === null ? null : etIsoDate(span.last_trading_date_ms),
  };
}
