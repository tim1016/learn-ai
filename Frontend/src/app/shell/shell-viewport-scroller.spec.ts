import { describe, expect, it } from 'vitest';

import { stickyBarOffset } from './shell-viewport-scroller';

function appendBar(height: number): HTMLElement {
  const bar = document.createElement('app-top-bar');
  bar.getBoundingClientRect = () =>
    ({ height, width: 390, top: 0, left: 0, right: 390, bottom: height, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  document.body.appendChild(bar);
  return bar;
}

describe('stickyBarOffset', () => {
  it('reads the sticky top bar height at call time', () => {
    const bar = appendBar(61.5);
    try {
      expect(stickyBarOffset()).toEqual([0, 61.5]);
    } finally {
      bar.remove();
    }
  });

  it('offsets nothing when the bar is absent', () => {
    expect(stickyBarOffset()).toEqual([0, 0]);
  });
});
