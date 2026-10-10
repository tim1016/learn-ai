import { ViewportScroller } from '@angular/common';
import { inject } from '@angular/core';

/**
 * The live height of the shell's sticky top bar, read at scroll time because
 * the bar grows when the lane-status pill wraps on narrow screens.
 */
export function stickyBarOffset(): [number, number] {
  const bar = document.querySelector('app-top-bar');
  return [0, bar ? bar.getBoundingClientRect().height : 0];
}

/**
 * Router anchor scrolling (`withInMemoryScrolling({anchorScrolling: 'enabled'})`)
 * scrolls with `window.scrollTo` on the anchor's rect and ignores CSS
 * `scroll-margin-top`, so without an offset a fragment link lands its target
 * flush under the sticky top bar — one frame after the markdown viewer's own
 * margin-aware smooth scroll started, cancelling it. Offsetting by the bar
 * height makes the router's scroll land below the bar (#3020).
 */
export function configureShellViewportScroller(): void {
  inject(ViewportScroller).setOffset(stickyBarOffset);
}
