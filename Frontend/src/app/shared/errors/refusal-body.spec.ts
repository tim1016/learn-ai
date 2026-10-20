import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import { refusalBody } from './refusal-body';

function httpError(body: unknown, status = 409): HttpErrorResponse {
  return new HttpErrorResponse({ error: body, status });
}

describe('refusalBody', () => {
  const flat = {
    reason: 'clerk_unreachable',
    message: 'The clerk did not answer.',
    next_step: 'Check the lane and retry.',
  };
  const nested = { reason: 'stale_revision', message: 'Someone else applied first.' };
  const both = { reason: 'nested_wins', message: 'nested' };

  it.each([
    ['the flat body the fleet coordinator returns', flat, flat],
    ['the nested body a FastAPI HTTPException returns', { detail: nested }, nested],
    ['the explicit envelope, preferred when a body carries both', { detail: both, message: 'flat' }, both],
    ['a flat body carrying only a message', { message: 'It failed.' }, { message: 'It failed.' }],
  ])('reads %s', (_name, body, expected) => {
    expect(refusalBody(httpError(body))).toEqual(expected);
  });

  it.each([
    ['a body that is not a refusal', httpError({ items: [1, 2, 3] })],
    ['a string body, as a non-object body', httpError('gateway timeout')],
    ['a null body, as a non-object body', httpError(null)],
    ['an array body, rather than mining it by index', httpError([{ reason: 'nope' }])],
    // A 422 body is `{detail: [{loc, msg, type}, ...]}`. `typeof [] === 'object'`,
    // so a naive nested check hands the caller an array and it reads
    // `.reason`/`.message` off it as undefined. There is no operator-facing
    // refusal here, so the honest answer is null.
    [
      "FastAPI's validation envelope, whose `detail` is a list",
      httpError({ detail: [{ loc: ['body', 'symbol'], msg: 'field required', type: 'value_error.missing' }] }, 422),
    ],
    ['an Error, as anything that is not an HttpErrorResponse', new Error('boom')],
    ['undefined, as anything that is not an HttpErrorResponse', undefined],
  ])('returns null for %s', (_name, error) => {
    expect(refusalBody(error)).toBeNull();
  });
});
