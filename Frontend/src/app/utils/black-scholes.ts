/**
 * Client-side Black-Scholes pricing and Greeks.
 * Pure math — zero dependencies.
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

/** Standard normal CDF — Abramowitz & Stegun approximation (error < 7.5e-8). */
export function normCdf(x: number): number {
  if (x < -8) return 0;
  if (x > 8) return 1;
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;
  const sign = x < 0 ? -1 : 1;
  const absX = Math.abs(x);
  const t = 1.0 / (1.0 + p * absX);
  const y =
    1.0 -
    ((((a5 * t + a4) * t + a3) * t + a2) * t + a1) *
      t *
      Math.exp((-absX * absX) / 2);
  return 0.5 * (1.0 + sign * y);
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
  if (t <= 0) {
    // At expiration — intrinsic value
    return optionType === 'call'
      ? Math.max(spot - strike, 0)
      : Math.max(strike - spot, 0);
  }
  if (sigma <= 0 || spot <= 0 || strike <= 0) return 0;

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
// Lognormal CDF — probability that S_T < x
// ---------------------------------------------------------------------------

export function lognormalCdf(
  x: number,
  spot: number,
  r: number,
  sigma: number,
  t: number,
): number {
  if (x <= 0 || spot <= 0 || sigma <= 0 || t <= 0) return 0;
  const d2 = (Math.log(x / spot) - (r - 0.5 * sigma * sigma) * t) /
    (sigma * Math.sqrt(t));
  return normCdf(d2);
}

// ---------------------------------------------------------------------------
// Composite: full-strategy P&L and Greeks at a given underlying price
// ---------------------------------------------------------------------------

/** BS-priced P&L for the entire strategy at a given (underlyingPrice, timeToExpiry). */
export function strategyPnlAtPrice(
  legs: LegParams[],
  underlyingPrice: number,
  t: number,
  r: number,
): number {
  let total = 0;
  for (const leg of legs) {
    const value = bsPrice(
      underlyingPrice,
      leg.strike,
      r,
      leg.iv,
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
