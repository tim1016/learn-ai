import { computed } from '@angular/core';
import { describe, expect, it } from 'vitest';

import { RunNameTracker } from './run-name';

describe('the name a run is recorded under', () => {
  it('is nothing before any setup was opened or saved', () => {
    const tracker = new RunNameTracker();

    expect(tracker.name()).toBeNull();
    expect(tracker.nameFor('{"a":1}')).toBeNull();
  });

  it('is the name of the setup that was opened, while the setup on screen is that one', () => {
    const tracker = new RunNameTracker();

    tracker.set('Straddle', '{"a":1}');

    expect(tracker.name()).toBe('Straddle');
    expect(tracker.nameFor('{"a":1}')).toBe('Straddle');
  });

  it('is nothing once the setup has been edited, though the name is still the one that was opened', () => {
    const tracker = new RunNameTracker();
    tracker.set('Straddle', '{"a":1}');

    expect(tracker.nameFor('{"a":2}')).toBeNull();
    expect(tracker.name()).toBe('Straddle');
  });

  it('is the name again when the edit is undone, since it is the same setup', () => {
    const tracker = new RunNameTracker();
    tracker.set('Straddle', '{"a":1}');

    expect(tracker.nameFor('{"a":2}')).toBeNull();
    expect(tracker.nameFor('{"a":1}')).toBe('Straddle');
  });

  it('is the name it was saved under, in place of the one the setup was opened under', () => {
    const tracker = new RunNameTracker();
    tracker.set('Straddle', '{"a":1}');

    tracker.set('Strangle', '{"a":2}');

    expect(tracker.nameFor('{"a":1}')).toBeNull();
    expect(tracker.nameFor('{"a":2}')).toBe('Strangle');
  });

  it('is nothing after the form is replaced by a setup that has no name, whatever its body', () => {
    const tracker = new RunNameTracker();
    tracker.set('Straddle', '{"a":1}');

    tracker.clear();

    expect(tracker.name()).toBeNull();
    expect(tracker.nameFor('{"a":1}')).toBeNull();
  });

  it('is nothing for a setup opened with no body to compare, even for another with none', () => {
    const tracker = new RunNameTracker();
    tracker.set('Incomplete', null);

    expect(tracker.nameFor('{"a":1}')).toBeNull();
  });

  it('counts each time the form is replaced, by a named setup or by one with no name, so a save under way can tell', () => {
    const tracker = new RunNameTracker();
    const start = tracker.replacements;

    tracker.set('Straddle', '{"a":1}');
    tracker.clear();

    expect(tracker.replacements - start).toBe(2);
  });

  it('is read by a computed, which follows each change of it', () => {
    const tracker = new RunNameTracker();
    const forBody = computed(() => tracker.nameFor('{"a":1}'));
    expect(forBody()).toBeNull();

    tracker.set('Straddle', '{"a":1}');
    expect(forBody()).toBe('Straddle');
    tracker.set('Straddle', '{"a":2}');
    expect(forBody()).toBeNull();
    tracker.set('Strangle', '{"a":1}');
    expect(forBody()).toBe('Strangle');
    tracker.clear();
    expect(forBody()).toBeNull();
  });
});
