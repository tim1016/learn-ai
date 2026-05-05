import { describe, expect, it } from 'vitest';
import { StrategySpec } from '../../graphql/spec-strategy-types';
import {
  addEntryCondition,
  addExitCondition,
  addIndicator,
  addSurvivalRule,
  buildCloseAllSurvivalRule,
  removeEntryConditionAt,
  removeExitConditionAt,
  removeIndicatorAt,
  removeSurvivalRuleAt,
  setEntryLogic,
  setStrategyName,
  updateEntryConditionAt,
  updateExitConditionAt,
  updateIndicatorAt,
  updateSurvivalRuleAt,
} from './spec-mutators';

const BASE: StrategySpec = {
  schema_version: '1.0',
  name: 'test',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [{ id: 'sma_s', kind: 'SMA', period: 10 }],
  entry: {
    logic: 'AND',
    size: { kind: 'SetHoldings', fraction: 1 },
    conditions: [{ kind: 'FreshCross', left: 'sma_s', right: 'sma_l', direction: 'up' }],
  },
  exit: { logic: 'OR', conditions: [] },
};

describe('spec-mutators', () => {
  it('addIndicator appends without mutating the original', () => {
    const out = addIndicator(BASE, { id: 'sma_l', kind: 'SMA', period: 30 });
    expect(out.indicators).toHaveLength(2);
    expect(out.indicators[1].id).toBe('sma_l');
    expect(BASE.indicators).toHaveLength(1); // original untouched
  });

  it('removeIndicatorAt drops by index', () => {
    const out = removeIndicatorAt(BASE, 0);
    expect(out.indicators).toHaveLength(0);
  });

  it('updateIndicatorAt patches in place', () => {
    const out = updateIndicatorAt(BASE, 0, { period: 20 });
    expect(out.indicators[0].period).toBe(20);
    expect(out.indicators[0].id).toBe('sma_s'); // other fields preserved
  });

  it('addEntryCondition appends to entry.conditions', () => {
    const out = addEntryCondition(BASE, { kind: 'BarsSinceEntry', op: '>=', value: 5 });
    expect(out.entry.conditions).toHaveLength(2);
  });

  it('removeEntryConditionAt drops by index', () => {
    const out = removeEntryConditionAt(BASE, 0);
    expect(out.entry.conditions).toHaveLength(0);
  });

  it('updateEntryConditionAt replaces in place', () => {
    const out = updateEntryConditionAt(BASE, 0, {
      kind: 'FreshCross',
      left: 'sma_s',
      right: 'sma_l',
      direction: 'down',
    });
    const cond = out.entry.conditions[0];
    expect(cond).toMatchObject({ kind: 'FreshCross', direction: 'down' });
  });

  it('setEntryLogic flips AND ↔ OR', () => {
    expect(setEntryLogic(BASE, 'OR').entry.logic).toBe('OR');
  });

  it('exit mutators mirror entry mutators', () => {
    const added = addExitCondition(BASE, { kind: 'BarsSinceEntry', op: '>=', value: 3 });
    expect(added.exit.conditions).toHaveLength(1);
    const updated = updateExitConditionAt(added, 0, { kind: 'BarsSinceEntry', op: '>=', value: 7 });
    expect((updated.exit.conditions[0] as { value: number }).value).toBe(7);
    expect(removeExitConditionAt(updated, 0).exit.conditions).toHaveLength(0);
  });

  it('survival rule mutators compose with buildCloseAllSurvivalRule', () => {
    const rule = buildCloseAllSurvivalRule('stop', {
      logic: 'AND',
      conditions: [{ kind: 'PnLPercent', op: '<=', value: -0.01 }],
    });
    const after = addSurvivalRule(BASE, rule);
    expect(after.survival).toHaveLength(1);
    expect(after.survival![0].name).toBe('stop');
    expect(after.survival![0].action).toEqual({ kind: 'CLOSE_ALL' });

    const renamed = updateSurvivalRuleAt(after, 0, {
      ...rule,
      name: 'tighter stop',
    });
    expect(renamed.survival![0].name).toBe('tighter stop');

    expect(removeSurvivalRuleAt(renamed, 0).survival).toHaveLength(0);
  });

  it('setStrategyName sets only the name', () => {
    const out = setStrategyName(BASE, 'My EMA crossover v2');
    expect(out.name).toBe('My EMA crossover v2');
    expect(out.indicators).toBe(BASE.indicators); // referential equality on untouched arrays
  });
});
