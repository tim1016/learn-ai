// PRD #607 / Slice 3 (#610) — closure tests for the typed pill-description map.

import { describe, expect, it } from 'vitest';

import {
  PILL_DESCRIPTIONS,
  pillDescription,
  type BannerPillId,
} from './pill-descriptions';

const PILL_IDS: BannerPillId[] = ['fleet', 'state', 'intent', 'safety', 'last_run'];

describe('PILL_DESCRIPTIONS', () => {
  it.each(PILL_IDS)('has a non-empty description and aria-hint for %s', (id) => {
    const entry = PILL_DESCRIPTIONS[id];
    expect(entry).toBeTruthy();
    expect(entry.label.length).toBeGreaterThan(0);
    expect(entry.description.length).toBeGreaterThan(0);
    expect(entry.ariaHint.length).toBeGreaterThan(0);
  });

  it('is closed — the map contains exactly the documented pills (regression guard)', () => {
    const mapped = Object.keys(PILL_DESCRIPTIONS).sort();
    const expected = [...PILL_IDS].sort();
    expect(mapped).toEqual(expected);
  });

  it('pillDescription() returns the documented entry for each id', () => {
    for (const id of PILL_IDS) {
      expect(pillDescription(id)).toBe(PILL_DESCRIPTIONS[id]);
    }
  });
});
