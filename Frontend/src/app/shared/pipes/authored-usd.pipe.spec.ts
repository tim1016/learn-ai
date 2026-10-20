import { describe, expect, it } from 'vitest';

import { AuthoredUsdPipe } from './authored-usd.pipe';

describe('AuthoredUsdPipe', () => {
  const pipe = new AuthoredUsdPipe();

  it('renders an authored amount as dollars, keeps its sign, pads a typed budget, and leaves everything else untouched', () => {
    expect.soft(pipe.transform('1234.56'), 'renders an authored whole-cent string with grouping, digits unchanged: 1234.56').toBe('$1,234.56');
    expect.soft(pipe.transform('98329.57'), 'renders an authored whole-cent string with grouping, digits unchanged: 98329.57').toBe('$98,329.57');
    expect.soft(pipe.transform('100000.00'), 'renders an authored whole-cent string with grouping, digits unchanged: 100000.00').toBe('$100,000.00');
    expect.soft(pipe.transform('0.00'), 'renders an authored whole-cent string with grouping, digits unchanged: 0.00').toBe('$0.00');
    expect.soft(pipe.transform('0.01'), 'renders an authored whole-cent string with grouping, digits unchanged: 0.01').toBe('$0.01');

    expect.soft(pipe.transform('-5.01'), 'keeps an authored sign ahead of the symbol').toBe('-$5.01');

    expect.soft(pipe.transform('800'), 'pads a typed budget amount to whole cents without parsing it: 800').toBe('$800.00');
    expect.soft(pipe.transform('800.5'), 'pads a typed budget amount to whole cents without parsing it: 800.5').toBe('$800.50');

    expect.soft(pipe.transform('12,345.67'), 'passes a string that is not an authored amount through untouched: grouped digits').toBe('12,345.67');
    expect.soft(pipe.transform('awaiting a reading'), 'passes a string that is not an authored amount through untouched: prose').toBe('awaiting a reading');

    expect.soft(pipe.transform(null), 'renders nothing for a value that was never authored: null').toBeNull();
    expect.soft(pipe.transform(undefined), 'renders nothing for a value that was never authored: undefined').toBeNull();
  });
});
