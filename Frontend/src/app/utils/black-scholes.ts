/**
 * Browser Black-Scholes pricing and Greeks, for the Strategy Builder's live curves.
 *
 * Formula: C = S·N(d1) − K·e^(−rT)·N(d2); P = K·e^(−rT)·N(−d2) − S·N(−d1);
 *   d1 = [ln(S/K) + (r + σ²/2)T] / (σ√T); d2 = d1 − σ√T. Theta is per calendar day,
 *   vega and rho per 1 point. The dividend yield is 0, as on the server. At T ≤ 0 or σ ≤ 0
 *   an option is worth its intrinsic value, as `strategy_engine.py` prices a leg.
 * Reference: Hull, Options Futures and Other Derivatives (10e), §15.8 and §19; the normal
 *   CDF is Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7).
 * Canonical implementation: PythonDataService/app/services/bs_greeks.py (price, Greeks) and
 *   PythonDataService/app/services/strategy_engine.py (strategy P&L and Greek curves). This copy
 *   is ADR 0068 exception 1: the builder re-prices a 1,200-point grid on every leg edit.
 * Validated against: Frontend/src/app/utils/black-scholes.parity.spec.ts, against
 *   Frontend/src/testing/bs-parity/grid.json, which
 *   PythonDataService/scripts/fixture_generators/bs_browser_parity.py writes from the canonical
 *   and PythonDataService/tests/fixtures/test_bs_browser_parity_fixture.py re-checks (BS-008).
 *
 * Do not add callers: a new surface that needs Black-Scholes goes through the server.
 */

export interface LegParams {
  strike: number;
  optionType: 'call' | 'put';
  position: 'long' | 'short';
  premium: number;
  iv: number;
  quantity: number;
}

export type GreekName = 'delta' | 'gamma' | 'theta' | 'vega' | 'rho';

// ---------------------------------------------------------------------------
// Normal distribution
// ---------------------------------------------------------------------------

/**
 * Standard normal CDF — Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7).
 *
 * Computes erf(|x|/√2) via the A&S rational approximation, then converts
 * to Φ(x) = 0.5 × (1 + sign(x) × erf(|x|/√2)).
 */
export function normCdf(x: number): number {
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;

  const sign = x < 0 ? -1 : 1;
  const absX = Math.abs(x) / Math.SQRT2;

  const t = 1.0 / (1.0 + p * absX);
  const poly =
    ((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t;

  const erf = 1.0 - poly * Math.exp(-absX * absX);

  return 0.5 * (1.0 + sign * erf);
}

/** Standard normal PDF. */
export function normPdf(x: number): number {
  return Math.exp((-x * x) / 2) / Math.sqrt(2 * Math.PI);
}

// ---------------------------------------------------------------------------
// Black-Scholes d1 / d2
// ---------------------------------------------------------------------------

export function bsD1(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
): number {
  if (sigma <= 0 || t <= 0 || spot <= 0 || strike <= 0) return 0;
  return (
    (Math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) /
    (sigma * Math.sqrt(t))
  );
}

export function bsD2(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
): number {
  if (sigma <= 0 || t <= 0) return 0;
  return bsD1(spot, strike, r, sigma, t) - sigma * Math.sqrt(t);
}

// ---------------------------------------------------------------------------
// Option pricing
// ---------------------------------------------------------------------------

/** Black-Scholes theoretical price for a European option. */
export function bsPrice(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
  optionType: 'call' | 'put',
): number {
  if (t <= 0 || sigma <= 0) {
    // Expired, or no volatility to price with (a chain row without an IV):
    // intrinsic value, which is what the server values such a leg at.
    return optionType === 'call'
      ? Math.max(spot - strike, 0)
      : Math.max(strike - spot, 0);
  }
  if (spot <= 0 || strike <= 0) return 0;

  const d1 = bsD1(spot, strike, r, sigma, t);
  const d2 = d1 - sigma * Math.sqrt(t);
  const discount = Math.exp(-r * t);

  if (optionType === 'call') {
    return spot * normCdf(d1) - strike * discount * normCdf(d2);
  }
  return strike * discount * normCdf(-d2) - spot * normCdf(-d1);
}

// ---------------------------------------------------------------------------
// Greeks (per single option, unsigned)
// ---------------------------------------------------------------------------

export function bsDelta(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
  optionType: 'call' | 'put',
): number {
  if (sigma <= 0 || t <= 0) {
    if (t <= 0) {
      return optionType === 'call'
        ? spot > strike ? 1 : 0
        : spot < strike ? -1 : 0;
    }
    return 0;
  }
  const d1 = bsD1(spot, strike, r, sigma, t);
  return optionType === 'call' ? normCdf(d1) : normCdf(d1) - 1;
}

export function bsGamma(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
): number {
  if (sigma <= 0 || t <= 0 || spot <= 0) return 0;
  const d1 = bsD1(spot, strike, r, sigma, t);
  return normPdf(d1) / (spot * sigma * Math.sqrt(t));
}

/** Theta per calendar day. */
export function bsTheta(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
  optionType: 'call' | 'put',
): number {
  if (sigma <= 0 || t <= 0 || spot <= 0) return 0;
  const sqrtT = Math.sqrt(t);
  const d1 = bsD1(spot, strike, r, sigma, t);
  const d2 = d1 - sigma * sqrtT;
  const npd1 = normPdf(d1);
  const discount = Math.exp(-r * t);

  if (optionType === 'call') {
    return (
      (-(spot * npd1 * sigma) / (2 * sqrtT) -
        r * strike * discount * normCdf(d2)) /
      365
    );
  }
  return (
    (-(spot * npd1 * sigma) / (2 * sqrtT) +
      r * strike * discount * normCdf(-d2)) /
    365
  );
}

/** Vega per 1 percentage-point IV move. */
export function bsVega(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
): number {
  if (sigma <= 0 || t <= 0 || spot <= 0) return 0;
  const d1 = bsD1(spot, strike, r, sigma, t);
  return (spot * normPdf(d1) * Math.sqrt(t)) / 100;
}

/** Rho per 1 percentage-point rate move. */
export function bsRho(
  spot: number,
  strike: number,
  r: number,
  sigma: number,
  t: number,
  optionType: 'call' | 'put',
): number {
  if (sigma <= 0 || t <= 0 || spot <= 0) return 0;
  const d2 = bsD2(spot, strike, r, sigma, t);
  const discount = Math.exp(-r * t);
  if (optionType === 'call') {
    return (strike * t * discount * normCdf(d2)) / 100;
  }
  return (-strike * t * discount * normCdf(-d2)) / 100;
}

// ---------------------------------------------------------------------------
// Composite: full-strategy P&L and Greeks at a given underlying price
// ---------------------------------------------------------------------------

/**
 * BS-priced P&L for the entire strategy at a given (underlyingPrice, timeToExpiry).
 * A what-if's `ivShift` is added to every leg's IV and floored at zero, as the
 * server's `compute_current_curve` floors it.
 */
export function strategyPnlAtPrice(
  legs: LegParams[],
  underlyingPrice: number,
  t: number,
  r: number,
  ivShift = 0,
): number {
  let total = 0;
  for (const leg of legs) {
    const value = bsPrice(
      underlyingPrice,
      leg.strike,
      r,
      Math.max(leg.iv + ivShift, 0),
      t,
      leg.optionType,
    );
    const pnl =
      leg.position === 'long'
        ? (value - leg.premium) * leg.quantity
        : (leg.premium - value) * leg.quantity;
    total += pnl;
  }
  return total;
}

/** Sum of a specific Greek across all legs at a given underlying price. */
export function strategyGreekAtPrice(
  legs: LegParams[],
  underlyingPrice: number,
  t: number,
  r: number,
  greek: GreekName,
): number {
  let total = 0;
  for (const leg of legs) {
    const sign = leg.position === 'long' ? 1 : -1;
    let value: number;
    switch (greek) {
      case 'delta':
        value = bsDelta(underlyingPrice, leg.strike, r, leg.iv, t, leg.optionType);
        break;
      case 'gamma':
        value = bsGamma(underlyingPrice, leg.strike, r, leg.iv, t);
        break;
      case 'theta':
        value = bsTheta(underlyingPrice, leg.strike, r, leg.iv, t, leg.optionType);
        break;
      case 'vega':
        value = bsVega(underlyingPrice, leg.strike, r, leg.iv, t);
        break;
      case 'rho':
        value = bsRho(underlyingPrice, leg.strike, r, leg.iv, t, leg.optionType);
        break;
    }
    total += value * sign * leg.quantity;
  }
  return total;
}
