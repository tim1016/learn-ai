/**
 * Copy-authority contract test for the broker-v2 vocabulary.
 *
 * This test locks the emergency-fallback copy map against the vocabulary
 * snapshot. It fails visibly when:
 *   (a) A code in the snapshot is missing from the fallback map.
 *   (b) A code in the fallback map has empty label or explanation.
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

describe('broker-v2 copy contract', () => {
  it('emergency fallback map covers every code in the vocabulary snapshot', () => {
    const missing: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      if (!(code in BROKER_V2_EMERGENCY_COPY)) {
        missing.push(code);
      }
    }
    expect(missing).toEqual(
      [],
      `Emergency-fallback copy is missing entries for: ${missing.join(', ')}. ` +
        'Add them to broker-v2-emergency-copy.ts.',
    );
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
    expect(invalid).toEqual(
      [],
      `Fallback entries with empty label or explanation: ${invalid.join(', ')}`,
    );
  });

  it('no code in the snapshot produces an unknown label (server-omit simulation)', () => {
    // Simulate what happens when the server omits copy for a code:
    // the consumer should fall back to BROKER_V2_EMERGENCY_COPY and find a non-empty label.
    // This test documents the contract: every snapshot code has emergency cover.
    const failures: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      const serverCopy = simulateServerOmit(code);
      const resolved = serverCopy.label || BROKER_V2_EMERGENCY_COPY[code]?.label || '';
      if (!resolved.trim()) {
        failures.push(code);
      }
    }
    expect(failures).toEqual(
      [],
      `No fallback label available for codes when server omits copy: ${failures.join(', ')}`,
    );
  });

  it('no code in the snapshot produces an unknown explanation (server-omit simulation)', () => {
    const failures: string[] = [];
    for (const code of SNAPSHOT_CODES) {
      const serverCopy = simulateServerOmit(code);
      const resolved =
        serverCopy.explanation || BROKER_V2_EMERGENCY_COPY[code]?.explanation || '';
      if (!resolved.trim()) {
        failures.push(code);
      }
    }
    expect(failures).toEqual(
      [],
      `No fallback explanation available for codes when server omits copy: ${failures.join(', ')}`,
    );
  });
});

/** Returns an empty copy object, simulating a server response that omits copy for the code. */
function simulateServerOmit(_code: string): { label: string; explanation: string } {
  return { label: '', explanation: '' };
}
