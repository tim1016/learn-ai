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
