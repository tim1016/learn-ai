// Cross-stack parity test for the operator-language copy map.
//
// The Python source of truth lives in:
//   PythonDataService/app/services/operator_capability.py → REASON_CODES
//   PythonDataService/app/services/resume_guard_state.py → RESUME_REASON_CODES
//
// The Python-side regenerator script writes two byte-identical JSON
// snapshots (one in PythonDataService/, one here). The pytest
// `test_operator_reason_codes_snapshot.py` asserts the Python-tree
// copy matches the live `REASON_CODES` set; this Vitest asserts the
// Frontend-tree copy matches `ALL_OPERATOR_REASON_CODES`. Together
// they form a real cross-stack contract — adding a code on the
// server fails one of the two tests. The 2026-06-22 cockpit audit
// review F-R4 closure required this design (the earlier parity test
// compared two manually-maintained TS lists and would not have
// caught a Python-side addition).

import { describe, expect, it } from 'vitest';

import {
  ALL_OPERATOR_REASON_CODES,
  ALL_LOCAL_REASON_CODES,
  actionTooltip,
  disabledReasonCopy,
} from './disabled-reason-copy';
// Direct JSON import — resolveJsonModule + the snapshot file
// committed alongside this spec gives Vitest a typed handle on the
// cross-stack snapshot without filesystem APIs.
import snapshotJson from './operator-reason-codes.snapshot.json';

interface ReasonCodesSnapshot {
  readonly $comment: string;
  readonly generated_by: string;
  readonly source_files: readonly string[];
  readonly codes: readonly string[];
}

const snapshot = snapshotJson as ReasonCodesSnapshot;

describe('disabled-reason-copy parity with server closed vocabulary (cross-stack)', () => {
  it('loads the committed snapshot file', () => {
    expect(snapshot.codes.length).toBeGreaterThan(0);
    expect(snapshot.generated_by).toContain('regenerate_operator_reason_codes_snapshot');
    expect(snapshot.source_files.some((s) => s.includes('operator_capability.py'))).toBe(true);
    expect(snapshot.source_files.some((s) => s.includes('resume_guard_state.py'))).toBe(true);
  });

  it('frontend copy map matches the snapshot exactly (no missing, no extra codes)', () => {
    const actual = new Set<string>(ALL_OPERATOR_REASON_CODES);
    const expected = new Set<string>(snapshot.codes);

    const missing = [...expected].filter((c) => !actual.has(c));
    const extra = [...actual].filter((c) => !expected.has(c));

    expect(missing, 'codes the snapshot has but the frontend map is missing').toEqual([]);
    expect(extra, 'codes the frontend map has but the snapshot does not').toEqual([]);
  });

  it('every operator code maps to non-trivial operator-language copy', () => {
    for (const code of ALL_OPERATOR_REASON_CODES) {
      const copy = disabledReasonCopy(code);
      expect(copy).toBeTruthy();
      // Operator-language: longer than the raw code (catches accidental
      // copy = code regressions) and not equal to the raw code.
      expect(copy?.length ?? 0).toBeGreaterThan(code.length);
      expect(copy).not.toBe(code);
    }
  });

  it('every local code maps to non-trivial copy', () => {
    for (const code of ALL_LOCAL_REASON_CODES) {
      const copy = disabledReasonCopy(code);
      expect(copy).toBeTruthy();
      expect(copy?.length ?? 0).toBeGreaterThan(code.length);
    }
  });
});

describe('disabledReasonCopy', () => {
  it('returns null for empty / undefined / null', () => {
    expect(disabledReasonCopy(null)).toBeNull();
    expect(disabledReasonCopy(undefined)).toBeNull();
    expect(disabledReasonCopy('')).toBeNull();
  });

  it('returns operator copy for a known server code', () => {
    const copy = disabledReasonCopy('BROKER_SAFETY_UNSAFE');
    expect(copy).toContain('UNSAFE');
    expect(copy).toContain('paper-only');
  });

  it('returns operator copy for a known local code', () => {
    const copy = disabledReasonCopy('LOCAL_TRANSPORT_STALE');
    expect(copy).toContain('transport');
  });

  it('returns a visibly diagnosable string for an unknown code (run-prompt §9.4)', () => {
    const copy = disabledReasonCopy('SOMETHING_NEW_THE_SERVER_ADDED');
    expect(copy).toBeTruthy();
    // The unknown code is preserved in the rendered string so the
    // operator can search the runbook and the regression is
    // immediately catchable.
    expect(copy).toContain('SOMETHING_NEW_THE_SERVER_ADDED');
  });

  it.each([
    ['toString'],
    ['constructor'],
    ['hasOwnProperty'],
    ['__proto__'],
    ['valueOf'],
  ])(
    'falls back to the unknown-code path for prototype-chain key %s (CR-6)',
    (key) => {
      // The `in` operator would match these on Object.prototype and
      // skip the fallback. Own-property checks must not.
      const copy = disabledReasonCopy(key);
      expect(copy).toBeTruthy();
      expect(copy).toContain(key);
      expect(copy?.toLowerCase()).toContain('unrecognized');
    },
  );
});

describe('actionTooltip composition', () => {
  it('prioritizes local transport-stale over server reason code', () => {
    const tooltip = actionTooltip({
      enabled: false,
      serverReasonCode: 'BROKER_SAFETY_UNSAFE',
      localTransportStale: true,
      busy: false,
      fallbackLabel: 'Resume',
    });
    expect(tooltip).toContain('CONNECTED');
    expect(tooltip).not.toContain('UNSAFE');
  });

  it('prioritizes busy over server reason code', () => {
    const tooltip = actionTooltip({
      enabled: false,
      serverReasonCode: 'BROKER_SAFETY_UNSAFE',
      localTransportStale: false,
      busy: true,
      fallbackLabel: 'Resume',
    });
    expect(tooltip).toContain('pending');
  });

  it('renders server reason copy for disabled-by-server', () => {
    const tooltip = actionTooltip({
      enabled: false,
      serverReasonCode: 'BROKER_SAFETY_UNSAFE',
      localTransportStale: false,
      busy: false,
      fallbackLabel: 'Resume',
    });
    expect(tooltip).toContain('UNSAFE');
  });

  it('falls back to the action label when enabled', () => {
    const tooltip = actionTooltip({
      enabled: true,
      serverReasonCode: null,
      localTransportStale: false,
      busy: false,
      fallbackLabel: 'Resume',
    });
    expect(tooltip).toBe('Resume');
  });

  it('falls back to the action label when disabled with no reason code (data-contract gap)', () => {
    const tooltip = actionTooltip({
      enabled: false,
      serverReasonCode: null,
      localTransportStale: false,
      busy: false,
      fallbackLabel: 'Resume',
    });
    expect(tooltip).toBe('Resume');
  });
});
