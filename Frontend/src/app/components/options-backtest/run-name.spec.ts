import { computed } from '@angular/core';
import { describe, expect, it } from 'vitest';

import { RunNameTracker } from './run-name';

describe('the name a run is recorded under', () => {
  it('is nothing before any setup was opened or saved', () => {
    const tracker = new RunNameTracker();

    expect(tracker.name()).toBeNull();
    expect(tracker.nameFor('{"a":1}')).toBeNull();
  });

  it('is the name of the setup that was opened while the setup on screen is that one, nothing once it has been edited, and the name again when the edit is undone', () => {
    const tracker = new RunNameTracker();
    tracker.set('Straddle', '{"a":1}');

    expect.soft(tracker.name(), 'is the name of the setup that was opened').toBe('Straddle');
    expect.soft(tracker.nameFor('{"a":1}'), 'is the name of the setup that was opened, while the setup on screen is that one').toBe('Straddle');
    expect.soft(tracker.nameFor('{"a":2}'), 'is nothing once the setup has been edited').toBeNull();
    expect.soft(tracker.name(), 'is still the name that was opened once the setup has been edited').toBe('Straddle');
    expect.soft(tracker.nameFor('{"a":2}'), 'is nothing while the edit stands, before it is undone').toBeNull();
    expect.soft(tracker.nameFor('{"a":1}'), 'is the name again when the edit is undone, since it is the same setup').toBe('Straddle');
  });

  it('is the name it was saved under, in place of the one the setup was opened under', () => {
    const tracker = new RunNameTracker();
    tracker.set('Straddle', '{"a":1}');

    tracker.set('Strangle', '{"a":2}');

    expect(tracker.nameFor('{"a":1}')).toBeNull();
    expect(tracker.nameFor('{"a":2}')).toBe('Strangle');
  });

  it('is nothing after the form is replaced by a setup that has no name, whatever its body, and counts each replacement', () => {
    const tracker = new RunNameTracker();
    const start = tracker.replacements;
    tracker.set('Straddle', '{"a":1}');

    tracker.clear();

    expect.soft(tracker.name(), 'is nothing after the form is replaced by a setup that has no name').toBeNull();
    expect.soft(tracker.nameFor('{"a":1}'), 'is nothing after the form is replaced by a setup that has no name, whatever its body').toBeNull();
    expect.soft(tracker.replacements - start, 'counts each time the form is replaced, by a named setup or by one with no name, so a save under way can tell').toBe(2);
  });

  it('is nothing for a setup opened with no body to compare, even for another with none', () => {
    const tracker = new RunNameTracker();
    tracker.set('Incomplete', null);

    expect(tracker.nameFor('{"a":1}')).toBeNull();
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
