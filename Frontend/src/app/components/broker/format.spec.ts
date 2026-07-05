import { describe, expect, it } from 'vitest';
import {
  absDiff,
  deltaAbsBand,
  diffBps,
  fmtBrokerExpiryDate,
  toleranceBand,
} from './format';

describe('fmtBrokerExpiryDate', () => {
  it('preserves midnight-UTC broker expiry markers on their UTC date', () => {
    expect(fmtBrokerExpiryDate(Date.UTC(2025, 11, 19, 0, 0, 0))).toBe('2025-12-19');
  });

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

  it('classifies green up to 0.005 inclusive', () => {
    expect(deltaAbsBand(0)).toBe('green');
    expect(deltaAbsBand(0.001)).toBe('green');
    expect(deltaAbsBand(0.005)).toBe('green');
  });

  it('classifies yellow above 0.005 up to 0.02 inclusive', () => {
    expect(deltaAbsBand(0.005001)).toBe('yellow');
    expect(deltaAbsBand(0.015)).toBe('yellow');
    expect(deltaAbsBand(0.02)).toBe('yellow');
  });

  it('classifies red above 0.02', () => {
    expect(deltaAbsBand(0.020001)).toBe('red');
    expect(deltaAbsBand(0.1)).toBe('red');
    expect(deltaAbsBand(1.0)).toBe('red');
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

describe('diffBps and toleranceBand (kept for unbounded scalars)', () => {
  it('toleranceBand still uses absolute magnitude consistently', () => {
    expect(toleranceBand(50)).toBe('green');
    expect(toleranceBand(-50)).toBe('green');
    expect(toleranceBand(200)).toBe('yellow');
    expect(toleranceBand(-200)).toBe('yellow');
    expect(toleranceBand(201)).toBe('red');
    expect(toleranceBand(-201)).toBe('red');
  });

  it('diffBps still returns null on a zero reference', () => {
    expect(diffBps(0.1, 0)).toBeNull();
  });
});
