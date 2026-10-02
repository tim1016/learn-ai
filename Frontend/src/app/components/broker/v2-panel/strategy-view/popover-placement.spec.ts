import { describe, expect, it } from 'vitest';

import { popoverPosition } from './popover-placement';

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
