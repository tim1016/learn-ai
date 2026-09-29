import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mediaQuerySignal } from './media-query';

/** A media query list whose match the test moves, as a resized window would. */
function fakeQueryList(initial: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  return {
    matches: initial,
    addEventListener: (_type: 'change', listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: 'change', listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    resize(matches: boolean) {
      for (const listener of listeners) listener({ matches } as MediaQueryListEvent);
    },
    listeners,
  };
}

describe('mediaQuerySignal', () => {
  const original = window.matchMedia;
  afterEach(() => {
    window.matchMedia = original;
    TestBed.resetTestingModule();
  });

  it('starts at the query’s current match and follows the viewport', () => {
    const list = fakeQueryList(false);
    window.matchMedia = vi.fn(() => list as unknown as MediaQueryList);

    const wide = TestBed.runInInjectionContext(() => mediaQuerySignal('(min-width: 48rem)'));

    expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 48rem)');
    expect(wide()).toBe(false);
    list.resize(true);
    expect(wide()).toBe(true);
  });

  it('stops listening when its injector is destroyed', () => {
    const list = fakeQueryList(true);
    window.matchMedia = vi.fn(() => list as unknown as MediaQueryList);

    TestBed.runInInjectionContext(() => mediaQuerySignal('(min-width: 48rem)'));
    expect(list.listeners.size).toBe(1);

    TestBed.resetTestingModule();
    expect(list.listeners.size).toBe(0);
  });
});
