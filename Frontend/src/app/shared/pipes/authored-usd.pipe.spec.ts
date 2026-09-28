import { describe, expect, it } from 'vitest';

import { AuthoredUsdPipe } from './authored-usd.pipe';

describe('AuthoredUsdPipe', () => {
  const pipe = new AuthoredUsdPipe();

  it('renders an authored whole-cent string with grouping, digits unchanged', () => {
    expect(pipe.transform('1234.56')).toBe('$1,234.56');
    expect(pipe.transform('98329.57')).toBe('$98,329.57');
    expect(pipe.transform('100000.00')).toBe('$100,000.00');
    expect(pipe.transform('0.00')).toBe('$0.00');
    expect(pipe.transform('0.01')).toBe('$0.01');
  });

  it('keeps an authored sign ahead of the symbol', () => {
    expect(pipe.transform('-5.01')).toBe('-$5.01');
  });

  it('pads a typed budget amount to whole cents without parsing it', () => {
    expect(pipe.transform('800')).toBe('$800.00');
    expect(pipe.transform('800.5')).toBe('$800.50');
  });

  it('passes a string that is not an authored amount through untouched', () => {
    expect(pipe.transform('12,345.67')).toBe('12,345.67');
    expect(pipe.transform('awaiting a reading')).toBe('awaiting a reading');
  });

  it('renders nothing for a value that was never authored', () => {
    expect(pipe.transform(null)).toBeNull();
    expect(pipe.transform(undefined)).toBeNull();
  });
});
