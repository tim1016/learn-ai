import { describe, expect, it } from 'vitest';
import {
  CONDITION_CATALOG,
  CONDITION_GROUPS,
  conditionsForContext,
  groupedConditionsForContext,
} from './condition-catalog';

describe('condition-catalog', () => {
  it('covers all 9 supported condition kinds with friendly labels and examples', () => {
    const kinds = Object.keys(CONDITION_CATALOG);
    expect(kinds).toHaveLength(9);
    for (const k of kinds) {
      const meta = CONDITION_CATALOG[k as keyof typeof CONDITION_CATALOG];
      expect(meta.label).toBeTruthy();
      expect(meta.short).toBeTruthy();
      expect(meta.blurb).toBeTruthy();
      expect(meta.example).toBeTruthy();
      expect(meta.group in CONDITION_GROUPS).toBe(true);
    }
  });

  it('hides trade-only kinds from the entry-context picker', () => {
    const entry = conditionsForContext('entry');
    const tradeOnly = ['BarsSinceEntry', 'PnLPercent', 'PnLPoints', 'DrawdownFromPeak'];
    for (const k of tradeOnly) {
      expect(entry).not.toContain(k);
    }
    // FreshCross etc. should still be there.
    expect(entry).toContain('FreshCross');
    expect(entry).toContain('IndicatorComparison');
  });

  it('exposes all kinds in exit and manage contexts', () => {
    expect(conditionsForContext('exit')).toContain('PnLPercent');
    expect(conditionsForContext('manage')).toContain('DrawdownFromPeak');
  });

  it('groups conditions by category for the picker UI', () => {
    const entry = groupedConditionsForContext('entry');
    const groupKeys = entry.map((g) => g.group);
    // Entry tab should show signals/filters/timing/shape but NOT risk
    // (because all risk kinds are trade-only).
    expect(groupKeys).toContain('signal');
    expect(groupKeys).toContain('filter');
    expect(groupKeys).toContain('timing');
    expect(groupKeys).not.toContain('risk');

    // Manage shows everything.
    const manage = groupedConditionsForContext('manage');
    expect(manage.map((g) => g.group)).toContain('risk');
  });

  it('orders groups consistently per CONDITION_GROUPS order', () => {
    const order = Object.keys(CONDITION_GROUPS);
    const manage = groupedConditionsForContext('manage').map((g) => g.group);
    // manage should preserve the order of CONDITION_GROUPS — i.e. signals first.
    expect(manage[0]).toBe(order[0]);
  });
});
