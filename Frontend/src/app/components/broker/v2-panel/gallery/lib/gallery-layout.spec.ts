import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  GALLERY_TILE_HEADER_HEIGHT_PX,
  chooseColumns,
  loadLayout,
  paginate,
  resetLayout,
  saveLayout,
  spliceVisibleIntoFullOrder,
  type TileLayout,
} from './gallery-layout';

describe('spliceVisibleIntoFullOrder', () => {
  it('reorders visible sids in place while leaving hidden sids at their original slots', () => {
    // A hidden, B/C/E visible, D hidden. Dragging E to the front of the
    // visible set gives reorderedVisible = [E, B, C].
    const fullOrder: TileLayout = ['A', 'B', 'C', 'D', 'E'];
    const reorderedVisible: TileLayout = ['E', 'B', 'C'];

    expect(spliceVisibleIntoFullOrder(fullOrder, reorderedVisible)).toEqual(
      ['A', 'E', 'B', 'D', 'C'],
    );
  });

  it('is a no-op when every sid is visible', () => {
    const fullOrder: TileLayout = ['A', 'B', 'C'];
    expect(spliceVisibleIntoFullOrder(fullOrder, ['C', 'A', 'B'])).toEqual(['C', 'A', 'B']);
  });

  it('leaves the full order untouched when nothing is visible', () => {
    const fullOrder: TileLayout = ['A', 'B', 'C'];
    expect(spliceVisibleIntoFullOrder(fullOrder, [])).toEqual(fullOrder);
  });
});

describe('chooseColumns', () => {
  it('returns a single 1x1 cell for zero bots', () => {
    expect(chooseColumns(0, 1600, 900, 12, GALLERY_TILE_HEADER_HEIGHT_PX)).toEqual({ cols: 1, rows: 1 });
  });

  it('picks the aspect-ratio-optimal column count for a 20-tile widescreen wall', () => {
    // At 1600x900 with a 12px gap and the production header height, 4 columns (5 rows)
    // lands closest to the 2.2 target chart AR — verified by hand in the
    // design spec's worked example.
    expect(chooseColumns(20, 1600, 900, 12, GALLERY_TILE_HEADER_HEIGHT_PX)).toEqual({ cols: 4, rows: 5 });
  });

  it('uses the compact production header height at a layout decision boundary', () => {
    expect(GALLERY_TILE_HEADER_HEIGHT_PX).toBe(24);
    expect(chooseColumns(2, 960, 480, 12, GALLERY_TILE_HEADER_HEIGHT_PX)).toEqual({ cols: 1, rows: 2 });
  });

  it('never considers more than 6 columns even for a large roster', () => {
    const { cols } = chooseColumns(40, 3200, 1200, 12, GALLERY_TILE_HEADER_HEIGHT_PX);
    expect(cols).toBeLessThanOrEqual(6);
  });

  it('caps columns at n for a roster smaller than MAX_COLS', () => {
    const { cols, rows } = chooseColumns(2, 1600, 900, 12, GALLERY_TILE_HEADER_HEIGHT_PX);
    expect(cols).toBeLessThanOrEqual(2);
    expect(cols * rows).toBeGreaterThanOrEqual(2);
  });

  it('produces a division that fits every tile (rows*cols >= n)', () => {
    for (const n of [1, 2, 3, 5, 7, 11, 13, 20, 25]) {
      const { cols, rows } = chooseColumns(n, 1600, 900, 12, GALLERY_TILE_HEADER_HEIGHT_PX);
      expect(cols * rows).toBeGreaterThanOrEqual(n);
    }
  });

  it('falls back to the tallest-tile division when every candidate is unusably short', () => {
    // A tiny 100x40 area can't clear the production header height + 70px of
    // chart height at any column count for 10 tiles — every candidate is
    // rejected, so the fallback (tallest tile) must still return a valid,
    // fully-covering division rather than throwing or picking nothing.
    const { cols, rows } = chooseColumns(10, 100, 40, 12, GALLERY_TILE_HEADER_HEIGHT_PX);
    expect(cols).toBeGreaterThanOrEqual(1);
    expect(rows).toBeGreaterThanOrEqual(1);
    expect(cols * rows).toBeGreaterThanOrEqual(10);
  });

  it('prefers more columns for a wider grid at the same tile count', () => {
    const narrow = chooseColumns(12, 900, 900, 12, GALLERY_TILE_HEADER_HEIGHT_PX);
    const wide = chooseColumns(12, 2400, 900, 12, GALLERY_TILE_HEADER_HEIGHT_PX);
    expect(wide.cols).toBeGreaterThanOrEqual(narrow.cols);
  });
});

describe('paginate', () => {
  const items = Array.from({ length: 25 }, (_, i) => `item-${i}`);

  it('returns 20 items and 2 pages on page 0 of a 25-item list', () => {
    const { pageItems, pages } = paginate(items, 0);
    expect(pageItems).toHaveLength(20);
    expect(pages).toBe(2);
  });

  it('returns the remaining 5 items on page 1', () => {
    const { pageItems, pages } = paginate(items, 1);
    expect(pageItems).toHaveLength(5);
    expect(pages).toBe(2);
  });

  it('clamps an out-of-range page down to the last page', () => {
    const { pageItems } = paginate(items, 99);
    expect(pageItems).toHaveLength(5);
  });

  it('clamps a negative page up to the first page', () => {
    const { pageItems } = paginate(items, -3);
    expect(pageItems).toHaveLength(20);
  });

  it('reports a single page for an empty list', () => {
    expect(paginate([], 0)).toEqual({ pageItems: [], pages: 1 });
  });

  it('honors a custom page size', () => {
    const { pageItems, pages } = paginate(items, 0, 10);
    expect(pageItems).toHaveLength(10);
    expect(pages).toBe(3);
  });
});

describe('loadLayout / saveLayout / resetLayout', () => {
  const accountId = 'PA3';

  beforeEach(() => {
    localStorage.clear();
  });

  it('round-trips a saved order-only layout', () => {
    const layout: TileLayout = ['sid-1', 'sid-2'];

    saveLayout(accountId, layout);

    expect(loadLayout(accountId)).toEqual(layout);
  });

  it('returns [] when nothing has been saved for that account', () => {
    expect(loadLayout('never-saved-account')).toEqual([]);
  });

  it('returns [] for corrupt JSON instead of throwing', () => {
    localStorage.setItem(`gallery-layout:${accountId}`, 'not-json{{{');

    expect(loadLayout(accountId)).toEqual([]);
  });

  it('returns [] for a well-formed JSON value that is not a sid array', () => {
    localStorage.setItem(`gallery-layout:${accountId}`, JSON.stringify({ not: 'an array' }));

    expect(loadLayout(accountId)).toEqual([]);
  });

  it('returns [] for an array containing a non-string entry', () => {
    for (const bad of [
      ['sid-1', 42],
      ['sid-1', null],
      ['sid-1', { sid: 'sid-2' }],
    ]) {
      localStorage.setItem(`gallery-layout:${accountId}`, JSON.stringify(bad));
      expect(loadLayout(accountId)).toEqual([]);
    }
  });

  it('returns [] when localStorage.getItem throws', () => {
    const getItemSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage unavailable');
    });

    expect(loadLayout(accountId)).toEqual([]);

    getItemSpy.mockRestore();
  });

  it('clears the persisted layout on reset', () => {
    saveLayout(accountId, ['sid-1']);

    resetLayout(accountId);

    expect(loadLayout(accountId)).toEqual([]);
  });

  it('does not throw when saveLayout is called while localStorage.setItem throws', () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded');
    });

    expect(() => saveLayout(accountId, ['sid-1'])).not.toThrow();

    setItemSpy.mockRestore();
  });

  it('does not throw when resetLayout is called while localStorage.removeItem throws', () => {
    const removeItemSpy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('storage unavailable');
    });

    expect(() => resetLayout(accountId)).not.toThrow();

    removeItemSpy.mockRestore();
  });
});
