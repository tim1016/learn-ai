import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import {
  actionOutcomeToast,
  deriveActionRejection,
  extractActionErrorDetail,
} from './panel-action-outcome';

function rejection(detail: Record<string, unknown>): HttpErrorResponse {
  return new HttpErrorResponse({ error: { detail } });
}

describe('extractActionErrorDetail', () => {
  it('reads the typed detail from an HttpErrorResponse', () => {
    const error = rejection({ outcome: 'failure', message: 'boom' });

    expect(extractActionErrorDetail(error)).toEqual({ outcome: 'failure', message: 'boom' });
  });

  it('returns null for a non-HTTP error', () => {
    expect(extractActionErrorDetail(new Error('network down'))).toBeNull();
  });

  it('returns null when the body has no detail object', () => {
    expect(extractActionErrorDetail(new HttpErrorResponse({ error: { detail: 'plain string' } }))).toBeNull();
  });
});

describe('deriveActionRejection', () => {
  it('renders backend-authored why as-is when present', () => {
    const error = rejection({
      outcome: 'conflict',
      message: 'Resume is no longer available for this bot.',
      why: 'The Clerk evidence changed before activation.',
      reason_code: 'TERMINAL_EVIDENCE_UNREADABLE',
    });

    const rejectionResult = deriveActionRejection(error, 'fallback');

    expect(rejectionResult).toEqual({
      outcome: 'conflict',
      message: 'Resume is no longer available for this bot.',
      why: 'The Clerk evidence changed before activation.',
      reasonCode: 'TERMINAL_EVIDENCE_UNREADABLE',
    });
  });

  it('falls back to a receiptLabel-formatted reason_code when why is absent', () => {
    const error = rejection({
      outcome: 'failure',
      message: 'Resume is no longer available for this bot.',
      why: null,
      reason_code: 'TERMINAL_EVIDENCE_UNREADABLE',
    });

    expect(deriveActionRejection(error, 'fallback').why).toBe('Terminal Evidence Unreadable');
  });

  it('is null when neither why, reason, nor reason_code is present', () => {
    const error = rejection({ outcome: 'unknown', message: 'boom', why: null, reason_code: null });

    expect(deriveActionRejection(error, 'fallback').why).toBeNull();
  });

  it('still falls back to the older receiptLabel-formatted reason field', () => {
    // A distinct backend error shape (historical-recovery's `{reason, message}`)
    // shares this parser and has no `why` or `reason_code` field at all.
    const error = rejection({ reason: 'stale_action_token', message: 'boom' });

    expect(deriveActionRejection(error, 'fallback').why).toBe('Stale Action Token');
  });

  it('exposes the raw, unformatted reason code so a caller can branch on the refusal kind', () => {
    const error = rejection({
      reason: 'clerk_binding_generation_conflict',
      message: 'Expected 3 is not 4.',
    });

    expect(deriveActionRejection(error, 'fallback').reasonCode).toBe(
      'clerk_binding_generation_conflict',
    );
  });

  it('falls back to the typed reason_code for the raw reasonCode when reason is absent', () => {
    const error = rejection({ outcome: 'failure', message: 'boom', reason_code: 'CLERK_UNREACHABLE' });

    expect(deriveActionRejection(error, 'fallback').reasonCode).toBe('CLERK_UNREACHABLE');
  });

  it('reasonCode is null when neither reason nor reason_code is present', () => {
    const error = rejection({ outcome: 'unknown', message: 'boom' });

    expect(deriveActionRejection(error, 'fallback').reasonCode).toBeNull();
  });

  it('defaults outcome to unknown and uses the fallback message for a non-HTTP error', () => {
    const rejectionResult = deriveActionRejection(new Error('network down'), 'fallback message');

    expect(rejectionResult.outcome).toBe('unknown');
    expect(rejectionResult.message).toBe('network down');
    expect(rejectionResult.why).toBeNull();
  });

  it('uses the fallback message when the error carries neither a message field nor an Error', () => {
    expect(deriveActionRejection('not an error', 'fallback message').message).toBe('fallback message');
  });
});

describe('fleet refusals (flat body, no `detail` envelope)', () => {
  // The broker clerk fleet returns `{reason, message, next_step}` directly:
  // `FleetControlError.detail()` builds the body rather than nesting it. The
  // parser used to require the nested shape, so every typed fleet refusal
  // reached the operator as generic failure prose instead.
  const fleetRefusal = new HttpErrorResponse({
    status: 409,
    error: {
      reason: 'binding_generation_stale',
      message: 'The lane was re-bound after this command was prepared.',
      next_step: 'Reload the lane and re-issue the command.',
    },
  });

  it('extracts the flat body', () => {
    expect(extractActionErrorDetail(fleetRefusal)).toEqual({
      reason: 'binding_generation_stale',
      message: 'The lane was re-bound after this command was prepared.',
      next_step: 'Reload the lane and re-issue the command.',
    });
  });

  it('renders the refusal message rather than the caller fallback', () => {
    const rejection = deriveActionRejection(fleetRefusal, 'Something went wrong.');

    expect(rejection.message).toBe('The lane was re-bound after this command was prepared.');
    expect(rejection.message).not.toBe('Something went wrong.');
  });

  it('uses `next_step` as remediation prose, not a formatted reason code', () => {
    const rejection = deriveActionRejection(fleetRefusal, 'Something went wrong.');

    // Backend-authored prose is rendered as-is; the raw code is only a
    // last resort when the backend sent no prose at all.
    expect(rejection.why).toBe('Reload the lane and re-issue the command.');
  });

  it('falls back to the vocabulary next-step for a known code with no prose', () => {
    const terse = new HttpErrorResponse({
      status: 409,
      error: { reason: 'clerk_unreachable', message: 'The clerk did not answer.' },
    });

    // `clerk_unreachable` is in the closed vocabulary (#2067), so this reads
    // real remediation prose, not a formatted reason code.
    expect(deriveActionRejection(terse, 'fallback').why).toBe("Retry once the clerk's agent reconnects.");
  });

  it('still falls back to a formatted reason code for one outside the closed vocabulary', () => {
    const terse = new HttpErrorResponse({
      status: 409,
      error: { reason: 'made_up_reason_never_declared', message: 'boom' },
    });

    expect(deriveActionRejection(terse, 'fallback').why).toBe('Made Up Reason Never Declared');
  });
});

describe('fleet refusal vocabulary fallback (#2067, #2102)', () => {
  // The fleet's flat body never carries an `outcome` field, so before this
  // fallback every fleet refusal — a fence's own #2068 rejection included —
  // rendered as the literal word "Unknown". A known code must now read
  // `conflict` for a 409 and `failure` for anything else; an unrecognized
  // code must still fall back to `unknown` rather than a plausible-looking
  // wrong guess.
  it('derives conflict for a known 409 fleet reason with no outcome field', () => {
    const error = new HttpErrorResponse({
      status: 409,
      error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
    });

    const rejection = deriveActionRejection(error, 'fallback');

    expect(rejection.outcome).toBe('conflict');
    expect(rejection.why).toBe("Refresh the lane's current binding generation, then re-prepare the command.");
  });

  it('derives failure for a known non-409 fleet reason with no outcome field', () => {
    const error = new HttpErrorResponse({
      status: 503,
      error: { reason: 'fleet_registry_unavailable', message: 'The registry could not be opened.' },
    });

    expect(deriveActionRejection(error, 'fallback').outcome).toBe('failure');
  });

  it('leaves outcome as unknown for a reason code outside the closed vocabulary', () => {
    const error = new HttpErrorResponse({
      status: 409,
      error: { reason: 'made_up_reason_never_declared', message: 'boom' },
    });

    expect(deriveActionRejection(error, 'fallback').outcome).toBe('unknown');
  });

  it('never lets a known reason override an outcome the backend did supply', () => {
    // The `/actions` route's typed contract does carry `outcome`; a fleet
    // reason code colliding with that field would be a bug in the reverse
    // direction — the backend's explicit outcome must still win.
    const error = new HttpErrorResponse({
      status: 409,
      error: {
        outcome: 'failure',
        reason: 'clerk_binding_generation_conflict',
        message: 'Expected 3 is not 4.',
      },
    });

    expect(deriveActionRejection(error, 'fallback').outcome).toBe('failure');
  });
});

describe('nested legacy bodies that carry next_step', () => {
  // `next_step` is read for BOTH shapes, so this also changes existing nested
  // emitters — `_historical_recovery_refusal` and the coverage-proof refusals
  // in `alpaca_clerk_sqlite.py`. They previously rendered a formatted reason
  // code as remediation; they now render the server's own prose, which is what
  // CLAUDE.md requires of backend-authored operator copy.
  it('prefers server prose over a formatted reason code', () => {
    const legacy = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason: 'coverage_proof_insufficient',
          message: 'The recovery evidence does not cover this run.',
          next_step: 'Keep new exposure blocked and refresh the recovery evidence.',
        },
      },
    });

    const rejection = deriveActionRejection(legacy, 'fallback');

    expect(rejection.why).toBe('Keep new exposure blocked and refresh the recovery evidence.');
    expect(rejection.why).not.toBe('Coverage Proof Insufficient');
  });

  it('still formats the reason code when a nested body carries no prose', () => {
    const terse = new HttpErrorResponse({
      status: 409,
      error: { detail: { reason: 'coverage_proof_insufficient', message: 'Not covered.' } },
    });

    expect(deriveActionRejection(terse, 'fallback').why).toBe('Coverage Proof Insufficient');
  });
});

describe('actionOutcomeToast', () => {
  it('appends why to the detail when present', () => {
    const toast = actionOutcomeToast('failure', 'Resume failed.', 'Refresh and try again.');

    expect(toast.detail).toBe('Resume failed. Refresh and try again.');
    expect(toast.severity).toBe('error');
  });

  it('uses only the message when why is null', () => {
    expect(actionOutcomeToast('success', 'Bot resumed.').detail).toBe('Bot resumed.');
  });
});
