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

  it('offers conditions by context: trade-only kinds hidden from entry, every kind in exit and manage', () => {
    const entry = conditionsForContext('entry');
    const tradeOnly = ['BarsSinceEntry', 'PnLPercent', 'PnLPoints', 'DrawdownFromPeak'];
    for (const k of tradeOnly) {
      expect.soft(entry, `hides trade-only kind ${k} from the entry-context picker`).not.toContain(k);
    }
    // FreshCross etc. should still be there.
    expect.soft(entry, 'keeps FreshCross in the entry-context picker').toContain('FreshCross');
    expect.soft(entry, 'keeps IndicatorComparison in the entry-context picker').toContain('IndicatorComparison');

    expect.soft(conditionsForContext('exit'), 'exposes PnLPercent in the exit context').toContain('PnLPercent');
    expect.soft(conditionsForContext('manage'), 'exposes DrawdownFromPeak in the manage context').toContain('DrawdownFromPeak');
  });

  it('groups conditions by category for the picker UI, in CONDITION_GROUPS order', () => {
    const entry = groupedConditionsForContext('entry');
    const groupKeys = entry.map((g) => g.group);
    // Entry tab should show signals/filters/timing/shape but NOT risk
    // (because all risk kinds are trade-only).
    expect.soft(groupKeys, 'entry shows the signal group').toContain('signal');
    expect.soft(groupKeys, 'entry shows the filter group').toContain('filter');
    expect.soft(groupKeys, 'entry shows the timing group').toContain('timing');
    expect.soft(groupKeys, 'entry hides the risk group').not.toContain('risk');

    // Manage shows everything.
    const manage = groupedConditionsForContext('manage');
    expect.soft(manage.map((g) => g.group), 'manage shows the risk group').toContain('risk');

    const order = Object.keys(CONDITION_GROUPS);
    const manageGroups = manage.map((g) => g.group);
    // manage should preserve the order of CONDITION_GROUPS — i.e. signals first.
    expect.soft(manageGroups[0], 'orders groups consistently per CONDITION_GROUPS order').toBe(order[0]);
  });
});
