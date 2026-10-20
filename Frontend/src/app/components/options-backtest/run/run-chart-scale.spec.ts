import { describe, expect, it } from 'vitest';

import { equityCoordinates, filledSharePercent, heatStrengths, scaledBarPercents } from './run-chart-scale';

describe('options run chart projection', () => {
  it('keeps dollar changes visible above JavaScript’s exact integer range, and projects only display strength and participation from exact source values', () => {
    expect.soft(equityCoordinates(['9007199254740993.01', '9007199254740993.02']), 'keeps dollar changes visible above JavaScript’s exact integer range: equity coordinates').toEqual([
      { x: 0, y: 148 }, { x: 1000, y: 12 },
    ]);
    expect.soft(scaledBarPercents(['-9007199254740993.01', '4503599627370496.505', '0'], 50), 'keeps dollar changes visible above JavaScript’s exact integer range: scaled bar percents').toEqual([
      '50.0', '25.0', '0.0',
    ]);
    expect.soft(heatStrengths(['-10', '5', '0']), 'projects only display strength from exact source values: heat strengths').toEqual(['0.840', '0.500', '0']);
    expect.soft(filledSharePercent(12, 2), 'projects only participation from exact source values: filled share of 12 and 2').toBe('85.7');
    expect.soft(filledSharePercent(0, 0), 'projects only participation from exact source values: filled share of 0 and 0').toBe('0.0');
  });
});
