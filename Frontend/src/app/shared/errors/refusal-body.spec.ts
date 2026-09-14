import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import { refusalBody } from './refusal-body';

function httpError(body: unknown, status = 409): HttpErrorResponse {
  return new HttpErrorResponse({ error: body, status });
}

describe('refusalBody', () => {
  it('reads the flat body the fleet coordinator returns', () => {
    const body = {
      reason: 'clerk_unreachable',
      message: 'The clerk did not answer.',
      next_step: 'Check the lane and retry.',
    };

    expect(refusalBody(httpError(body))).toEqual(body);
  });

  it('reads the nested body a FastAPI HTTPException returns', () => {
    const detail = { reason: 'stale_revision', message: 'Someone else applied first.' };

    expect(refusalBody(httpError({ detail }))).toEqual(detail);
  });

  it('prefers the explicit envelope when a body carries both', () => {
    const detail = { reason: 'nested_wins', message: 'nested' };

    expect(refusalBody(httpError({ detail, message: 'flat' }))).toEqual(detail);
  });

  it('accepts a flat body carrying only a message', () => {
    expect(refusalBody(httpError({ message: 'It failed.' }))).toEqual({ message: 'It failed.' });
  });

  it('returns null for a body that is not a refusal', () => {
    expect(refusalBody(httpError({ items: [1, 2, 3] }))).toBeNull();
  });

  it('returns null for a non-object body', () => {
    expect(refusalBody(httpError('gateway timeout'))).toBeNull();
    expect(refusalBody(httpError(null))).toBeNull();
  });

  it('returns null for an array body rather than mining it by index', () => {
    expect(refusalBody(httpError([{ reason: 'nope' }]))).toBeNull();
  });

  it('returns null for anything that is not an HttpErrorResponse', () => {
    expect(refusalBody(new Error('boom'))).toBeNull();
    expect(refusalBody(undefined)).toBeNull();
  });
});
