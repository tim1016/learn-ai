import { describe, expect, it } from 'vitest';
import { describeError, lookupErrorEntry, resolveErrorCode, resolveMathRef } from './error-catalog';
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

describe('resolveMathRef', () => {
  it('reads extensions.mathRef when present', () => {
    const err = new GraphqlError([
      {
        message: 'greeks diverged',
        extensions: { code: 'NUMERIC_DIVERGENCE', mathRef: '/docs/math-sources-of-truth.md' },
      },
    ]);
    expect(resolveMathRef(err)).toBe('/docs/math-sources-of-truth.md');
  });

  it('returns undefined when no mathRef is attached', () => {
    const err = new GraphqlError([
      { message: 'x', extensions: { code: 'BROKER_DISCONNECTED' } },
    ]);
    expect(resolveMathRef(err)).toBeUndefined();
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

  it('passes through the math deep-link when supplied', () => {
    const err = new GraphqlError([
      {
        message: 'greeks diverged',
        extensions: { code: 'NUMERIC_DIVERGENCE', mathRef: '/docs/math.md' },
      },
    ]);
    expect(describeError(err).mathRef).toBe('/docs/math.md');
  });
});
