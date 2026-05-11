/* Named multi-instance presets surfaced above the category list in the picker.
 * Clicking a preset emits one (addInstance) event per instance. */

export interface PresetInstance {
  indicator: string;
  params: Record<string, number>;
}

export interface IndicatorPreset {
  name: string;
  subtitle: string;
  stack: 's4' | 's3' | 's2-combo' | 's2-trend';
  count: string;
  instances: PresetInstance[];
}

export const INDICATOR_PRESETS: readonly IndicatorPreset[] = [
  {
    name: 'EMA ribbon',
    subtitle: '5 / 10 / 20 / 50',
    stack: 's4',
    count: '×4',
    instances: [
      { indicator: 'ema', params: { length: 5 } },
      { indicator: 'ema', params: { length: 10 } },
      { indicator: 'ema', params: { length: 20 } },
      { indicator: 'ema', params: { length: 50 } },
    ],
  },
  {
    name: 'Bollinger triple',
    subtitle: '10 / 20 / 50',
    stack: 's3',
    count: '×3',
    instances: [
      { indicator: 'bbands', params: { length: 10 } },
      { indicator: 'bbands', params: { length: 20 } },
      { indicator: 'bbands', params: { length: 50 } },
    ],
  },
  {
    name: 'RSI + MACD',
    subtitle: 'momentum combo',
    stack: 's2-combo',
    count: '×2',
    instances: [
      { indicator: 'rsi', params: {} },
      { indicator: 'macd', params: {} },
    ],
  },
  {
    name: 'ATR + ADX',
    subtitle: 'trend strength',
    stack: 's2-trend',
    count: '×2',
    instances: [
      { indicator: 'atr', params: {} },
      { indicator: 'adx', params: {} },
    ],
  },
];
