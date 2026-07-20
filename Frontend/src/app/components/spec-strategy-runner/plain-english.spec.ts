import { describe, expect, it } from 'vitest';
import { IndicatorBlock, StrategySpec } from '../../graphql/spec-strategy.models';
import {
  formatCondition,
  formatEntryBlock,
  formatExitBlock,
  formatStrategySummary,
  formatSurvivalRule,
} from './plain-english';

const INDS: readonly IndicatorBlock[] = [
  { id: 'ema5', kind: 'EMA', period: 5 },
  { id: 'ema10', kind: 'EMA', period: 10 },
  { id: 'rsi14', kind: 'RSI', period: 14 },
];

describe('plain-english', () => {
  describe('formatCondition', () => {
    it('renders IndicatorComparison with two IndicatorRefs', () => {
      const out = formatCondition(
        {
          kind: 'IndicatorComparison',
          left: { kind: 'IndicatorRef', indicator: 'ema5' },
          op: '>',
          right: { kind: 'IndicatorRef', indicator: 'ema10' },
        },
        INDS,
      );
      expect(out).toBe('EMA(5) > EMA(10)');
    });

    it('renders IndicatorComparison with Subtract operand', () => {
      const out = formatCondition(
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
      );
      expect(out).toBe('EMA(5) − EMA(10) ≥ 0.2');
    });

    it('renders IndicatorBetween inclusive vs exclusive', () => {
      expect(
        formatCondition(
          { kind: 'IndicatorBetween', indicator: 'rsi14', lo: 50, hi: 70, inclusive: true },
          INDS,
        ),
      ).toBe('RSI(14) is between 50 and 70');
      expect(
        formatCondition(
          { kind: 'IndicatorBetween', indicator: 'rsi14', lo: 50, hi: 70, inclusive: false },
          INDS,
        ),
      ).toBe('RSI(14) is strictly between 50 and 70');
    });

    it('renders FreshCross direction-aware', () => {
      expect(
        formatCondition(
          { kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' },
          INDS,
        ),
      ).toBe('EMA(5) crosses above EMA(10)');
      expect(
        formatCondition(
          { kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'down' },
          INDS,
        ),
      ).toBe('EMA(5) crosses below EMA(10)');
    });

    it('renders BarsSinceEntry naturally for >=, >, <=, <, ==', () => {
      const c = (op: string) => ({ kind: 'BarsSinceEntry' as const, op: op as '>=' | '>' | '<=' | '<' | '==', value: 5 });
      expect(formatCondition(c('>='), INDS)).toBe('5 or more bars since entry');
      expect(formatCondition(c('>'), INDS)).toBe('more than 5 bars since entry');
      expect(formatCondition(c('<='), INDS)).toBe('5 or fewer bars since entry');
      expect(formatCondition(c('<'), INDS)).toBe('fewer than 5 bars since entry');
      expect(formatCondition(c('=='), INDS)).toBe('exactly 5 bars since entry');
    });

    it('renders TimeOfDay with after, before, both, neither', () => {
      expect(
        formatCondition({ kind: 'TimeOfDay', after: '09:45', before: '15:30' }, INDS),
      ).toBe('between 09:45 and 15:30');
      expect(formatCondition({ kind: 'TimeOfDay', after: '09:45' }, INDS)).toBe('after 09:45');
      expect(formatCondition({ kind: 'TimeOfDay', before: '15:30' }, INDS)).toBe('before 15:30');
      expect(formatCondition({ kind: 'TimeOfDay' }, INDS)).toBe('any time');
    });

    it('renders PnLPercent as percent value', () => {
      expect(formatCondition({ kind: 'PnLPercent', op: '<=', value: -0.01 }, INDS)).toBe(
        'unrealized PnL ≤ -1%',
      );
      expect(formatCondition({ kind: 'PnLPercent', op: '>=', value: 0.005 }, INDS)).toBe(
        'unrealized PnL ≥ 0.5%',
      );
    });

    it('renders DrawdownFromPeak as percent retrace', () => {
      expect(formatCondition({ kind: 'DrawdownFromPeak', value: 0.005 }, INDS)).toBe(
        '0.5% retrace from peak since entry',
      );
    });

    it('renders BarProperty with friendly label', () => {
      expect(
        formatCondition(
          { kind: 'BarProperty', property: 'range_pct', op: '>=', value: 0.003 },
          INDS,
        ),
      ).toBe('bar range % ≥ 0.003');
    });

    it('shows {id} braces when an indicator id is undeclared (catches dangling refs)', () => {
      expect(
        formatCondition(
          {
            kind: 'IndicatorComparison',
            left: { kind: 'IndicatorRef', indicator: 'ghost' },
            op: '>',
            right: { kind: 'Const', value: 1 },
          },
          INDS,
        ),
      ).toBe('{ghost} > 1');
    });

    it('renders nested LogicNode with the right joiner and parens', () => {
      const out = formatCondition(
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
      );
      expect(out).toBe('(EMA(5) > EMA(10) OR EMA(5) crosses above EMA(10))');
    });
  });

  describe('formatEntryBlock', () => {
    it('renders the SPY EMA fixture entry as a single readable sentence', () => {
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
      expect(out).toBe(
        'Enter all-in when EMA(5) crosses above EMA(10) AND EMA(5) − EMA(10) ≥ 0.2 AND RSI(14) is between 50 and 70.',
      );
    });

    it('falls back to fraction percent label for non-100% sizing', () => {
      const out = formatEntryBlock(
        {
          logic: 'AND',
          size: { kind: 'SetHoldings', fraction: 0.5 },
          conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' }],
        },
        INDS,
      );
      expect(out).toContain('Enter 50% of equity');
    });
  });

  describe('formatExitBlock', () => {
    it('renders BarsSinceEntry exit', () => {
      expect(
        formatExitBlock(
          { logic: 'OR', conditions: [{ kind: 'BarsSinceEntry', op: '>=', value: 5 }] },
          INDS,
        ),
      ).toBe('Exit when 5 or more bars since entry.');
    });

    it('handles empty exit conditions', () => {
      expect(formatExitBlock({ logic: 'OR', conditions: [] }, INDS)).toBe(
        'No signal-flip exit configured.',
      );
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

  describe('formatStrategySummary', () => {
    it('combines entry / survival / exit into one paragraph', () => {
      const spec: StrategySpec = {
        schema_version: '1.0',
        name: 'test',
        symbols: ['SPY'],
        resolution: { period_minutes: 15 },
        indicators: INDS as IndicatorBlock[],
        entry: {
          logic: 'AND',
          size: { kind: 'SetHoldings', fraction: 1 },
          conditions: [{ kind: 'FreshCross', left: 'ema5', right: 'ema10', direction: 'up' }],
        },
        survival: [
          {
            name: 'hard stop',
            when: { logic: 'AND', conditions: [{ kind: 'PnLPercent', op: '<=', value: -0.01 }] },
            action: { kind: 'CLOSE_ALL' },
          },
        ],
        exit: { logic: 'OR', conditions: [{ kind: 'BarsSinceEntry', op: '>=', value: 5 }] },
      };
      const out = formatStrategySummary(spec);
      expect(out).toContain('Enter all-in when EMA(5) crosses above EMA(10).');
      expect(out).toContain('Manage rules: hard stop.');
      expect(out).toContain('Exit when 5 or more bars since entry.');
    });
  });
});
