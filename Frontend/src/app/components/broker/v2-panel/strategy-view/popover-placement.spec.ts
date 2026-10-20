import { afterEach, describe, expect, it, vi } from 'vitest';

import { keepInsideViewport, popoverPosition } from './popover-placement';

const VIEWPORT = { width: 1000, height: 700 };
const SIZE = { width: 400, height: 300 };

describe('popoverPosition', () => {
  it.each([
    ['opens below its anchor', { left: 100, top: 50, bottom: 80 }, { left: 100, top: 88 }],
    ['flips above an anchor near the bottom and stays inside the right edge', { left: 900, top: 600, bottom: 620 }, { left: 592, top: 292 }],
    ['never leaves the top-left corner of the viewport', { left: -40, top: 100, bottom: 500 }, { left: 8, top: 8 }],
  ])('%s', (_name, anchor, want) => {
    expect(popoverPosition(anchor, SIZE, VIEWPORT)).toEqual(want);
  });
});

describe('keepInsideViewport', () => {
  afterEach(() => vi.unstubAllGlobals());

  /** A dropdown laid out at `left`, `width` wide, in a 390 px phone window. */
  function dropdownAt(left: number, width: number): HTMLElement {
    vi.stubGlobal('innerWidth', 390);
    const element = document.createElement('div');
    element.getBoundingClientRect = () => new DOMRect(left, 0, width, 100);
    return element;
  }

  it.each([
    ['slides a dropdown that runs off the right edge back inside it', 194, 358, '-170px 0'],
    ['slides one that starts off the left edge back inside it', -32, 240, '40px 0'],
  ])('%s', (_name, left, width, want) => {
    const menu = dropdownAt(left, width);

    keepInsideViewport(menu);

    expect(menu.style.translate).toBe(want);
  });

  it('leaves one that fits where it is', () => {
    const menu = dropdownAt(40, 240);
    menu.style.translate = '12px 0';

    keepInsideViewport(menu);

    expect(menu.style.translate).toBe('');
  });
});
