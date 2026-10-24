import { describe, expect, it } from 'vitest';

import { StoredInputsError } from './stored-inputs';
import { storedFacts } from './stored-study-facts';
import { storedRecord } from './testing/stored-study-fixtures';

describe('storedFacts', () => {
  it.each([
    [
      'feature-study',
      [
        ['Instrument', 'SPY'],
        ['Bars', '5 minute'],
        ['Indicator', 'rsi_14'],
      ],
    ],
    [
      'signal-study',
      [
        ['Instrument', 'SPY'],
        ['Bars', '15 minute'],
        ['Indicator', 'macd_signal'],
        ['Flip sign', 'off'],
        ['Regime gate', 'on'],
      ],
    ],
    [
      'cross-sectional-study',
      [
        ['Universe', 'SPY, QQQ, AAPL'],
        ['Feature', 'iv_rank_60'],
        ['Target', 'directional'],
      ],
    ],
    [
      'indicator-reliability',
      [
        ['Instrument', 'SPY'],
        ['Indicator', 'rsi (length 14)'],
        ['Horizons', '1, 5 bars'],
        ['Slope IC', 'not included'],
      ],
    ],
    [
      'realized-vs-iv',
      [
        ['Instrument', 'SPY'],
        ['Bars', '1d'],
        ['Tenor', '30 days'],
      ],
    ],
  ] as const)('lists what a saved %s run was asked, in its study’s own words', (study, expected) => {
    expect(storedFacts(storedRecord(study)).map(({ label, value }) => [label, value])).toEqual(expected);
  });

  it('refuses a field it cannot read rather than showing a value nobody entered', () => {
    const broken = storedRecord('feature-study', { inputs: { symbol: 'SPY' } as never });

    expect(() => storedFacts(broken)).toThrow(StoredInputsError);
  });
});
