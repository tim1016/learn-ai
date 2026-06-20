// PRD #607 / Slice 1 (#608) — reason-code lookup tests.
//
// Closure: every server-documented code maps to a non-empty string.
// Fallback: unknown codes render the raw code (never silenced).

import { describe, expect, it } from 'vitest';

import {
  ACTION_REASON_COPY,
  type ActionReasonCode,
  getActionReasonCopy,
} from './action-reason-codes';

const DOCUMENTED_CODES: ActionReasonCode[] = [
  'NO_LIVE_BINDING',
  'SAFETY_BLOCK_HALT',
  'RECONCILE_NOT_WIRED',
  'NO_OWNED_POSITIONS',
  'ALREADY_POISONED',
];

describe('action reason-code lookup', () => {
  it.each(DOCUMENTED_CODES)(
    'maps documented code %s to a non-empty operator-language string',
    (code) => {
      const copy = ACTION_REASON_COPY[code];
      expect(copy).toBeTruthy();
      expect(copy.length).toBeGreaterThan(0);
    },
  );

  it('returns the raw code for an unknown token so the gap is visible', () => {
    // The whole point: surfacing the gap loudly is better than rendering
    // an empty tooltip.
    expect(getActionReasonCopy('SOME_FUTURE_CODE')).toBe('SOME_FUTURE_CODE');
  });

  it('returns the documented copy for known codes via the helper', () => {
    for (const code of DOCUMENTED_CODES) {
      expect(getActionReasonCopy(code)).toBe(ACTION_REASON_COPY[code]);
    }
  });

  it('returns the empty string for null and empty inputs', () => {
    expect(getActionReasonCopy(null)).toBe('');
    expect(getActionReasonCopy(undefined)).toBe('');
    expect(getActionReasonCopy('')).toBe('');
  });

  it('contains only documented codes in the map (regression guard)', () => {
    const mapped = Object.keys(ACTION_REASON_COPY).sort();
    const expected = [...DOCUMENTED_CODES].sort();
    expect(mapped).toEqual(expected);
  });

  it('does not surface tokens the server explicitly removed (#608)', () => {
    // BUSY_VERB_IN_FLIGHT lives only in Angular request state.
    // ALREADY_RUNNING / NOT_RUNNING described eligibility for intent
    // transitions that durable writes do not have.
    expect(ACTION_REASON_COPY).not.toHaveProperty('BUSY_VERB_IN_FLIGHT');
    expect(ACTION_REASON_COPY).not.toHaveProperty('ALREADY_RUNNING');
    expect(ACTION_REASON_COPY).not.toHaveProperty('NOT_RUNNING');
  });
});
