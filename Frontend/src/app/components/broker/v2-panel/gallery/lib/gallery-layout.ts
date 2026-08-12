/**
 * Pure gallery tile layout model: near-square auto-grid division, page
 * slicing, and per-account persistence (tile order + resize spans) to
 * `localStorage`. No Angular imports — `BotGalleryDockComponent` consumes
 * this, but the math and the storage guard are unit-tested directly here,
 * without mounting a component.
 */

/** One tile's CSS-grid span, persisted per account. Array order *is* display order. */
export interface TileLayout {
  readonly sid: string;
  readonly colSpan: number;
  readonly rowSpan: number;
}

/** Default page size for `paginate` and the dock's page-relative reorder/resize index math. */
export const GALLERY_PAGE_SIZE = 20;

const STORAGE_PREFIX = 'gallery-layout:';

/** Near-square auto grid: `cols = ceil(sqrt(n))`, `rows = ceil(n / cols)`. An empty gallery is a single 1x1 cell. */
export function autoDivision(n: number): { cols: number; rows: number } {
  if (n <= 0) return { cols: 1, rows: 1 };
  const cols = Math.ceil(Math.sqrt(n));
  const rows = Math.ceil(n / cols);
  return { cols, rows };
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

function storageKey(accountId: string): string {
  return `${STORAGE_PREFIX}${accountId}`;
}

function isTileLayout(value: unknown): value is TileLayout {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<TileLayout>;
  return typeof candidate.sid === 'string'
    && typeof candidate.colSpan === 'number'
    && typeof candidate.rowSpan === 'number';
}

/** Persisted layout for `accountId`, or `[]` if none was ever saved, the entry is corrupt, or storage is unavailable. */
export function loadLayout(accountId: string): TileLayout[] {
  try {
    const raw = localStorage.getItem(storageKey(accountId));
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.every(isTileLayout) ? parsed : [];
  } catch (error) {
    // localStorage.getItem/JSON.parse can throw — storage disabled, over
    // quota, private-browsing restrictions, or a hand-corrupted entry.
    // None of those are recoverable here, and the caller's fallback (the
    // auto grid) is the correct behavior either way, so an explicit `[]`
    // return is the deliberate handling — not a silently swallowed error.
    void error;
    return [];
  }
}

/** Persist `layout` for `accountId`. Best-effort: a storage failure just means the layout won't survive a reload. */
export function saveLayout(accountId: string, layout: readonly TileLayout[]): void {
  try {
    localStorage.setItem(storageKey(accountId), JSON.stringify(layout));
  } catch (error) {
    // Same reasoning as loadLayout: storage can be unavailable/full. The
    // gallery still works for the rest of the session; only cross-reload
    // persistence is lost, so this is a deliberate no-op.
    void error;
  }
}

/** Clear the persisted layout for `accountId`, reverting to the auto grid on next load. */
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
