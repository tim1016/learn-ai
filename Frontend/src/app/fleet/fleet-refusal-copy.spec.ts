/**
 * Contract test for the fleet refusal fallback copy (#2067 / #2102).
 *
 * `fleet-refusal-copy.ts` is a client-authored safety net, not the primary
 * copy source (`FleetControlError.detail()`'s `message`/`next_step` always
 * win — see that module's docstring). This test proves the net actually
 * covers the closed vocabulary it is locked to:
 *
 * 1. **Snapshot -> fallback parity** — every code in the committed
 *    `fleet-refusal-vocabulary.snapshot.json` has an entry in
 *    `FLEET_REFUSAL_COPY`. A reason code minted without a matching entry
 *    here would otherwise render blank instead of falling back.
 * 2. **No orphan fallback entries** — every code in `FLEET_REFUSAL_COPY`
 *    still exists in the snapshot (a retired family's stale copy is a
 *    maintenance hazard, not a real fallback).
 * 3. **Non-empty prose** — every fallback entry has real message and
 *    next-step text, not an empty string a caller would render as blank.
 * 4. **Outcome derivation matches meaning** — conflicts map to `'conflict'`,
 *    an unsettled dispatch maps to `'unknown'`, and other refusals map to
 *    `'failure'`.
 *
 * Adding a fleet refusal family requires updating:
 *   1. PythonDataService/app/broker/fleet/refusal_vocabulary.py
 *   2. PythonDataService/scripts/regenerate_fleet_refusal_vocabulary_snapshot.py output
 *   3. fleet-refusal-copy.ts (FLEET_REFUSAL_COPY)
 * Any missing step fails this test.
 */
import { describe, expect, it } from 'vitest';
import snapshot from './fleet-refusal-vocabulary.snapshot.json';
import { FLEET_REFUSAL_COPY, fleetRefusalCopyFor } from './fleet-refusal-copy';

const SNAPSHOT_REASONS = snapshot.reasons as Record<string, { status_code: number; meaning: string }>;
const SNAPSHOT_CODES = Object.keys(SNAPSHOT_REASONS);

describe('FLEET_REFUSAL_COPY', () => {
  it('has an entry for every code in the fleet refusal vocabulary snapshot', () => {
    const missing = SNAPSHOT_CODES.filter((code) => !(code in FLEET_REFUSAL_COPY));

    // If this fails: a reason code was added to refusal_vocabulary.py /
    // the snapshot but fleet-refusal-copy.ts was not updated to match.
    expect(missing).toEqual([]);
  });

  it('carries no fallback entry for a code outside the snapshot', () => {
    const codeSet = new Set(SNAPSHOT_CODES);
    const orphans = Object.keys(FLEET_REFUSAL_COPY).filter((code) => !codeSet.has(code));

    // If this fails: the listed code(s) were removed from the backend
    // vocabulary but not from FLEET_REFUSAL_COPY. Remove the stale entries.
    expect(orphans).toEqual([]);
  });

  it('gives every entry non-empty message and next-step prose', () => {
    const blank = SNAPSHOT_CODES.filter((code) => {
      const entry = FLEET_REFUSAL_COPY[code];
      return !entry || !entry.message.trim() || !entry.nextStep.trim();
    });

    expect(blank).toEqual([]);
  });

  it('derives unknown for an unsettled dispatch, conflict for 409, and failure otherwise', () => {
    const mismatched = SNAPSHOT_CODES.filter((code) => {
      const expected = code === 'clerk_routing_outcome_unknown'
        ? 'unknown'
        : SNAPSHOT_REASONS[code].status_code === 409 ? 'conflict' : 'failure';
      return FLEET_REFUSAL_COPY[code]?.outcome !== expected;
    });

    expect(mismatched).toEqual([]);
  });

  it('anti-vacuous: the snapshot actually names a 409 family and a non-409 family', () => {
    // A snapshot that regressed to all-409 (or all-non-409) would make the
    // outcome-derivation test above pass without exercising both branches.
    const statuses = new Set(SNAPSHOT_CODES.map((code) => SNAPSHOT_REASONS[code].status_code));

    expect(statuses.has(409)).toBe(true);
    expect([...statuses].some((status) => status !== 409)).toBe(true);
  });
});

describe('fleetRefusalCopyFor', () => {

  it.each([
    ['a code outside the closed vocabulary', 'not_a_real_fleet_reason'],
    ['a null reason code', null],
  ])('returns null for %s', (_name, code) => {
    expect(fleetRefusalCopyFor(code)).toBeNull();
  });
});
