import { describe, expect, it } from 'vitest';
import validSingleStock from '../../testing/action_plan_fixtures/valid_single_stock.json';
import invalidMissingUnderlying from '../../testing/action_plan_fixtures/invalid_missing_underlying.json';
import invalidQtyRatioZero from '../../testing/action_plan_fixtures/invalid_qty_ratio_zero.json';
import invalidOrphanCloseLeg from '../../testing/action_plan_fixtures/invalid_orphan_close_leg.json';
import invalidDuplicateLegId from '../../testing/action_plan_fixtures/invalid_duplicate_leg_id.json';
import {
  validateActionPlan,
  type ActionPlanIssueCode,
} from './action-plan-validator';

/** Cross-language parity assertion: every fixture that fails in Python
 * (via Pydantic) must also fail in TypeScript with a categorical match.
 * Python tests live at ``tests/schemas/test_action_plan_fixtures.py``;
 * fixtures are mounted into the frontend container from
 * ``PythonDataService/tests/fixtures/action_plan`` (see compose.yaml +
 * tsconfig.json paths). */
describe('validateActionPlan — shared JSON fixtures', () => {
  it('accepts valid_single_stock.json', () => {
    expect(validateActionPlan(validSingleStock)).toEqual([]);
  });

  it.each<[unknown, ActionPlanIssueCode]>([
    [invalidMissingUnderlying, 'missing_underlying'],
    [invalidQtyRatioZero, 'invalid_qty_ratio'],
    [invalidOrphanCloseLeg, 'orphan_close_leg'],
    [invalidDuplicateLegId, 'duplicate_leg_id'],
  ])('rejects an invalid fixture with the expected issue code', (plan, expected) => {
    const issues = validateActionPlan(plan);

    expect(issues.length).toBeGreaterThan(0);
    expect(issues.map((i) => i.code)).toContain(expected);
  });
});
