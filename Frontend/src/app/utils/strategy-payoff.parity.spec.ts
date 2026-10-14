/**
 * Frontend payoff parity test (#3186).
 *
 * Pins agreement between `Frontend/src/app/utils/strategy-payoff.ts`
 * (`payoffExtremes`, `payoffBreakevens`) and the canonical Python engine
 * (`PythonDataService/app/services/strategy_engine.py`:
 * `compute_max_profit_loss`, `find_breakevens`). ADR 0068 exception 1.
 *
 * Fixture: `Frontend/src/testing/payoff-parity/cases.json`, written from the
 * canonical by `PythonDataService/scripts/regenerate_strategy_payoff_parity_fixture.py`
 * and re-checked against it on every Python run
 * (`tests/contracts/test_strategy_payoff_parity_fixture.py`).
 *
 * Tolerance (`atol=1e-4`, `rtol=0`): Python rounds what it returns to 4 dp
 * (error <= 5e-5) and the browser copy does not round.
 */

import { describe, expect, it } from 'vitest';

import payoffParity from '../../testing/payoff-parity/cases.json';
import { payoffBreakevens, payoffExtremes, PayoffLeg } from './strategy-payoff';

interface ParityCase {
  name: string;
  legs: {
    strike: number;
    option_type: 'call' | 'put';
    position: 'long' | 'short';
    premium: number;
    quantity: number;
  }[];
  // null: a side with no limit.
  expected: { max_profit: number | null; max_loss: number | null; breakevens: number[] };
}

interface ParityFixture {
  schema_version: number;
  tolerance: { atol: number; rtol: number };
  cases: ParityCase[];
}

const fixture = payoffParity as unknown as ParityFixture;
const { atol, rtol } = fixture.tolerance;

function legsOf(parityCase: ParityCase): PayoffLeg[] {
  return parityCase.legs.map(l => ({
    strike: l.strike,
    optionType: l.option_type,
    position: l.position,
    premium: l.premium,
    quantity: l.quantity,
  }));
}

describe('strategy-payoff.ts — frontend ↔ Python payoff parity', () => {
  it('loads the fixture with the expected shape', () => {
    expect(fixture.schema_version).toBe(1);
    expect(fixture.cases.length).toBeGreaterThan(0);
    expect(rtol).toBe(0);
  });

  it.each(fixture.cases.map(c => [c.name, c] as const))(
    '%s: max profit and max loss agree with the canonical',
    (_name, parityCase) => {
      const got = payoffExtremes(legsOf(parityCase));
      const { max_profit, max_loss } = parityCase.expected;

      if (max_profit === null) {
        expect(got.maxProfit).toBe(Number.POSITIVE_INFINITY);
      } else {
        expect(Math.abs(got.maxProfit - max_profit)).toBeLessThanOrEqual(atol);
      }
      if (max_loss === null) {
        expect(got.maxLoss).toBe(Number.NEGATIVE_INFINITY);
      } else {
        expect(Math.abs(got.maxLoss - max_loss)).toBeLessThanOrEqual(atol);
      }
    },
  );

  it.each(fixture.cases.map(c => [c.name, c] as const))(
    '%s: breakevens agree with the canonical',
    (_name, parityCase) => {
      const got = payoffBreakevens(legsOf(parityCase));
      const want = parityCase.expected.breakevens;

      expect(got.length).toBe(want.length);
      want.forEach((breakeven, i) => {
        expect(Math.abs(got[i] - breakeven)).toBeLessThanOrEqual(atol);
      });
    },
  );
});
