/**
 * Expiry payoff of an options strategy: its value per share at a price, its
 * greatest gain and loss, and the prices it breaks even at.
 *
 * Formula: payoff(p) = Σ_leg sign · qty · (intrinsic(p) − premium), p ≥ 0. It is
 *   piecewise linear and turns only at the strikes, so its extremes sit at p = 0
 *   and at the strikes, and above the highest strike it runs on with slope
 *   (net long call quantity): a gain without limit where that is above zero, a
 *   loss without limit where it is below. A breakeven is a price above zero
 *   where the payoff changes sign. No price window enters.
 * Reference: Hull, Options Futures and Other Derivatives (10e), §11–12 (payoff diagrams).
 * Canonical implementation: PythonDataService/app/services/strategy_engine.py
 *   (`compute_max_profit_loss`, `find_breakevens`). This copy is ADR 0068
 *   exception 1: the Strategy Builder re-reads these on every leg edit.
 * Validated against: Frontend/src/app/utils/strategy-payoff.parity.spec.ts, against
 *   Frontend/src/testing/payoff-parity/cases.json, which
 *   PythonDataService/scripts/regenerate_strategy_payoff_parity_fixture.py writes
 *   from the canonical and PythonDataService/tests/contracts/test_strategy_payoff_parity_fixture.py
 *   re-checks against it.
 *
 * Unlike the canonical this copy does not round: the canonical rounds to 4 dp,
 * and the parity test bounds the difference.
 */

export interface PayoffLeg {
  strike: number;
  optionType: 'call' | 'put';
  position: 'long' | 'short';
  premium: number;
  quantity: number;
}

/** Per share. An infinite side is a gain or loss with no limit. */
export interface PayoffExtremes {
  maxProfit: number;
  maxLoss: number;
}

/** The strategy's payoff per share at one underlying price at expiry. */
export function payoffAtExpiry(legs: readonly PayoffLeg[], price: number): number {
  let total = 0;
  for (const leg of legs) {
    const intrinsic = leg.optionType === 'call'
      ? Math.max(price - leg.strike, 0)
      : Math.max(leg.strike - price, 0);
    total += leg.position === 'long'
      ? (intrinsic - leg.premium) * leg.quantity
      : (leg.premium - intrinsic) * leg.quantity;
  }
  return total;
}

/** Price and payoff at 0 and at each distinct strike, ascending. Between two of them the payoff is a line. */
function payoffKnots(legs: readonly PayoffLeg[]): { price: number; pnl: number }[] {
  const prices = [...new Set([0, ...legs.map(l => l.strike)])].sort((a, b) => a - b);
  return prices.map(price => ({ price, pnl: payoffAtExpiry(legs, price) }));
}

/** Slope of the payoff above the highest strike: the net long call quantity. Puts are worthless up there. */
function tailSlope(legs: readonly PayoffLeg[]): number {
  let slope = 0;
  for (const leg of legs) {
    if (leg.optionType === 'call') slope += (leg.position === 'long' ? 1 : -1) * leg.quantity;
  }
  return slope;
}

/** Greatest gain and loss of the expiry payoff over every price at or above zero. */
export function payoffExtremes(legs: readonly PayoffLeg[]): PayoffExtremes {
  const values = payoffKnots(legs).map(k => k.pnl);
  const slope = tailSlope(legs);
  return {
    maxProfit: slope > 0 ? Number.POSITIVE_INFINITY : Math.max(...values),
    maxLoss: slope < 0 ? Number.NEGATIVE_INFINITY : Math.min(...values),
  };
}

/**
 * Prices above zero where the expiry payoff changes sign. A price where it only
 * touches zero and turns back is not one. Where it sits at zero over a stretch
 * and leaves it on the other side, the breakeven is the price where it first
 * reaches zero.
 */
export function payoffBreakevens(legs: readonly PayoffLeg[]): number[] {
  const slope = tailSlope(legs);
  const breakevens: number[] = [];
  let last: { price: number; pnl: number } | null = null; // the latest knot whose payoff is not zero
  let zeroFrom: number | null = null; // first price of the zeros since `last`

  for (const knot of payoffKnots(legs)) {
    if (knot.pnl === 0) {
      zeroFrom ??= knot.price;
      continue;
    }
    if (last !== null && (last.pnl > 0) !== (knot.pnl > 0)) {
      breakevens.push(zeroFrom ?? last.price - last.pnl * (knot.price - last.price) / (knot.pnl - last.pnl));
    }
    last = knot;
    zeroFrom = null;
  }

  // Above the highest strike the payoff is a line of the tail slope, or holds
  // the highest strike's value when there is none: a flat tail never crosses.
  if (last !== null && slope !== 0 && (last.pnl > 0) !== (slope > 0)) {
    breakevens.push(zeroFrom ?? last.price - last.pnl / slope);
  }
  return breakevens;
}
