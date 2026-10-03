import { afterEach, describe, expect, it, vi } from 'vitest';

import { keepInsideViewport, popoverPosition } from './popover-placement';

const VIEWPORT = { width: 1000, height: 700 };
const SIZE = { width: 400, height: 300 };

describe('popoverPosition', () => {
  it('opens below its anchor', () => {
    expect(popoverPosition({ left: 100, top: 50, bottom: 80 }, SIZE, VIEWPORT)).toEqual({ left: 100, top: 88 });
  });

  it('flips above an anchor near the bottom and stays inside the right edge', () => {
    expect(popoverPosition({ left: 900, top: 600, bottom: 620 }, SIZE, VIEWPORT)).toEqual({ left: 592, top: 292 });
  });

  it('never leaves the top-left corner of the viewport', () => {
    expect(popoverPosition({ left: -40, top: 100, bottom: 500 }, SIZE, VIEWPORT)).toEqual({ left: 8, top: 8 });
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

  it('slides a dropdown that runs off the right edge back inside it', () => {
    const summary = dropdownAt(194, 358);

    keepInsideViewport(summary);

    expect(summary.style.translate).toBe('-170px 0');
  });

  it('slides one that starts off the left edge back inside it', () => {
    const menu = dropdownAt(-32, 240);

    keepInsideViewport(menu);

    expect(menu.style.translate).toBe('40px 0');
  });

  it('leaves one that fits where it is', () => {
    const menu = dropdownAt(40, 240);
    menu.style.translate = '12px 0';

    keepInsideViewport(menu);

    expect(menu.style.translate).toBe('');
  });
});
