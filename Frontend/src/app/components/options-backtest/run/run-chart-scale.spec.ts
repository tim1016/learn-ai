import { describe, expect, it } from 'vitest';

import { equityCoordinates, filledSharePercent, heatStrengths, scaledBarPercents } from './run-chart-scale';

describe('options run chart projection', () => {
  it('keeps dollar changes visible above JavaScript’s exact integer range', () => {
    expect(equityCoordinates(['9007199254740993.01', '9007199254740993.02'])).toEqual([
      { x: 0, y: 148 }, { x: 1000, y: 12 },
    ]);
    expect(scaledBarPercents(['-9007199254740993.01', '4503599627370496.505', '0'], 50)).toEqual([
      '50.0', '25.0', '0.0',
    ]);
  });

  it('projects only display strength and participation from exact source values', () => {
    expect(heatStrengths(['-10', '5', '0'])).toEqual(['0.840', '0.500', '0']);
    expect(filledSharePercent(12, 2)).toBe('85.7');
    expect(filledSharePercent(0, 0)).toBe('0.0');
  });
});
