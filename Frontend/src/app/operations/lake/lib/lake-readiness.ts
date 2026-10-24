import type {
  DataLakeDataType,
  PriceAdjustmentMode,
  SymbolRunReadiness,
} from '../../../shared/data-lake';
import { tradingRangeSpanDays } from '../../../shared/data-lake';
import { etIsoDate } from '../../../shared/date/et-midnight';

/** The views the page can show; the backfill panel refuses the one nothing derives. */
export const PRICE_ADJUSTMENT_OPTIONS: readonly PriceAdjustmentMode[] = [
  'polygon_split_adjusted',
  'raw',
  'lean_adjusted',
];

/** Only a split-adjusted file carries an adjustment record; raw prices have nothing to adjust. */
export function needsAdjustmentRecord(mode: PriceAdjustmentMode): boolean {
  return mode === 'polygon_split_adjusted';
}

/** One symbol's backfill window, as the backfill form holds dates: `YYYY-MM-DD`, inclusive. */
export interface RebuildRequest {
  readonly symbol: string;
  readonly from: string;
  readonly to: string;
  /** True when the sessions to rebuild are quote bars, which a backfill derives alongside trade. */
  readonly includeQuotes: boolean;
}

/**
 * Whether a run can read a symbol: every session (`ready`), some (`partial`)
 * or none (`unreadable`).
 */
export type SymbolReadinessState = 'ready' | 'partial' | 'unreadable';

export interface SymbolReadinessRow {
  readonly symbol: string;
  readonly state: SymbolReadinessState;
  readonly sessionCount: number;
  readonly runReadyCount: number;
  /** Unreadable sessions a backfill can rebuild. */
  readonly rebuildableCount: number;
  /** Unreadable sessions older than the provider's history: no backfill can fetch them again. */
  readonly unrebuildableCount: number;
  /** Date-anchored session opens, int64 ms UTC. Render in `date-et`. */
  readonly firstTradingDateMs: number;
  readonly lastTradingDateMs: number;
  /** The window a rebuild asks for, or null when no session can be rebuilt. */
  readonly rebuild: RebuildRequest | null;
  /** Sessions the calendar schedules between the first and last held one that the lake does not hold. */
  readonly gapSessionCount: number;
  /** Unbroken stretches of missing sessions: one long hole, or several. */
  readonly gapCount: number;
  /** Missing sessions older than the provider's history: no backfill can fetch them. */
  readonly unfillableGapCount: number;
  /** The window a backfill asks for to fill the gaps, or null when no missing session can be fetched. */
  readonly fill: RebuildRequest | null;
}

export interface LakeReadinessTotals {
  readonly symbolCount: number;
  readonly readyCount: number;
  /** Symbols with at least one session a backfill can rebuild. */
  readonly rebuildSymbolCount: number;
  readonly rebuildSessionCount: number;
  readonly unrebuildableSessionCount: number;
}

/** One symbol's backfill over `[from, to]`, both session opens; null when either is. */
function backfillWindow(
  symbol: string,
  from: number | null,
  to: number | null,
  bars: DataLakeDataType,
): RebuildRequest | null {
  if (from === null || to === null) return null;
  return { symbol, from: etIsoDate(from), to: etIsoDate(to), includeQuotes: bars === 'quote' };
}

function toRow(entry: SymbolRunReadiness, bars: DataLakeDataType): SymbolReadinessRow {
  const unreadable = entry.session_count - entry.run_ready_count;
  return {
    symbol: entry.symbol,
    state: unreadable === 0 ? 'ready' : entry.run_ready_count === 0 ? 'unreadable' : 'partial',
    sessionCount: entry.session_count,
    runReadyCount: entry.run_ready_count,
    rebuildableCount: unreadable - entry.unrebuildable_count,
    unrebuildableCount: entry.unrebuildable_count,
    firstTradingDateMs: entry.first_trading_date_ms,
    lastTradingDateMs: entry.last_trading_date_ms,
    rebuild: backfillWindow(
      entry.symbol,
      entry.rebuild_first_trading_date_ms,
      entry.rebuild_last_trading_date_ms,
      bars,
    ),
    gapSessionCount: entry.gap_session_count ?? 0,
    gapCount: entry.gap_count ?? 0,
    unfillableGapCount: entry.unfillable_gap_session_count ?? 0,
    fill: backfillWindow(
      entry.symbol,
      entry.fill_first_trading_date_ms ?? null,
      entry.fill_last_trading_date_ms ?? null,
      bars,
    ),
  };
}

/** What needs the operator comes first: a rebuild, then a gap to fill, then the rest that is not whole, then ready. */
function attention(row: SymbolReadinessRow): number {
  if (row.rebuildableCount > 0) return 0;
  if (row.fill !== null) return 1;
  return row.state === 'ready' && row.gapSessionCount === 0 ? 3 : 2;
}

/** `bars` is the bar type the readiness was read for; a rebuild has to write the same one. */
export function readinessRows(
  symbols: readonly SymbolRunReadiness[],
  bars: DataLakeDataType,
): SymbolReadinessRow[] {
  return symbols
    .map((entry) => toRow(entry, bars))
    .sort((a, b) => attention(a) - attention(b) || a.symbol.localeCompare(b.symbol));
}

export function readinessTotals(rows: readonly SymbolReadinessRow[]): LakeReadinessTotals {
  const rebuildable = rows.filter((row) => row.rebuildableCount > 0);
  return {
    symbolCount: rows.length,
    readyCount: rows.filter((row) => row.state === 'ready' && row.gapSessionCount === 0).length,
    rebuildSymbolCount: rebuildable.length,
    rebuildSessionCount: rebuildable.reduce((total, row) => total + row.rebuildableCount, 0),
    unrebuildableSessionCount: rows.reduce((total, row) => total + row.unrebuildableCount, 0),
  };
}

export function rebuildRequests(rows: readonly SymbolReadinessRow[]): RebuildRequest[] {
  return rows.flatMap((row) => (row.rebuild === null ? [] : [row.rebuild]));
}

/** A symbol with a gap a backfill can fill. */
export type FillRow = SymbolReadinessRow & { readonly fill: RebuildRequest };

/** The symbols with a gap a backfill can fill. */
export function fillRows(rows: readonly SymbolReadinessRow[]): FillRow[] {
  return rows.filter((row): row is FillRow => row.fill !== null);
}

const DAY_MS = 86_400_000;

/**
 * The window the session grid reads for a symbol: its whole held span, or the
 * newest `capDays` of it when the span is longer than the coverage endpoint
 * accepts.
 */
export function coverageWindow(
  row: SymbolReadinessRow,
  capDays: number,
): { readonly from: string; readonly to: string } {
  const from = etIsoDate(row.firstTradingDateMs);
  const to = etIsoDate(row.lastTradingDateMs);
  const span = tradingRangeSpanDays(from, to);
  if (span === null || span <= capDays) return { from, to };
  const [year, month, day] = to.split('-').map(Number);
  const start = new Date(Date.UTC(year, month - 1, day) - (capDays - 1) * DAY_MS);
  return { from: start.toISOString().slice(0, 10), to };
}
