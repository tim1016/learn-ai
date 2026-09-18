import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { ActiveLensBridgeService } from './active-lens-bridge.service';

describe('ActiveLensBridgeService', () => {
  it('starts with no host registered', () => {
    const bridge = TestBed.inject(ActiveLensBridgeService);
    expect(bridge.host()).toBeNull();
  });

  it('exposes a registered host until it unregisters', () => {
    const bridge = TestBed.inject(ActiveLensBridgeService);
    const select = vi.fn();
    const unregister = bridge.register({ lens: signal('trader'), select });

    expect(bridge.host()?.select).toBe(select);

    unregister();

    expect(bridge.host()).toBeNull();
  });

  it('lets a second host replace the first without the first unregistering it', () => {
    const bridge = TestBed.inject(ActiveLensBridgeService);
    const firstSelect = vi.fn();
    const secondSelect = vi.fn();
    const unregisterFirst = bridge.register({ lens: signal('trader'), select: firstSelect });
    bridge.register({ lens: signal('operator'), select: secondSelect });

    expect(bridge.host()?.select).toBe(secondSelect);

    // A stale cleanup — the first host unmounting after being replaced —
    // must not clobber the second host's registration.
    unregisterFirst();

    expect(bridge.host()?.select).toBe(secondSelect);
  });
});
