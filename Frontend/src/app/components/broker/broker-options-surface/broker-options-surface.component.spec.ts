import { describe, expect, it } from 'vitest';
import { intersectAll, pickStrikesAroundAtm } from './broker-options-surface.component';

describe('pickStrikesAroundAtm', () => {
  it('returns ±band strikes around the closest-to-ATM strike', () => {
    const grid = [400, 405, 410, 415, 420, 425, 430, 435, 440];
    expect(pickStrikesAroundAtm(grid, 420, 2)).toEqual([410, 415, 420, 425, 430]);
  });

  it('clips at the low edge of the grid', () => {
    const grid = [400, 405, 410, 415, 420];
    expect(pickStrikesAroundAtm(grid, 401, 3)).toEqual([400, 405, 410, 415]);
  });

  it('clips at the high edge of the grid', () => {
    const grid = [400, 405, 410, 415, 420];
    expect(pickStrikesAroundAtm(grid, 419, 3)).toEqual([405, 410, 415, 420]);
  });

  it('handles ATM exactly between two strikes by snapping to the nearer of the two (ties = lower)', () => {
    const grid = [400, 405, 410, 415, 420];
    // 407.5 is equidistant from 405 and 410; first-found wins → 405.
    expect(pickStrikesAroundAtm(grid, 407.5, 1)).toEqual([400, 405, 410]);
  });

  it('returns an empty list when given no strikes', () => {
    expect(pickStrikesAroundAtm([], 420, 5)).toEqual([]);
  });
});

describe('intersectAll', () => {
  it('returns the strikes present in every list', () => {
    const result = intersectAll([
      [400, 405, 410, 415],
      [405, 410, 415, 420],
      [400, 410, 415],
    ]).sort((a, b) => a - b);
    expect(result).toEqual([410, 415]);
  });

  it('returns an empty list when no common strike exists', () => {
    const result = intersectAll([
      [400, 405],
      [410, 415],
    ]);
    expect(result).toEqual([]);
  });

  it('returns the single list unchanged when given one input', () => {
    const result = intersectAll([[400, 405, 410]]).sort((a, b) => a - b);
    expect(result).toEqual([400, 405, 410]);
  });

  it('returns an empty list when given no inputs', () => {
    expect(intersectAll([])).toEqual([]);
  });
});
