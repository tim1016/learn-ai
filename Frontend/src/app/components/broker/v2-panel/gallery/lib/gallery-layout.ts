/**
 * Pure gallery tile layout model: aspect-ratio-optimised uniform column
 * choice, page slicing, and per-account order-only persistence to
 * `localStorage`. No Angular imports — `BotGalleryDockComponent` consumes
 * this, but the math and the storage guard are unit-tested directly here,
 * without mounting a component.
 */

/** Persisted per-account tile order: bot sids in display order. Array order *is* display order. */
export type TileLayout = readonly string[];

/** Default page size for `paginate` and the dock's page-relative reorder index math. */
export const GALLERY_PAGE_SIZE = 20;

/** `chooseColumns` never considers more columns than this, even for a large roster (the mock's cap). */
const MAX_COLS = 6;
/** Target chart aspect ratio (`tileWidth / (tileHeight - headerHeight)`) the scorer optimises toward. */
const TARGET_CHART_AR = 2.2;
/** Chart-AR band tolerated before the out-of-band penalty applies. */
const MIN_CHART_AR = 1.9;
const MAX_CHART_AR = 3.1;
/** Score penalty added when a candidate's chart AR falls outside `[MIN_CHART_AR, MAX_CHART_AR]`. */
const OUT_OF_BAND_PENALTY = 0.9;
/** Score penalty per empty trailing grid cell (`rows*cols - n`) — a light nudge away from wasteful divisions. */
const EMPTY_CELL_PENALTY = 0.02;
/** A tile whose height can't clear `headerHeight + this` can't usefully show a chart under the header; the candidate is rejected outright. */
const MIN_CHART_HEIGHT_PX = 70;

/**
 * Choose the column/row division for `n` tiles inside a `gridWidth` x
 * `gridHeight` px area so each tile's *chart* body (tile height minus the
 * fixed `headerHeight`) lands as close as possible to `TARGET_CHART_AR`.
 *
 * Scans `cols` from 1 to `min(n, MAX_COLS)`. For each, derives `rows =
 * ceil(n / cols)` and the resulting tile width/height (accounting for
 * `gap`-px gutters between cells), rejects candidates whose tile height
 * can't fit a usable chart under the header, and scores the rest on a log
 * scale (so 2x-too-wide and 2x-too-narrow score the same), penalising
 * out-of-band aspect ratios and wasted trailing cells. If every candidate is
 * rejected (a degenerate viewport), falls back to whichever column count
 * produced the tallest tile rather than choosing nothing.
 */
export function chooseColumns(
  n: number,
  gridWidth: number,
  gridHeight: number,
  gap: number,
  headerHeight: number,
): { cols: number; rows: number } {
  if (n <= 0) return { cols: 1, rows: 1 };

  const maxCols = Math.min(n, MAX_COLS);
  const candidates = Array.from({ length: maxCols }, (_unused, index) => {
    const cols = index + 1;
    const rows = Math.ceil(n / cols);
    const tileWidth = (gridWidth - gap * (cols - 1)) / cols;
    const tileHeight = (gridHeight - gap * (rows - 1)) / rows;
    return { cols, rows, tileWidth, tileHeight };
  });

  const usable = candidates.filter((c) => c.tileHeight >= headerHeight + MIN_CHART_HEIGHT_PX);
  if (usable.length === 0) {
    const tallest = candidates.reduce((a, b) => (b.tileHeight > a.tileHeight ? b : a));
    return { cols: tallest.cols, rows: tallest.rows };
  }

  const scored = usable.map((c) => {
    const chartAr = c.tileWidth / (c.tileHeight - headerHeight);
    let score = Math.abs(Math.log(chartAr / TARGET_CHART_AR));
    if (chartAr < MIN_CHART_AR || chartAr > MAX_CHART_AR) score += OUT_OF_BAND_PENALTY;
    score += (c.rows * c.cols - n) * EMPTY_CELL_PENALTY;
    return { cols: c.cols, rows: c.rows, score };
  });

  const best = scored.reduce((a, b) => (b.score < a.score ? b : a));
  return { cols: best.cols, rows: best.rows };
}

/** Slice `items` into fixed-size pages, clamping `page` into `[0, pages)`. */
export function paginate<T>(
  items: readonly T[],
  page: number,
  size: number = GALLERY_PAGE_SIZE,
): { pageItems: T[]; pages: number } {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const clampedPage = Math.min(Math.max(page, 0), pages - 1);
  const start = clampedPage * size;
  return { pageItems: items.slice(start, start + size), pages };
}

const STORAGE_PREFIX = 'gallery-layout:';

function storageKey(accountId: string): string {
  return `${STORAGE_PREFIX}${accountId}`;
}

function isTileLayout(value: unknown): value is TileLayout {
  return Array.isArray(value) && value.every((entry) => typeof entry === 'string');
}

/** Persisted layout for `accountId`, or `[]` if none was ever saved, the entry is corrupt, or storage is unavailable. */
export function loadLayout(accountId: string): TileLayout {
  try {
    const raw = localStorage.getItem(storageKey(accountId));
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    return isTileLayout(parsed) ? parsed : [];
  } catch (error) {
    // localStorage.getItem/JSON.parse can throw — storage disabled, over
    // quota, private-browsing restrictions, or a hand-corrupted entry.
    // None of those are recoverable here, and the caller's fallback (the
    // auto layout) is the correct behavior either way, so an explicit `[]`
    // return is the deliberate handling — not a silently swallowed error.
    void error;
    return [];
  }
}

/** Persist `layout` for `accountId`. Best-effort: a storage failure just means the layout won't survive a reload. */
export function saveLayout(accountId: string, layout: TileLayout): void {
  try {
    localStorage.setItem(storageKey(accountId), JSON.stringify(layout));
  } catch (error) {
    // Same reasoning as loadLayout: storage can be unavailable/full. The
    // gallery still works for the rest of the session; only cross-reload
    // persistence is lost, so this is a deliberate no-op.
    void error;
  }
}

/** Clear the persisted layout for `accountId`, reverting to the auto layout on next load. */
export function resetLayout(accountId: string): void {
  try {
    localStorage.removeItem(storageKey(accountId));
  } catch (error) {
    // Same reasoning as loadLayout/saveLayout: storage can be unavailable.
    // Leaving a stale entry behind isn't a correctness problem here (the
    // caller resets its own in-memory layout state regardless of whether
    // the persisted copy was actually cleared), so this is a deliberate
    // no-op, not a silently swallowed error.
    void error;
  }
}
