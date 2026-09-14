import { Injector, runInInjectionContext, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { openLaneFence } from './open-lane-fence';
import type { LaneFence } from './lane-fence';

function fence(bindingGeneration: number | null): LaneFence {
  return { bindingGeneration, routingEpoch: 1 };
}

describe('openLaneFence', () => {
  it('materializes the fence eagerly, before anything reads the returned signal', () => {
    // linkedSignal's computation is lazy on its own — a value only ever read
    // inside a click handler would freeze at CLICK time, not OPEN time,
    // silently freezing nothing (#2068). The internal eager effect() is what
    // forces it to run as soon as the lane renders.
    const freeze = vi.fn(() => fence(3));
    runInInjectionContext(TestBed.inject(Injector), () => openLaneFence(freeze, () => 'lane-a'));

    expect(freeze).not.toHaveBeenCalled();
    TestBed.tick();
    expect(freeze).toHaveBeenCalledTimes(1);
  });

  it('does not re-derive when a signal read inside freeze changes, only when source changes', () => {
    // This is the untracked() guarantee: a live directory refresh reads
    // through `freeze`, but must not silently re-derive — and therefore
    // un-freeze — an already-opened fence.
    const live = signal(3);
    const source = signal('lane-a');
    const result = runInInjectionContext(TestBed.inject(Injector), () =>
      openLaneFence(() => fence(live()), () => source()),
    );
    TestBed.tick();
    expect(result()).toEqual(fence(3));

    live.set(4);
    TestBed.tick();
    expect(result()).toEqual(fence(3));

    source.set('lane-b');
    TestBed.tick();
    expect(result()).toEqual(fence(4));
  });
});
