/**
 * Frontend Black-Scholes parity test.
 * See `docs/architecture/iv-ownership-research.md` §6 (tolerances and
 * validation) for the consolidated tolerance table.
 *
 * Pins agreement between `Frontend/src/app/utils/black-scholes.ts::bsPrice`
 * and the canonical Python pricer
 * (`PythonDataService/app/services/bs_greeks.py::bs_european_price`),
 * which itself is pinned at `atol=1e-10` against py_vollib.
 *
 * Fixture: `Frontend/src/testing/bs-parity/grid.json` (360 cases).
 * Regenerable via `Frontend/scripts/generate-bs-parity-fixture.py`.
 *
 * Tolerance (`atol=1e-4`) is bounded by the frontend's Abramowitz & Stegun
 * 7.1.26 normal-CDF approximation (|error| < 1.5e-7), which propagates to
 * up to ~1.5e-5 in BS price units. The test fails loudly if drift
 * exceeds this — that would indicate a real bug in the frontend pricer,
 * not a precision artifact.
 *
 * Until this test exists, the legacy frontend BS module has a single
 * audit point (the cross-engine parity test on the Python side that
 * pins all server-canonical pricing) but no automatic check that the
 * client-side mirror agrees with it. This test closes that gap.
 */

import { describe, expect, it } from 'vitest';

import bsParityGrid from '../../testing/bs-parity/grid.json';
import { bsPrice } from './black-scholes';

interface BsParityCase {
  spot: number;
  strike: number;
  ttm_days: number;
  ttm_years: number;
  volatility: number;
  rate: number;
  dividend: number;
  option_type: 'call' | 'put';
  expected_price: number;
}

interface BsParityFixture {
  schema_version: number;
  source: string;
  tolerance: { atol: number; rtol: number; rationale: string };
  n_cases: number;
  cases: BsParityCase[];
}

describe('black-scholes.ts — frontend ↔ Python BS parity', () => {
  const fixture = bsParityGrid as unknown as BsParityFixture;
  const { atol, rtol } = fixture.tolerance;

  it('loads the fixture with the expected shape', () => {
    expect(fixture.schema_version).toBe(1);
    expect(fixture.cases.length).toBe(360);
    expect(fixture.n_cases).toBe(fixture.cases.length);
    expect(rtol).toBe(0);
  });

  it('every case agrees with the canonical Python pricer within atol', () => {
    // Frontend BS doesn't take dividend yield; we mimic q via spot
    // pre-discount (S' = S·e^(-qT), use r as carry). This produces the
    // same d1 / d2 as the Python `bs_european_price(dividend=q)`:
    //   d1' = (ln(S'/K) + (r + 0.5σ²)T) / (σ√T)
    //       = (ln(S/K) + (r - q + 0.5σ²)T) / (σ√T) = BS-Merton d1.
    //   price' = S'·N(d1) - K·e^(-rT)·N(d2)
    //          = S·e^(-qT)·N(d1) - K·e^(-rT)·N(d2) = BS-Merton call.

    let maxErr = 0;
    let worstCase: BsParityCase | null = null;
    let mismatchCount = 0;

    for (const c of fixture.cases) {
      const spotAdjusted = c.spot * Math.exp(-c.dividend * c.ttm_years);
      const got = bsPrice(
        spotAdjusted,
        c.strike,
        c.rate,
        c.volatility,
        c.ttm_years,
        c.option_type,
      );
      const err = Math.abs(got - c.expected_price);
      const tol = atol + rtol * Math.abs(c.expected_price);
      if (err > tol) {
        mismatchCount += 1;
      }
      if (err > maxErr) {
        maxErr = err;
        worstCase = c;
      }
    }

    if (mismatchCount > 0 || maxErr > atol) {
      console.error(
        `BS parity: ${mismatchCount}/${fixture.cases.length} cases over tolerance; ` +
          `max error ${maxErr.toExponential(3)} > atol=${atol}\n` +
          `Worst case: ${JSON.stringify(worstCase, null, 2)}`,
      );
    }
    expect(mismatchCount).toBe(0);
    expect(maxErr).toBeLessThanOrEqual(atol);
  });
});
