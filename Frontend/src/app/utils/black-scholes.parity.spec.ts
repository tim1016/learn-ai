/**
 * Browser Black-Scholes parity test (ADR 0068 exception 1).
 *
 * Holds `Frontend/src/app/utils/black-scholes.ts` and `strategy-payoff.ts` to the
 * canonical Python: `PythonDataService/app/services/bs_greeks.py` for one option's
 * price and Greeks, `app/services/strategy_engine.py` for a strategy's expiry
 * payoff, current-time P&L under the page's what-ifs, and aggregate Greeks.
 *
 * Fixture: `Frontend/src/testing/bs-parity/grid.json`, golden fixture BS-008.
 * `PythonDataService/scripts/fixture_generators/bs_browser_parity.py` writes it
 * from the canonical, and `tests/fixtures/test_bs_browser_parity_fixture.py`
 * re-checks it against the canonical on every Python run. Each quantity has its
 * own tolerance in the file, with the reason: the browser's normal CDF is
 * Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7), the canonical's is exact.
 *
 * Max profit, max loss and breakevens are held by `strategy-payoff.parity.spec.ts`.
 */

import { describe, expect, it } from 'vitest';

import bsParityGrid from '../../testing/bs-parity/grid.json';
import {
  bsDelta, bsGamma, bsPrice, bsRho, bsTheta, bsVega,
  strategyGreekAtPrice, strategyPnlAtPrice, LegParams,
} from './black-scholes';
import { payoffAtExpiry } from './strategy-payoff';

interface Tolerance {
  atol: number;
  rtol: number;
  why: string;
}

interface OptionCase {
  spot: number;
  strike: number;
  ttm_days: number;
  ttm_years: number;
  volatility: number;
  rate: number;
  dividend: number;
  option_type: 'call' | 'put';
  expected_price: number;
  // Only where the dividend is 0: the browser copy takes no dividend yield.
  expected_greeks?: { delta: number; gamma: number; theta: number; vega: number; rho: number };
}

interface StrategyCase {
  name: string;
  rate: number;
  ttm_years: number;
  legs: {
    strike: number;
    option_type: 'call' | 'put';
    position: 'long' | 'short';
    premium: number;
    iv: number;
    quantity: number;
  }[];
  prices: number[];
  expected: {
    expiry_payoff: number[];
    pnl: Record<string, number[]>;
    greeks: { delta: number[]; gamma: number[]; theta: number[]; vega: number[] };
  };
}

interface ParityFixture {
  schema_version: number;
  tolerance: Record<string, Tolerance>;
  scenarios: { name: string; iv_shift: number; time_shift_days: number }[];
  n_cases: number;
  cases: OptionCase[];
  strategies: StrategyCase[];
}

const fixture = bsParityGrid as unknown as ParityFixture;

/** The difference of `got` from `want`, as a failure message, or null when it is within the quantity's tolerance. */
function miss(got: number, want: number, quantity: string, where: string): string | null {
  const { atol, rtol } = fixture.tolerance[quantity];
  const error = Math.abs(got - want);
  return error <= atol + rtol * Math.abs(want)
    ? null
    : `${quantity} ${where}: got ${got}, canonical ${want}, off by ${error.toExponential(2)} (atol ${atol})`;
}

function missesOf(checks: (string | null)[]): string[] {
  return checks.filter((message): message is string => message !== null);
}

describe('black-scholes.ts — frontend ↔ Python parity', () => {
  it('holds every quantity to a tolerance that is absolute', () => {
    expect(fixture.schema_version).toBe(2);
    expect(fixture.cases.length).toBe(fixture.n_cases);
    for (const [quantity, tolerance] of Object.entries(fixture.tolerance)) {
      expect(tolerance.rtol, quantity).toBe(0);
      expect(tolerance.why.length, quantity).toBeGreaterThan(0);
    }
  });

  it('prices every case of the grid like the canonical', () => {
    // The browser pricer takes no dividend yield. Pricing at S·e^(−qT) with the
    // same rate gives the same d1 and d2 and the same price as the canonical at q.
    const misses = missesOf(fixture.cases.map(c => {
      const got = bsPrice(c.spot * Math.exp(-c.dividend * c.ttm_years), c.strike, c.rate, c.volatility, c.ttm_years, c.option_type);
      return miss(got, c.expected_price, 'price', JSON.stringify(c));
    }));

    expect(misses).toEqual([]);
  });

  it('gives every case the canonical delta, gamma, theta, vega and rho', () => {
    expect(fixture.cases.some(c => c.expected_greeks !== undefined)).toBe(true);

    const misses = missesOf(fixture.cases.flatMap(c => {
      const want = c.expected_greeks;
      if (want === undefined) return [];
      const args = [c.spot, c.strike, c.rate, c.volatility, c.ttm_years] as const;
      const where = JSON.stringify(c);
      return [
        miss(bsDelta(...args, c.option_type), want.delta, 'delta', where),
        miss(bsGamma(...args), want.gamma, 'gamma', where),
        miss(bsTheta(...args, c.option_type), want.theta, 'theta', where),
        miss(bsVega(...args), want.vega, 'vega', where),
        miss(bsRho(...args, c.option_type), want.rho, 'rho', where),
      ];
    }));

    expect(misses).toEqual([]);
  });

  describe('a strategy', () => {
    it.each(fixture.strategies.map(s => [s.name, s] as const))(
      '%s: expiry payoff, P&L under every what-if and aggregate Greeks agree with the canonical',
      (_name, strategy) => {
        const legs: LegParams[] = strategy.legs.map(l => ({
          strike: l.strike,
          optionType: l.option_type,
          position: l.position,
          premium: l.premium,
          iv: l.iv,
          quantity: l.quantity,
        }));
        const { expected, rate, ttm_years: t } = strategy;
        const misses: (string | null)[] = [];

        strategy.prices.forEach((price, i) => {
          const where = `${strategy.name} @ ${price}`;
          misses.push(miss(payoffAtExpiry(legs, price), expected.expiry_payoff[i], 'strategy_expiry_payoff', where));
          for (const scenario of fixture.scenarios) {
            const timeLeft = Math.max(t - scenario.time_shift_days / 365, 0);
            misses.push(miss(
              strategyPnlAtPrice(legs, price, timeLeft, rate, scenario.iv_shift),
              expected.pnl[scenario.name][i],
              'strategy_pnl',
              `${where} (${scenario.name})`,
            ));
          }
          for (const greek of ['delta', 'gamma', 'theta', 'vega'] as const) {
            misses.push(miss(
              strategyGreekAtPrice(legs, price, t, rate, greek),
              expected.greeks[greek][i],
              `strategy_${greek}`,
              where,
            ));
          }
        });

        expect(missesOf(misses)).toEqual([]);
      },
    );
  });
});
