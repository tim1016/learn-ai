// Parity test for the operator-language copy map against the
// server-authored closed reason-code vocabulary.
//
// The Python source of truth lives in:
//   PythonDataService/app/services/operator_capability.py → REASON_CODES
//   PythonDataService/app/services/resume_guard_state.py → RESUME_REASON_CODES
//
// This spec pins the union here so that adding a new code on the
// server fails this Vitest immediately — no silent gap where the
// cockpit renders the raw enum.

import { describe, expect, it } from 'vitest';

import {
  ALL_OPERATOR_REASON_CODES,
  ALL_LOCAL_REASON_CODES,
  actionTooltip,
  disabledReasonCopy,
  type OperatorReasonCode,
} from './disabled-reason-copy';

const EXPECTED_OPERATOR_REASON_CODES: ReadonlySet<OperatorReasonCode> = new Set<OperatorReasonCode>([
  // operator_capability.py REASON_CODES (action-conflict matrix + live-binding + transport)
  'MUTATION_UNRESOLVED_START',
  'MUTATION_UNRESOLVED_STOP',
  'MUTATION_UNRESOLVED_FLATTEN',
  'MUTATION_UNRESOLVED_RESUME',
  'OUTCOME_UNKNOWN',
  'NO_LIVE_BINDING',
  'NO_OWNED_POSITIONS',
  'ALREADY_POISONED',
  'ALREADY_STOPPED',
  'POSTURE_DEMOTED',
  // resume_guard_state.py RESUME_REASON_CODES
  'BROKER_SAFETY_UNSAFE',
  'BROKER_SAFETY_UNKNOWN',
  'SUBMISSION_CAPABILITY_BLOCKED',
  'SUBMISSION_CAPABILITY_UNKNOWN',
  'RECONCILIATION_FAILED',
  'RECONCILIATION_STALE',
  'RECONCILIATION_NOT_AVAILABLE',
  'RECONCILIATION_UNKNOWN',
  'UNRESOLVED_UNCERTAIN_INTENT',
  'UNCERTAIN_INTENT_STATE_UNKNOWN',
  'ALREADY_RUNNING',
  'ALREADY_PAUSED',
  'STOPPED_REQUIRES_REDEPLOY',
  'REDEPLOY_REQUIRED',
]);

describe('disabled-reason-copy parity with server closed vocabulary', () => {
  it('covers every code in the expected vocabulary, and no extras', () => {
    const actual = new Set(ALL_OPERATOR_REASON_CODES);
    const expected = new Set(EXPECTED_OPERATOR_REASON_CODES);

    const missing = [...expected].filter((c) => !actual.has(c));
    const extra = [...actual].filter((c) => !expected.has(c));

    expect(missing).toEqual([]);
    expect(extra).toEqual([]);
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
