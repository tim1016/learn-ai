import { describe, expect, it } from 'vitest';
import { StrategySpec } from '../../graphql/spec-strategy.models';
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
  it('indicator mutators append, drop and patch without touching the original', () => {
    const added = addIndicator(BASE, { id: 'sma_l', kind: 'SMA', period: 30 });
    expect.soft(added.indicators, 'addIndicator appends without mutating the original: appended length').toHaveLength(2);
    expect.soft(added.indicators[1].id, 'addIndicator appends without mutating the original: appended id').toBe('sma_l');
    expect.soft(BASE.indicators, 'addIndicator appends without mutating the original: original untouched').toHaveLength(1);

    const removed = removeIndicatorAt(BASE, 0);
    expect.soft(removed.indicators, 'removeIndicatorAt drops by index').toHaveLength(0);

    const updated = updateIndicatorAt(BASE, 0, { period: 20 });
    expect.soft(updated.indicators[0].period, 'updateIndicatorAt patches in place: patched period').toBe(20);
    expect.soft(updated.indicators[0].id, 'updateIndicatorAt patches in place: other fields preserved').toBe('sma_s');
  });

  it('entry mutators append, drop and replace conditions and flip the logic', () => {
    const added = addEntryCondition(BASE, { kind: 'BarsSinceEntry', op: '>=', value: 5 });
    expect.soft(added.entry.conditions, 'addEntryCondition appends to entry.conditions').toHaveLength(2);

    const removed = removeEntryConditionAt(BASE, 0);
    expect.soft(removed.entry.conditions, 'removeEntryConditionAt drops by index').toHaveLength(0);

    const replaced = updateEntryConditionAt(BASE, 0, {
      kind: 'FreshCross',
      left: 'sma_s',
      right: 'sma_l',
      direction: 'down',
    });
    const cond = replaced.entry.conditions[0];
    expect.soft(cond, 'updateEntryConditionAt replaces in place').toMatchObject({ kind: 'FreshCross', direction: 'down' });

    expect.soft(setEntryLogic(BASE, 'OR').entry.logic, 'setEntryLogic flips AND ↔ OR').toBe('OR');
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
    expect((after.survival ?? [])[0].name).toBe('stop');
    expect((after.survival ?? [])[0].action).toEqual({ kind: 'CLOSE_ALL' });

    const renamed = updateSurvivalRuleAt(after, 0, {
      ...rule,
      name: 'tighter stop',
    });
    expect((renamed.survival ?? [])[0].name).toBe('tighter stop');

    expect(removeSurvivalRuleAt(renamed, 0).survival).toHaveLength(0);
  });

  it('setStrategyName sets only the name', () => {
    const out = setStrategyName(BASE, 'My EMA crossover v2');
    expect(out.name).toBe('My EMA crossover v2');
    expect(out.indicators).toBe(BASE.indicators); // referential equality on untouched arrays
  });
});
