/**
 * Copy-authority contract test for the broker-v2 vocabulary.
 *
 * The snapshot (`broker-v2-vocabulary.snapshot.json`) is the single source
 * of truth for vocabulary codes AND their server-authored copy.  This test
 * asserts three things independently:
 *
 * 1. **Server copy completeness** — every code in the snapshot has a non-empty
 *    label and explanation in `snapshot.copy`.  A missing or empty server value
 *    fails this test visibly.  The TS emergency-fallback map is NOT consulted
 *    here; that map is an explicit last-resort, not the authority.
 *
 * 2. **Snapshot ↔ fallback parity** — every snapshot code has an entry in
 *    `BROKER_V2_EMERGENCY_COPY`, and every fallback entry has non-empty copy.
 *    This ensures the emergency map covers the full vocabulary if the server
 *    ever fails to deliver copy at runtime.
 *
 * 3. **No orphan fallback entries** — every code in `BROKER_V2_EMERGENCY_COPY`
 *    exists in the snapshot.  Stale entries in the fallback map are a
 *    maintenance hazard.
 *
 * Adding a vocabulary code requires updating:
 *   1. PythonDataService/app/broker/v2panel/vocabulary.py
 *   2. Re-running PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py
 *   3. broker-v2-emergency-copy.ts (BROKER_V2_EMERGENCY_COPY)
 * Any missing step will fail this test.
 */
import { describe, expect, it } from 'vitest';
import snapshot from './broker-v2-vocabulary.snapshot.json';
import { BROKER_V2_EMERGENCY_COPY } from './broker-v2-emergency-copy';

const SNAPSHOT_CODES: readonly string[] = snapshot.codes;
const SERVER_COPY = snapshot.copy as Record<string, { label: string; explanation: string }>;

describe('broker-v2 copy contract', () => {
  // ── 1. Server copy completeness ──────────────────────────────────────────
  it('snapshot.copy has an entry for every vocabulary code', () => {
    const missing: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      if (!(code in SERVER_COPY)) {
        missing.push(code);
      }
    }
    // If this fails: re-run regenerate_broker_v2_vocabulary_snapshot.py to
    // add the missing code(s) to snapshot.copy before deploying.
    expect(missing).toEqual([]);
  });

  it('every snapshot.copy entry has a non-empty label', () => {
    const invalid: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      const entry = SERVER_COPY[code];
      if (!entry || !entry.label.trim()) {
        invalid.push(code);
      }
    }
    // If this fails: the backend vocabulary.py OPERATOR_COPY map is missing a
    // label for the listed code(s).  Fix it there; do NOT paper over it in the
    // TS emergency-fallback map.
    expect(invalid).toEqual([]);
  });

  it('every snapshot.copy entry has a non-empty explanation', () => {
    const invalid: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      const entry = SERVER_COPY[code];
      if (!entry || !entry.explanation.trim()) {
        invalid.push(code);
      }
    }
    // Same as above — fix in vocabulary.py OPERATOR_COPY, not in the TS map.
    expect(invalid).toEqual([]);
  });

  // ── 2. Emergency-fallback parity ─────────────────────────────────────────
  it('emergency fallback map covers every code in the vocabulary snapshot', () => {
    const missing: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      if (!(code in BROKER_V2_EMERGENCY_COPY)) {
        missing.push(code);
      }
    }
    // failure output: `missing` lists the codes absent from BROKER_V2_EMERGENCY_COPY
    expect(missing).toEqual([]);
  });

  it('every fallback entry has a non-empty label and explanation', () => {
    const invalid: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      const entry = BROKER_V2_EMERGENCY_COPY[code];
      if (!entry) continue; // caught by the previous test
      if (!entry.label.trim() || !entry.explanation.trim()) {
        invalid.push(code);
      }
    }
    expect(invalid).toEqual([]);
  });

  // ── 3. No orphan fallback entries ────────────────────────────────────────
  it('no code in BROKER_V2_EMERGENCY_COPY is absent from the snapshot', () => {
    const codeSet = new Set(SNAPSHOT_CODES);
    const orphans = Object.keys(BROKER_V2_EMERGENCY_COPY).filter(
      (code) => !codeSet.has(code),
    );
    // If this fails: the listed code(s) were removed from vocabulary.py but
    // not from BROKER_V2_EMERGENCY_COPY.  Remove the stale entries.
    expect(orphans).toEqual([]);
  });
});
