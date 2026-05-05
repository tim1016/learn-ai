import { describe, expect, it } from 'vitest';
import { StrategySpec } from '../../graphql/spec-strategy-types';
import { collectIndicatorReferences, validateStrategy } from './validation';

const VALID_SPEC: StrategySpec = {
  schema_version: '1.0',
  name: 'test',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [
    { id: 'ema5', kind: 'EMA', period: 5 },
    { id: 'ema10', kind: 'EMA', period: 10 },
  ],
  entry: {
    logic: 'AND',
    size: { kind: 'SetHoldings', fraction: 1 },
    conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' }],
  },
  exit: {
    logic: 'OR',
    conditions: [{ kind: 'BarsSinceEntry', op: '>=', value: 5 }],
  },
};

describe('validateStrategy', () => {
  it('flags an empty indicator list as a hard error', () => {
    const issues = validateStrategy({ ...VALID_SPEC, indicators: [] });
    const errs = issues.filter((i) => i.sev === 'error').map((i) => i.text);
    expect(errs).toContain('No indicators defined.');
  });

  it('flags duplicate indicator ids as errors', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      indicators: [
        { id: 'ema', kind: 'EMA', period: 5 },
        { id: 'ema', kind: 'EMA', period: 10 },
      ],
    });
    expect(issues.some((i) => i.text.includes('share the id'))).toBe(true);
  });

  it('flags dangling indicator refs in entry conditions', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      entry: {
        ...VALID_SPEC.entry,
        conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'nonexistent', direction: 'up' }],
      },
    });
    expect(
      issues.some((i) => i.sev === 'error' && i.text.includes('"nonexistent"')),
    ).toBe(true);
  });

  it('flags reversed lo/hi in IndicatorBetween', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      indicators: [{ id: 'rsi', kind: 'RSI', period: 14 }],
      entry: {
        ...VALID_SPEC.entry,
        conditions: [
          { kind: 'IndicatorBetween', indicator: 'rsi', lo: 70, hi: 30, inclusive: true },
        ],
      },
    });
    expect(issues.some((i) => i.text.includes('range is reversed'))).toBe(true);
  });

  it('flags an empty entry block as an error', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      entry: { ...VALID_SPEC.entry, conditions: [] },
    });
    expect(issues.some((i) => i.text.includes('Entry block has no conditions'))).toBe(true);
  });

  it('warns on EMA fast/slow ordering when fast is not actually faster', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      // Cross "ema10 over ema5" — the "fast" line is the slower one.
      entry: {
        ...VALID_SPEC.entry,
        conditions: [{ kind: 'FreshCross', left: 'ema10', right: 'ema5', direction: 'up' }],
      },
    });
    const warns = issues.filter((i) => i.sev === 'warn').map((i) => i.text);
    expect(warns.some((t) => t.includes("fast") && t.includes("isn't faster"))).toBe(true);
  });

  it('warns when both exit and manage are empty', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      exit: { logic: 'OR', conditions: [] },
      survival: [],
    });
    expect(
      issues.some(
        (i) => i.sev === 'warn' && i.text.includes('never sells until the run window ends'),
      ),
    ).toBe(true);
  });

  it('flags unused indicators as info', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      indicators: [
        ...VALID_SPEC.indicators,
        { id: 'rsi_unused', kind: 'RSI', period: 14 },
      ],
    });
    expect(issues.some((i) => i.sev === 'info' && i.text.includes('rsi_unused'))).toBe(true);
  });

  it('flags reversed run-config dates as errors', () => {
    const issues = validateStrategy(VALID_SPEC, {
      start: '2024-12-01',
      end: '2024-01-01',
    });
    expect(issues.some((i) => i.text.includes('End date is before start date'))).toBe(true);
  });

  it('returns zero errors / warnings for the SPY EMA fixture', () => {
    const issues = validateStrategy(VALID_SPEC);
    const errs = issues.filter((i) => i.sev === 'error');
    expect(errs).toHaveLength(0);
  });
});

describe('collectIndicatorReferences', () => {
  it('finds refs in IndicatorComparison Subtract operands', () => {
    const refs = collectIndicatorReferences({
      ...VALID_SPEC,
      entry: {
        ...VALID_SPEC.entry,
        conditions: [
          {
            kind: 'IndicatorComparison',
            left: {
              kind: 'Subtract',
              left: { kind: 'IndicatorRef', indicator: 'ema5' },
              right: { kind: 'IndicatorRef', indicator: 'ema10' },
            },
            op: '>=',
            right: { kind: 'Const', value: 0.2 },
          },
        ],
      },
    });
    expect(refs.has('ema5')).toBe(true);
    expect(refs.has('ema10')).toBe(true);
  });

  it('finds refs in survival rules', () => {
    const refs = collectIndicatorReferences({
      ...VALID_SPEC,
      survival: [
        {
          name: 'rsi cap',
          when: {
            logic: 'AND',
            conditions: [{ kind: 'IndicatorBetween', indicator: 'rsi_x', lo: 0, hi: 100 }],
          },
          action: { kind: 'CLOSE_ALL' },
        },
      ],
    });
    expect(refs.has('rsi_x')).toBe(true);
  });
});
