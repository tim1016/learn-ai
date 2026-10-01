import { describe, expect, it } from 'vitest';
import { describeError, lookupErrorEntry, resolveErrorCode } from './error-catalog';
import { GraphqlError } from '../graphql/graphql-error';

describe('lookupErrorEntry', () => {
  it('returns the catalog entry for a known code', () => {
    const entry = lookupErrorEntry('BROKER_DISCONNECTED');
    expect(entry.what).toContain('IB Gateway');
    expect(entry.tryCopy).toContain('Retry');
  });

  it('returns the fallback entry for an unknown code', () => {
    const entry = lookupErrorEntry('DEFINITELY_NOT_A_CODE');
    expect(entry.what).toBe('Something went wrong.');
  });

  it('treats null / undefined as unknown', () => {
    expect(lookupErrorEntry(undefined).what).toBe('Something went wrong.');
    expect(lookupErrorEntry(null).what).toBe('Something went wrong.');
  });
});

describe('resolveErrorCode', () => {
  it('reads extensions.code from a GraphqlError payload', () => {
    const err = new GraphqlError([
      { message: 'no gateway', extensions: { code: 'BROKER_DISCONNECTED' } },
    ]);
    expect(resolveErrorCode(err)).toBe('BROKER_DISCONNECTED');
  });

  it('returns undefined when extensions.code is absent', () => {
    const err = new GraphqlError([{ message: 'mystery' }]);
    expect(resolveErrorCode(err)).toBeUndefined();
  });

  it('returns undefined for non-GraphqlError values', () => {
    expect(resolveErrorCode(new Error('boom'))).toBeUndefined();
    expect(resolveErrorCode('boom')).toBeUndefined();
    expect(resolveErrorCode(null)).toBeUndefined();
  });
});

describe('describeError', () => {
  it('uses catalog copy when the code is mapped', () => {
    const err = new GraphqlError([
      { message: 'no gateway', extensions: { code: 'BROKER_DISCONNECTED' } },
    ]);
    const display = describeError(err);
    expect(display.what).toContain('IB Gateway');
    expect(display.tryCopy).toContain('Retry');
  });

  it('falls back to the error message when no code is present', () => {
    const err = new Error('Network broken');
    const display = describeError(err);
    expect(display.what).toBe('Network broken');
  });

  it('prefers an explicit contextWhat over the raw message', () => {
    const err = new Error('ECONNREFUSED');
    const display = describeError(err, 'We could not reach the broker.');
    expect(display.what).toBe('We could not reach the broker.');
  });
});
