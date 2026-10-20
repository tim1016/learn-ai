import { describe, expect, it } from 'vitest';
import { StrategySpec } from '../../graphql/spec-strategy.models';
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

  it.each<[string, string, StrategySpec, string]>([
    [
      'dangling indicator refs in entry conditions',
      'error',
      {
        ...VALID_SPEC,
        entry: {
          ...VALID_SPEC.entry,
          conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'nonexistent', direction: 'up' }],
        },
      },
      '"nonexistent"',
    ],
    [
      'unknown references nested inside DifferenceBps operands',
      'error',
      {
        ...VALID_SPEC,
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            {
              kind: 'IndicatorComparison',
              left: {
                kind: 'DifferenceBps',
                left: { kind: 'IndicatorRef', indicator: 'ema5' },
                right: { kind: 'IndicatorRef', indicator: 'ghost' },
              },
              op: '>=',
              right: { kind: 'Const', value: 4 },
            },
          ],
        },
      },
      '"ghost"',
    ],
    [
      'identical DifferenceBps operands',
      'error',
      {
        ...VALID_SPEC,
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            {
              kind: 'IndicatorComparison',
              left: {
                kind: 'DifferenceBps',
                left: { kind: 'IndicatorRef', indicator: 'ema5' },
                right: { kind: 'IndicatorRef', indicator: 'ema5' },
              },
              op: '>=',
              right: { kind: 'Const', value: 4 },
            },
          ],
        },
      },
      'same expression',
    ],
    [
      'unknown indicators inside nested LogicNode groups',
      'error',
      {
        ...VALID_SPEC,
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            {
              logic: 'OR',
              conditions: [
                { kind: 'FreshCross', left: 'ema5', right: 'ghost', direction: 'up' },
              ],
            },
          ],
        },
      },
      '"ghost"',
    ],
    [
      'unused indicators',
      'info',
      {
        ...VALID_SPEC,
        indicators: [
          ...VALID_SPEC.indicators,
          { id: 'rsi_unused', kind: 'RSI', period: 14 },
        ],
      },
      'rsi_unused',
    ],
    [
      'an exit and a manage block that are both empty',
      'warn',
      {
        ...VALID_SPEC,
        exit: { logic: 'OR', conditions: [] },
        survival: [],
      },
      'never sells until the run window ends',
    ],
  ])('flags %s with severity %s', (_name, sev, spec, text) => {
    const issues = validateStrategy(spec);
    expect(
      issues.some((i) => i.sev === sev && i.text.includes(text)),
    ).toBe(true);
  });

  it.each<[string, StrategySpec, { start: string; end: string } | null, string]>([
    [
      'duplicate indicator ids',
      {
        ...VALID_SPEC,
        indicators: [
          { id: 'ema', kind: 'EMA', period: 5 },
          { id: 'ema', kind: 'EMA', period: 10 },
        ],
      },
      null,
      'share the id',
    ],
    [
      'a reversed lo/hi in IndicatorBetween',
      {
        ...VALID_SPEC,
        indicators: [{ id: 'rsi', kind: 'RSI', period: 14 }],
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            { kind: 'IndicatorBetween', indicator: 'rsi', lo: 70, hi: 30, inclusive: true },
          ],
        },
      },
      null,
      'range is reversed',
    ],
    [
      'an empty entry block',
      {
        ...VALID_SPEC,
        entry: { ...VALID_SPEC.entry, conditions: [] },
      },
      null,
      'Entry block has no conditions',
    ],
    [
      'reversed run-config dates',
      VALID_SPEC,
      { start: '2024-12-01', end: '2024-01-01' },
      'End date is before start date',
    ],
    [
      'a reversed range inside nested LogicNode groups',
      {
        ...VALID_SPEC,
        indicators: [{ id: 'rsi', kind: 'RSI', period: 14 }],
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            {
              logic: 'AND',
              conditions: [
                { kind: 'IndicatorBetween', indicator: 'rsi', lo: 70, hi: 30, inclusive: true },
              ],
            },
          ],
        },
      },
      null,
      'range is reversed',
    ],
  ])('flags %s', (_name, spec, runCfg, text) => {
    const issues = validateStrategy(spec, runCfg);
    expect(issues.some((i) => i.text.includes(text))).toBe(true);
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

  it('returns zero errors / warnings for the SPY EMA fixture', () => {
    const issues = validateStrategy(VALID_SPEC);
    const errs = issues.filter((i) => i.sev === 'error');
    expect(errs).toHaveLength(0);
  });

  it('warns on out-of-range RSI bounds inside manage rules', () => {
    const issues = validateStrategy({
      ...VALID_SPEC,
      indicators: [{ id: 'rsi', kind: 'RSI', period: 14 }],
      survival: [
        {
          name: 'rsi exit',
          when: {
            logic: 'AND',
            conditions: [
              { kind: 'IndicatorBetween', indicator: 'rsi', lo: 0, hi: 120 },
            ],
          },
          action: { kind: 'CLOSE_ALL' },
        },
      ],
    });
    expect(
      issues.some(
        (i) => i.sev === 'warn' && i.text.includes('RSI range') && i.text.includes('120'),
      ),
    ).toBe(true);
  });
});

describe('collectIndicatorReferences', () => {
  it.each<[string, StrategySpec, string, string]>([
    [
      'IndicatorComparison Subtract operands',
      {
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
      },
      'ema5',
      'ema10',
    ],
    [
      'IndicatorComparison DifferenceBps operands',
      {
        ...VALID_SPEC,
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            {
              kind: 'IndicatorComparison',
              left: {
                kind: 'DifferenceBps',
                left: { kind: 'IndicatorRef', indicator: 'ema5' },
                right: { kind: 'IndicatorRef', indicator: 'ema10' },
              },
              op: '>=',
              right: { kind: 'Const', value: 4 },
            },
          ],
        },
      },
      'ema5',
      'ema10',
    ],
  ])('finds refs in %s', (_name, spec, first, second) => {
    const refs = collectIndicatorReferences(spec);
    expect(refs.has(first)).toBe(true);
    expect(refs.has(second)).toBe(true);
  });

  it.each<[string, StrategySpec, string]>([
    [
      'survival rules',
      {
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
      },
      'rsi_x',
    ],
    [
      'nested LogicNode groups',
      {
        ...VALID_SPEC,
        indicators: [
          ...VALID_SPEC.indicators,
          { id: 'rsi_inner', kind: 'RSI', period: 14 },
        ],
        entry: {
          ...VALID_SPEC.entry,
          conditions: [
            {
              logic: 'OR',
              conditions: [
                { kind: 'IndicatorBetween', indicator: 'rsi_inner', lo: 30, hi: 70 },
              ],
            },
          ],
        },
      },
      'rsi_inner',
    ],
  ])('finds refs in %s', (_name, spec, ref) => {
    const refs = collectIndicatorReferences(spec);
    expect(refs.has(ref)).toBe(true);
  });
});
