import { Injectable, Injector, computed, inject, resource, type Signal } from '@angular/core';

import { etIsoDate } from '../date/et-midnight';
import { DataLakeService } from '../data-lake';
import type {
  DataLakeDataType,
  PriceAdjustmentMode,
  StorageSummaryResponse,
  SymbolCoverageSpan,
} from '../data-lake';
import type { TickerOption } from '../ticker-range-picker';
import type { TickerCatalog, TickerCatalogView } from './ticker-catalog';
import { SEED_RECENT_TICKERS, TICKER_LABELS } from './ticker-labels';

/**
 * The tree a run reads unless its page says otherwise. A backtest resolves its
 * roots with `adjusted=true` by default (`engine.py::_policy_adjusted`), which
 * is the `polygon_split_adjusted` segment of the lake root (#1866).
 */
export const DEFAULT_ADJUSTMENT_MODE: PriceAdjustmentMode = 'polygon_split_adjusted';

/**
 * A backtest consumes trade bars. A symbol whose quote side completed while
 * its trade backfill failed still has complete catalog rows, so an unfiltered
 * pool would offer it and the run would find nothing to read.
 */
const RUNNABLE_DATA_TYPE: DataLakeDataType = 'trade';

/**
 * The instruments a run can actually be configured against.
 *
 * The lake is the sole market-data store (#1893 retired the policy store), so
 * "what can I backtest" has exactly one honest answer: what the lake holds.
 * This service asks `/api/data-lake/storage-summary` and hands the pickers a
 * pool that cannot drift from reality the way the hardcoded list it replaces
 * had.
 *
 * **The catalog is sliced by adjustment mode, and the mode is the caller's.**
 * Coverage is per mode — the mode is a segment of the lake root, so a symbol
 * backfilled only in `raw` has no bars at all for a split-adjusted reader.
 * Each mode gets its own resource, cached here so several pickers asking for
 * the same tree share one read. Mode deliberately is *not* service state: an
 * earlier revision kept a single `requestedMode` that pages set, and because
 * this service is `providedIn: 'root'` that choice outlived the page's own
 * teardown — a Strategy Lab `both` run left every later picker reading raw.
 *
 * On a failed read the pool is empty and `unavailable()` carries the reason.
 * That is deliberate: falling back to a canned list would offer instruments we
 * cannot prove we hold bars for, which is the exact failure being fixed. A
 * silent wrong list is worse than a visible outage.
 *
 * Pickers whose subject is **not** lake bars — Ticker Explorer's live options
 * snapshot, say — must not use this at all; they pass their own universe to
 * the picker instead.
 */
@Injectable({ providedIn: 'root' })
export class TickerCatalogService implements TickerCatalog {
  private readonly lake = inject(DataLakeService);
  private readonly injector = inject(Injector);
  private readonly views = new Map<PriceAdjustmentMode, TickerCatalogView>();

  viewFor(mode: PriceAdjustmentMode): TickerCatalogView {
    const existing = this.views.get(mode);
    if (existing) return existing;

    // `injector` lets the resource be created lazily, outside the constructor's
    // injection context, and ties its lifetime to this root-scoped service.
    const summary = resource({
      loader: () => this.lake.storageSummary('usa', mode, RUNNABLE_DATA_TYPE),
      injector: this.injector,
    });

    const pool: Signal<readonly TickerOption[]> = computed(() => {
      const read = summary.value();
      return read?.kind === 'ok' ? toPool(read.value) : [];
    });

    const view: TickerCatalogView = {
      pool,
      recent: computed(() => {
        const held = new Set(pool().map((t) => t.symbol));
        return SEED_RECENT_TICKERS.filter((symbol) => held.has(symbol));
      }),
      loading: computed(() => summary.isLoading()),
      unavailable: computed(() => {
        // A read in flight has no verdict yet. Without this the previous
        // failure outranks the loading state, so a retry shows the operator
        // nothing until it resolves and the button looks dead.
        if (summary.isLoading()) return null;
        const read = summary.value();
        if (read === undefined || read.kind === 'ok') return null;
        return read.message;
      }),
      reload: () => summary.reload(),
    };
    this.views.set(mode, view);
    return view;
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
 * read — the row exists but the bars do not. The endpoint is asked for
 * trade-bar spans specifically, so a symbol whose quote side backfilled and
 * whose trade side failed does not reach here at all.
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
