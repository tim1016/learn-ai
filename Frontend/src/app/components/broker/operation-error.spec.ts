import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import {
  describeOperationError,
  extractServerMessage,
  readOutcomeUnknownBody,
  readPreconditionBody,
} from './operation-error';

describe('extractServerMessage', () => {
  it('preserves both legacy FastAPI string details and structured server messages', () => {
    expect(extractServerMessage({ error: { detail: 'Account evidence expired.' } }, 'Fallback.'))
      .toBe('Account evidence expired.');
    expect(extractServerMessage({ error: { detail: { message: 'Fresh proof is required.' } } }, 'Fallback.'))
      .toBe('Fresh proof is required.');
  });

  it('uses the caller-owned fallback for unrecognised error shapes', () => {
    expect(extractServerMessage({ error: { detail: { reason: 'opaque' } } }, 'Retry later.'))
      .toBe('Retry later.');
  });

  // ── #2067 / #2102 — the third parser learns the fleet's flat body ────────
  // `refusalBody` requires a real `HttpErrorResponse` (it checks `instanceof`),
  // unlike the plain object literals above, so these use one — matching what
  // Angular's `HttpClient` actually throws in production.
  it('reads the fleet clerk fleet\'s flat {reason, message, next_step} body', () => {
    const fleetRefusal = new HttpErrorResponse({
      status: 503,
      error: {
        reason: 'clerk_unreachable',
        message: 'The clerk did not answer.',
        next_step: 'Retry once the lane recovers.',
      },
    });

    expect(extractServerMessage(fleetRefusal, 'Fallback.')).toBe('The clerk did not answer.');
  });

  it('still prefers the nested FastAPI envelope over a flat top-level message', () => {
    const nested = new HttpErrorResponse({
      status: 409,
      error: { detail: { message: 'Nested wins.' }, message: 'flat, should be ignored' },
    });

    expect(extractServerMessage(nested, 'Fallback.')).toBe('Nested wins.');
  });

  it('still reads a plain string detail on a real HttpErrorResponse', () => {
    const legacy = new HttpErrorResponse({ status: 404, error: { detail: 'Account evidence expired.' } });

    expect(extractServerMessage(legacy, 'Fallback.')).toBe('Account evidence expired.');
  });
});

describe('describeOperationError', () => {
  it('maps a 409 deploy to a precondition with deploy-specific remediation', () => {
    const e = describeOperationError('deploy', 409, 'Working tree is dirty; commit or stash.');
    expect(e.category).toBe('precondition');
    expect(e.title).toContain('Deploy');
    expect(e.detail).toBe('Working tree is dirty; commit or stash.');
    expect(e.remediation.toLowerCase()).toContain('commit');
  });

  it('maps a 503 to infra with start-specific remediation', () => {
    const e = describeOperationError('start', 503, 'host daemon unreachable');
    expect(e.category).toBe('infra');
    expect(e.remediation.toLowerCase()).toContain('live engine');
  });

  it('maps a 409 command to a no-binding precondition', () => {
    const e = describeOperationError('flatten', 409, 'no live run bound to this instance');
    expect(e.category).toBe('precondition');
    expect(e.remediation.toLowerCase()).toContain('start the instance');
  });

  it('treats a null status as an infra (transport) failure', () => {
    const e = describeOperationError('stop', null, 'connection refused');
    expect(e.category).toBe('infra');
  });

  it('falls back to a generic remediation for an unmapped status', () => {
    const e = describeOperationError('pause', 418, 'teapot');
    expect(e.category).toBe('unknown');
    expect(e.remediation).toBeTruthy();
  });

  it('never derives remediation from the detail string', () => {
    // Two different detail strings on the same (operation, status) yield the
    // SAME remediation — proving the wording is not parsed.
    const a = describeOperationError('deploy', 409, 'dirty tree at PythonDataService');
    const b = describeOperationError('deploy', 409, 'totally different wording here');
    expect(a.remediation).toBe(b.remediation);
  });
});

// `toOperationError` was deleted (#2102): zero production callers remained
// once every caller migrated to `extractServerMessage` /
// `extractServerReasonCode`, and its coverage collapsed to these two parser
// functions it wrapped — both still exported and exercised directly below.
describe('readOutcomeUnknownBody', () => {
  it('rejects outcome-unknown bodies with endpoints outside the closed contract', () => {
    const parsed = readOutcomeUnknownBody({
      detail: {
        outcome: 'UNKNOWN',
        reason_code: 'OUTCOME_UNKNOWN',
        error_category: 'read_timeout',
        detail: 'cancel response lost',
        endpoint: 'cancel_order',
        occurred_at_ms: 1_700_000_000_000,
        runbook_hint: 'Refresh before retrying.',
      },
    });

    expect(parsed).toBeNull();
  });

  it('accepts renew-daemon-lease as one of the closed-contract endpoints', () => {
    const parsed = readOutcomeUnknownBody({
      detail: {
        outcome: 'UNKNOWN',
        reason_code: 'OUTCOME_UNKNOWN',
        error_category: 'read_timeout',
        detail: 'lease response lost',
        endpoint: 'renew_daemon_lease',
        occurred_at_ms: 1_700_000_000_000,
        runbook_hint: 'Refresh Bot Control before retrying.',
      },
    });

    expect(parsed?.endpoint).toBe('renew_daemon_lease');
  });
});

describe('readPreconditionBody', () => {
  it('parses structured deterministic precondition bodies', () => {
    const parsed = readPreconditionBody({
      detail: {
        reason_code: 'STOPPED_REQUIRES_RESUME',
        message: 'DIagVal6 is durably STOPPED.',
        remediation: 'Use Resume to clear the stop latch.',
        gate_id: 'desired_state.start',
      },
    });

    expect(parsed).toEqual({
      reason_code: 'STOPPED_REQUIRES_RESUME',
      message: 'DIagVal6 is durably STOPPED.',
      remediation: 'Use Resume to clear the stop latch.',
      gate_id: 'desired_state.start',
    });
  });

  it('returns null when reason_code or message is missing', () => {
    expect(readPreconditionBody({ detail: { message: 'no reason code' } })).toBeNull();
    expect(readPreconditionBody({ detail: { reason_code: 'X' } })).toBeNull();
  });
});
