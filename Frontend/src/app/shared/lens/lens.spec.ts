import { describe, expect, it } from 'vitest';

import { lensFromKey, lensLabel, parseLens } from './lens';

describe('parseLens', () => {
  it.each(['trader', 'operator'] as const)('accepts %s', (value) => {
    expect(parseLens(value)).toBe(value);
  });

  it('recovers to null for an invalid value', () => {
    expect(parseLens('overview')).toBeNull();
    expect(parseLens('OPERATOR')).toBeNull();
    expect(parseLens('')).toBeNull();
    expect(parseLens(null)).toBeNull();
  });
});

describe('lensFromKey', () => {
  it('moves to operator on ArrowRight and End', () => {
    expect(lensFromKey('ArrowRight')).toBe('operator');
    expect(lensFromKey('End')).toBe('operator');
  });

  it('moves to trader on ArrowLeft and Home', () => {
    expect(lensFromKey('ArrowLeft')).toBe('trader');
    expect(lensFromKey('Home')).toBe('trader');
  });

  it('ignores keys that are not lens transitions', () => {
    expect(lensFromKey('ArrowDown')).toBeNull();
    expect(lensFromKey('Enter')).toBeNull();
    expect(lensFromKey(' ')).toBeNull();
  });
});

describe('lensLabel', () => {
  it('labels both lenses with their user-facing names', () => {
    expect(lensLabel('trader')).toBe('Trader');
    expect(lensLabel('operator')).toBe('Operator');
  });
});
