/**
 * Copy-authority contract test for the broker-v2 vocabulary.
 *
 * The snapshot (`broker-v2-vocabulary.snapshot.json`) is the single source
 * of truth for vocabulary codes AND their server-authored copy. There is no
 * client-side copy map: every code must carry a non-empty label and
 * explanation in `snapshot.copy`, and a missing or empty server value fails
 * this test visibly.
 *
 * Adding a vocabulary code requires updating:
 *   1. PythonDataService/app/broker/v2panel/vocabulary.py
 *   2. Re-running PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py
 * Any missing step will fail this test.
 */
import { describe, expect, it } from 'vitest';
import snapshot from './broker-v2-vocabulary.snapshot.json';

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
    // label for the listed code(s).  Fix it there.
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
    // Same as above — fix in vocabulary.py OPERATOR_COPY.
    expect(invalid).toEqual([]);
  });
});
