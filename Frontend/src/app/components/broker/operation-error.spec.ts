import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import { extractServerMessage } from './operation-error';

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
