import { describe, expect, it } from 'vitest';
import {
  absDiff,
  deltaAbsBand,
  diffBps,
  fmtBrokerExpiryDate,
} from './format';

describe('fmtBrokerExpiryDate', () => {
  it('returns a dash for absent expiry markers', () => {
    expect(fmtBrokerExpiryDate(null)).toBe('—');
  });
});

describe('absDiff', () => {
  it('returns null when either side is missing', () => {
    expect(absDiff(null, 0.5)).toBeNull();
    expect(absDiff(0.5, null)).toBeNull();
    expect(absDiff(undefined, 0.5)).toBeNull();
    expect(absDiff(null, null)).toBeNull();
  });

  it('returns the absolute distance between two scalars', () => {
    expect(absDiff(0.5, 0.4)).toBeCloseTo(0.1, 12);
    expect(absDiff(0.4, 0.5)).toBeCloseTo(0.1, 12);
    expect(absDiff(-0.5, -0.4)).toBeCloseTo(0.1, 12);
  });

  it('does NOT explode when the reference is near-zero (the regression that motivated it)', () => {
    // Strike 639P regression from the screenshot:
    //   IBKR  Δ = -0.003
    //   Engine Δ ≈ -1e-9 (rendered as -0.000)
    // diffBps would return ~-3,028,671,573 for these inputs; absDiff
    // gives the actual absolute disagreement instead.
    const ibkr = -0.003;
    const engine = -1e-9;
    expect(diffBps(ibkr, engine)).toBeLessThan(-1e9); // pre-fix behaviour
    expect(absDiff(ibkr, engine)).toBeCloseTo(0.003, 8); // post-fix behaviour
  });
});

describe('deltaAbsBand', () => {
  it('returns null when the input is null', () => {
    expect(deltaAbsBand(null)).toBeNull();
  });

  it.each([
    ['green up to 0.005 inclusive', 'green', 0, 0.001, 0.005],
    ['yellow above 0.005 up to 0.02 inclusive', 'yellow', 0.005001, 0.015, 0.02],
    ['red above 0.02', 'red', 0.020001, 0.1, 1.0],
  ] as const)('classifies %s', (_name, band, low, mid, high) => {
    expect(deltaAbsBand(low)).toBe(band);
    expect(deltaAbsBand(mid)).toBe(band);
    expect(deltaAbsBand(high)).toBe(band);
  });

  it('uses absolute magnitude — sign of the diff does not change the band', () => {
    // absDiff returns a non-negative number, but deltaAbsBand should
    // tolerate negative inputs in case a future caller passes a
    // signed diff. Symmetric thresholds guarantee colour stability
    // regardless of which side of the disagreement is larger.
    expect(deltaAbsBand(-0.003)).toBe('green');
    expect(deltaAbsBand(-0.015)).toBe('yellow');
    expect(deltaAbsBand(-0.05)).toBe('red');
  });
});
