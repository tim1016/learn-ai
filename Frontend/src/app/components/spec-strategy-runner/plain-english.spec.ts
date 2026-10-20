import { describe, expect, it } from 'vitest';
import { IndicatorBlock, StrategySpec } from '../../graphql/spec-strategy.models';
import {
  buildSummaryFragments,
  formatCondition,
  formatEntryBlock,
  formatExitBlock,
  formatSurvivalRule,
} from './plain-english';

const INDS: readonly IndicatorBlock[] = [
  { id: 'ema5', kind: 'EMA', period: 5 },
  { id: 'ema10', kind: 'EMA', period: 10 },
  { id: 'rsi14', kind: 'RSI', period: 14 },
];

const VALID_SUMMARY_SPEC: StrategySpec = {
  schema_version: '1.0',
  name: 'test',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [...INDS],
  entry: {
    logic: 'AND',
    size: { kind: 'SetHoldings', fraction: 1 },
    conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' }],
  },
  exit: { logic: 'OR', conditions: [{ kind: 'BarsSinceEntry', op: '>=', value: 5 }] },
};

describe('plain-english', () => {
  describe('formatCondition', () => {
    it('renders indicator comparisons (IndicatorRefs, Subtract, normalized basis-point difference), undeclared ids and nested logic in plain English', () => {
      expect
        .soft(
          formatCondition(
            {
              kind: 'IndicatorComparison',
              left: { kind: 'IndicatorRef', indicator: 'ema5' },
              op: '>',
              right: { kind: 'IndicatorRef', indicator: 'ema10' },
            },
            INDS,
          ),
          'renders IndicatorComparison with two IndicatorRefs',
        )
        .toBe('EMA(5) > EMA(10)');

      expect
        .soft(
          formatCondition(
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
            INDS,
          ),
          'renders IndicatorComparison with Subtract operand',
        )
        .toBe('EMA(5) − EMA(10) ≥ 0.2');

      expect
        .soft(
          formatCondition(
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
            INDS,
          ),
          'renders IndicatorComparison with normalized basis-point difference',
        )
        .toBe('Δbps(EMA(5), EMA(10)) ≥ 4');

      const fragments = buildSummaryFragments({
        ...VALID_SUMMARY_SPEC,
        entry: {
          ...VALID_SUMMARY_SPEC.entry,
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
      });

      expect
        .soft(
          fragments.map((fragment) => fragment.text).join(''),
          'renders normalized basis-point differences in rich summary fragments',
        )
        .toContain('Δbps(EMA(5), EMA(10)) ≥ 4');

      expect
        .soft(
          formatCondition(
            {
              kind: 'IndicatorComparison',
              left: { kind: 'IndicatorRef', indicator: 'ghost' },
              op: '>',
              right: { kind: 'Const', value: 1 },
            },
            INDS,
          ),
          'shows {id} braces when an indicator id is undeclared (catches dangling refs)',
        )
        .toBe('{ghost} > 1');

      expect
        .soft(
          formatCondition(
            {
              logic: 'OR',
              conditions: [
                {
                  kind: 'IndicatorComparison',
                  left: { kind: 'IndicatorRef', indicator: 'ema5' },
                  op: '>',
                  right: { kind: 'IndicatorRef', indicator: 'ema10' },
                },
                { kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' },
              ],
            },
            INDS,
          ),
          'renders nested LogicNode with the right joiner and parens',
        )
        .toBe('(EMA(5) > EMA(10) OR EMA(5) crosses above EMA(10))');
    });

    it('renders the between, cross, bars-since-entry, time-of-day, PnL, drawdown and bar-property kinds in plain English', () => {
      expect
        .soft(
          formatCondition(
            { kind: 'IndicatorBetween', indicator: 'rsi14', lo: 50, hi: 70, inclusive: true },
            INDS,
          ),
          'renders IndicatorBetween inclusive',
        )
        .toBe('RSI(14) is between 50 and 70');
      expect
        .soft(
          formatCondition(
            { kind: 'IndicatorBetween', indicator: 'rsi14', lo: 50, hi: 70, inclusive: false },
            INDS,
          ),
          'renders IndicatorBetween exclusive',
        )
        .toBe('RSI(14) is strictly between 50 and 70');

      expect
        .soft(
          formatCondition(
            { kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' },
            INDS,
          ),
          'renders FreshCross direction-aware: up',
        )
        .toBe('EMA(5) crosses above EMA(10)');
      expect
        .soft(
          formatCondition(
            { kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'down' },
            INDS,
          ),
          'renders FreshCross direction-aware: down',
        )
        .toBe('EMA(5) crosses below EMA(10)');

      const c = (op: string) => ({ kind: 'BarsSinceEntry' as const, op: op as '>=' | '>' | '<=' | '<' | '==', value: 5 });
      expect.soft(formatCondition(c('>='), INDS), 'renders BarsSinceEntry naturally for >=').toBe('5 or more bars since entry');
      expect.soft(formatCondition(c('>'), INDS), 'renders BarsSinceEntry naturally for >').toBe('more than 5 bars since entry');
      expect.soft(formatCondition(c('<='), INDS), 'renders BarsSinceEntry naturally for <=').toBe('5 or fewer bars since entry');
      expect.soft(formatCondition(c('<'), INDS), 'renders BarsSinceEntry naturally for <').toBe('fewer than 5 bars since entry');
      expect.soft(formatCondition(c('=='), INDS), 'renders BarsSinceEntry naturally for ==').toBe('exactly 5 bars since entry');

      expect
        .soft(
          formatCondition({ kind: 'TimeOfDay', after: '09:45', before: '15:30' }, INDS),
          'renders TimeOfDay with both after and before',
        )
        .toBe('between 09:45 and 15:30');
      expect
        .soft(formatCondition({ kind: 'TimeOfDay', after: '09:45' }, INDS), 'renders TimeOfDay with after only')
        .toBe('after 09:45');
      expect
        .soft(formatCondition({ kind: 'TimeOfDay', before: '15:30' }, INDS), 'renders TimeOfDay with before only')
        .toBe('before 15:30');
      expect.soft(formatCondition({ kind: 'TimeOfDay' }, INDS), 'renders TimeOfDay with neither').toBe('any time');

      expect
        .soft(
          formatCondition({ kind: 'PnLPercent', op: '<=', value: -0.01 }, INDS),
          'renders PnLPercent as percent value: -1%',
        )
        .toBe('unrealized PnL ≤ -1%');
      expect
        .soft(
          formatCondition({ kind: 'PnLPercent', op: '>=', value: 0.005 }, INDS),
          'renders PnLPercent as percent value: 0.5%',
        )
        .toBe('unrealized PnL ≥ 0.5%');

      expect
        .soft(
          formatCondition({ kind: 'DrawdownFromPeak', value: 0.005 }, INDS),
          'renders DrawdownFromPeak as percent retrace',
        )
        .toBe('0.5% retrace from peak since entry');

      expect
        .soft(
          formatCondition(
            { kind: 'BarProperty', property: 'range_pct', op: '>=', value: 0.003 },
            INDS,
          ),
          'renders BarProperty with friendly label',
        )
        .toBe('bar range % ≥ 0.003');
    });
  });

  describe('formatEntryBlock', () => {
    it('renders the entry block as a readable sentence: the SPY EMA fixture in full, a non-100% sizing as a fraction percent label', () => {
      const entry = {
        logic: 'AND' as const,
        size: { kind: 'SetHoldings' as const, fraction: 1.0 },
        conditions: [
          { kind: 'FreshCross' as const, left: 'ema5', right: 'ema10', direction: 'up' as const },
          {
            kind: 'IndicatorComparison' as const,
            left: {
              kind: 'Subtract' as const,
              left: { kind: 'IndicatorRef' as const, indicator: 'ema5' },
              right: { kind: 'IndicatorRef' as const, indicator: 'ema10' },
            },
            op: '>=' as const,
            right: { kind: 'Const' as const, value: 0.2 },
          },
          {
            kind: 'IndicatorBetween' as const,
            indicator: 'rsi14',
            lo: 50,
            hi: 70,
            inclusive: true,
          },
        ],
      };
      const out = formatEntryBlock(entry, INDS);
      expect
        .soft(out, 'renders the SPY EMA fixture entry as a single readable sentence')
        .toBe(
          'Enter all-in when EMA(5) crosses above EMA(10) AND EMA(5) − EMA(10) ≥ 0.2 AND RSI(14) is between 50 and 70.',
        );

      const halfSized = formatEntryBlock(
        {
          logic: 'AND',
          size: { kind: 'SetHoldings', fraction: 0.5 },
          conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' }],
        },
        INDS,
      );
      expect.soft(halfSized, 'falls back to fraction percent label for non-100% sizing').toContain('Enter 50% of equity');
    });
  });

  describe('formatExitBlock', () => {
    it('renders the exit block: a BarsSinceEntry exit, and the no-exit message for empty conditions', () => {
      expect
        .soft(
          formatExitBlock(
            { logic: 'OR', conditions: [{ kind: 'BarsSinceEntry', op: '>=', value: 5 }] },
            INDS,
          ),
          'renders BarsSinceEntry exit',
        )
        .toBe('Exit when 5 or more bars since entry.');
      expect
        .soft(formatExitBlock({ logic: 'OR', conditions: [] }, INDS), 'handles empty exit conditions')
        .toBe('No signal-flip exit configured.');
    });
  });

  describe('formatSurvivalRule', () => {
    it('renders a typical stop-loss', () => {
      const out = formatSurvivalRule(
        {
          name: 'stop loss',
          when: {
            logic: 'AND',
            conditions: [{ kind: 'PnLPercent', op: '<=', value: -0.01 }],
          },
          action: { kind: 'CLOSE_ALL' },
        },
        INDS,
      );
      expect(out).toBe('stop loss: when unrealized PnL ≤ -1%, close the position.');
    });
  });
});
