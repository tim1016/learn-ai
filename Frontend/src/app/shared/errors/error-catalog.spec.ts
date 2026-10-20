import { describe, expect, it } from 'vitest';
import { describeError, lookupErrorEntry, resolveErrorCode } from './error-catalog';
import { GraphqlError } from '../graphql/graphql-error';

describe('lookupErrorEntry', () => {
  it('returns the catalog entry for a known code and the fallback for an unknown one', () => {
    const entry = lookupErrorEntry('BROKER_DISCONNECTED');
    expect.soft(entry.what, 'returns the catalog entry for a known code: what happened').toContain('IB Gateway');
    expect.soft(entry.tryCopy, 'returns the catalog entry for a known code: what to try').toContain('Retry');

    const unknown = lookupErrorEntry('DEFINITELY_NOT_A_CODE');
    expect.soft(unknown.what, 'returns the fallback entry for an unknown code').toBe('Something went wrong.');

    expect.soft(lookupErrorEntry(undefined).what, 'treats undefined as unknown').toBe('Something went wrong.');
    expect.soft(lookupErrorEntry(null).what, 'treats null as unknown').toBe('Something went wrong.');
  });
});

describe('resolveErrorCode', () => {
  it('reads extensions.code from a GraphqlError payload and is undefined for anything else', () => {
    const err = new GraphqlError([
      { message: 'no gateway', extensions: { code: 'BROKER_DISCONNECTED' } },
    ]);
    expect.soft(resolveErrorCode(err), 'reads extensions.code from a GraphqlError payload').toBe('BROKER_DISCONNECTED');

    const mystery = new GraphqlError([{ message: 'mystery' }]);
    expect.soft(resolveErrorCode(mystery), 'returns undefined when extensions.code is absent').toBeUndefined();

    expect.soft(resolveErrorCode(new Error('boom')), 'returns undefined for non-GraphqlError values: an Error').toBeUndefined();
    expect.soft(resolveErrorCode('boom'), 'returns undefined for non-GraphqlError values: a string').toBeUndefined();
    expect.soft(resolveErrorCode(null), 'returns undefined for non-GraphqlError values: null').toBeUndefined();
  });
});

describe('describeError', () => {
  it('describes an error by catalog copy, else by its message, with an explicit contextWhat first', () => {
    const mapped = describeError(
      new GraphqlError([{ message: 'no gateway', extensions: { code: 'BROKER_DISCONNECTED' } }]),
    );
    expect.soft(mapped.what, 'uses catalog copy when the code is mapped: what happened').toContain('IB Gateway');
    expect.soft(mapped.tryCopy, 'uses catalog copy when the code is mapped: what to try').toContain('Retry');

    const unmapped = describeError(new Error('Network broken'));
    expect.soft(unmapped.what, 'falls back to the error message when no code is present').toBe('Network broken');

    const withContext = describeError(new Error('ECONNREFUSED'), 'We could not reach the broker.');
    expect.soft(withContext.what, 'prefers an explicit contextWhat over the raw message').toBe('We could not reach the broker.');
  });
});
