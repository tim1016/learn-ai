import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  autoDivision,
  loadLayout,
  paginate,
  resetLayout,
  saveLayout,
  type TileLayout,
} from './gallery-layout';

describe('autoDivision', () => {
  it('divides 20 into a 5x4 near-square grid', () => {
    expect(autoDivision(20)).toEqual({ cols: 5, rows: 4 });
  });

  it('divides 9 into a 3x3 square grid', () => {
    expect(autoDivision(9)).toEqual({ cols: 3, rows: 3 });
  });

  it('divides 6 into a 3x2 grid', () => {
    expect(autoDivision(6)).toEqual({ cols: 3, rows: 2 });
  });

  it('returns a single 1x1 cell for zero bots', () => {
    expect(autoDivision(0)).toEqual({ cols: 1, rows: 1 });
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

  it('round-trips a saved layout', () => {
    const layout: TileLayout[] = [
      { sid: 'sid-1', colSpan: 2, rowSpan: 1 },
      { sid: 'sid-2', colSpan: 1, rowSpan: 1 },
    ];

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

  it('returns [] for a well-formed JSON value that is not a tile-layout array', () => {
    localStorage.setItem(`gallery-layout:${accountId}`, JSON.stringify({ not: 'an array' }));

    expect(loadLayout(accountId)).toEqual([]);
  });

  it('returns [] for a corrupted entry with a NaN, negative, or fractional span', () => {
    for (const bad of [
      [{ sid: 'sid-1', colSpan: Number.NaN, rowSpan: 1 }],
      [{ sid: 'sid-1', colSpan: -1, rowSpan: 1 }],
      [{ sid: 'sid-1', colSpan: 0, rowSpan: 1 }],
      [{ sid: 'sid-1', colSpan: 1.5, rowSpan: 1 }],
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
    saveLayout(accountId, [{ sid: 'sid-1', colSpan: 1, rowSpan: 1 }]);

    resetLayout(accountId);

    expect(loadLayout(accountId)).toEqual([]);
  });

  it('does not throw when saveLayout is called while localStorage.setItem throws', () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded');
    });

    expect(() => saveLayout(accountId, [{ sid: 'sid-1', colSpan: 1, rowSpan: 1 }])).not.toThrow();

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
