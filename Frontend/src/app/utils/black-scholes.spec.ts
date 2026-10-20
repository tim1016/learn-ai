/**
 * Black-Scholes pricing and Greeks — unit tests.
 *
 * Reference values computed from standard BS formulas.
 * Typical reference: Hull "Options, Futures, and Other Derivatives"
 *
 * Standard params used across tests:
 *   spot=100, strike=100, r=0.05, sigma=0.20, t=1.0 (ATM, 1-year, 20% vol)
 */
import {
  normCdf,
  normPdf,
  bsD1,
  bsD2,
  bsPrice,
  bsDelta,
  bsGamma,
  bsTheta,
  bsVega,
  bsRho,
  strategyPnlAtPrice,
  strategyGreekAtPrice,
  LegParams,
} from './black-scholes';

// ---------------------------------------------------------------------------
// normCdf — Standard normal CDF
// ---------------------------------------------------------------------------

describe('normCdf', () => {
  it.each([
    ['x=0 (0.5)', 0, 0.5, 7],
    ['x=1 (~0.8413)', 1, 0.8413, 4],
    ['x=-1 (~0.1587, symmetry)', -1, 0.1587, 4],
    ['x=2 (~0.9772)', 2, 0.9772, 4],
    ['x=6 (approaches 1 for large positive x)', 6, 1.0, 6],
    ['x=-6 (approaches 0 for large negative x)', -6, 0.0, 6],
  ])('matches the reference value at %s', (_name, x, expected, digits) => {
    expect(normCdf(x)).toBeCloseTo(expected, digits);
  });

  it('should satisfy Phi(x) + Phi(-x) = 1', () => {
    for (const x of [0.5, 1.0, 1.5, 2.0, 3.0]) {
      expect(normCdf(x) + normCdf(-x)).toBeCloseTo(1.0, 6);
    }
  });
});

// ---------------------------------------------------------------------------
// normPdf — Standard normal PDF
// ---------------------------------------------------------------------------

describe('normPdf', () => {
  it('peaks at x=0, is symmetric and falls off away from the peak', () => {
    expect.soft(normPdf(0), 'should return ~0.3989 at x=0 (peak)').toBeCloseTo(1 / Math.sqrt(2 * Math.PI), 6);
    expect.soft(normPdf(1.5), 'should be symmetric: f(x) = f(-x)').toBeCloseTo(normPdf(-1.5), 10);
    expect.soft(normPdf(2), 'should be smaller at x=2 than x=1').toBeLessThan(normPdf(1));
  });
});

// ---------------------------------------------------------------------------
// bsD1 / bsD2
// ---------------------------------------------------------------------------

describe('bsD1', () => {
  it('should compute correct d1 for ATM option', () => {
    // d1 = [ln(100/100) + (0.05 + 0.5*0.04)*1] / (0.20*1) = 0.07/0.20 = 0.35
    const d1 = bsD1(100, 100, 0.05, 0.20, 1.0);
    expect(d1).toBeCloseTo(0.35, 4);
  });

  it.each([
    ['zero sigma', 100, 100, 0.05, 0, 1.0],
    ['zero time', 100, 100, 0.05, 0.20, 0],
    ['zero spot', 0, 100, 0.05, 0.20, 1.0],
    ['zero strike', 100, 0, 0.05, 0.20, 1.0],
  ])('should return 0 for %s', (_name, spot, strike, r, sigma, t) => {
    expect(bsD1(spot, strike, r, sigma, t)).toBe(0);
  });
});

describe('bsD2', () => {
  it('should equal d1 - sigma*sqrt(t)', () => {
    const d1 = bsD1(100, 100, 0.05, 0.20, 1.0);
    const d2 = bsD2(100, 100, 0.05, 0.20, 1.0);
    expect(d2).toBeCloseTo(d1 - 0.20, 6);
  });
});

// ---------------------------------------------------------------------------
// bsPrice — Option pricing
// ---------------------------------------------------------------------------

describe('bsPrice', () => {
  it('prices the reference ATM call, put-call parity and a deep ITM call', () => {
    // Reference: ATM call with S=100, K=100, r=5%, σ=20%, T=1
    // Expected ≈ 10.45 (standard BS)
    const call = bsPrice(100, 100, 0.05, 0.20, 1.0, 'call');
    expect.soft(call, 'should price ATM call correctly').toBeCloseTo(10.4506, 2);

    const put = bsPrice(100, 100, 0.05, 0.20, 1.0, 'put');
    // Put-Call parity: C - P = S - K*e^(-rT)
    const parity = 100 - 100 * Math.exp(-0.05);
    expect.soft(call - put, 'should price ATM put correctly via put-call parity').toBeCloseTo(parity, 4);

    const deepItm = bsPrice(150, 100, 0.05, 0.20, 1.0, 'call');
    expect.soft(deepItm, 'should price deep ITM call near intrinsic + time value: at least intrinsic').toBeGreaterThan(50);
    expect.soft(deepItm, 'should price deep ITM call near intrinsic + time value: bounded time value').toBeLessThan(55);
  });

  it('prices the degenerate inputs at intrinsic value or zero', () => {
    expect.soft(bsPrice(110, 100, 0.05, 0.20, 0, 'call'), 'should return intrinsic value at expiration (t=0): ITM call').toBe(10);
    expect.soft(bsPrice(90, 100, 0.05, 0.20, 0, 'call'), 'should return intrinsic value at expiration (t=0): OTM call').toBe(0);
    expect.soft(bsPrice(90, 100, 0.05, 0.20, 0, 'put'), 'should return intrinsic value at expiration (t=0): ITM put').toBe(10);
    expect.soft(bsPrice(110, 100, 0.05, 0.20, 0, 'put'), 'should return intrinsic value at expiration (t=0): OTM put').toBe(0);
    expect.soft(bsPrice(90, 100, 0.05, 0, 1.0, 'call'), 'should return intrinsic value for zero sigma: OTM call').toBe(0);
    expect.soft(bsPrice(110, 100, 0.05, 0, 1.0, 'call'), 'should return intrinsic value for zero sigma: ITM call').toBe(10);
    expect.soft(bsPrice(90, 100, 0.05, 0, 1.0, 'put'), 'should return intrinsic value for zero sigma: ITM put').toBe(10);
    expect.soft(bsPrice(0, 100, 0.05, 0.20, 1.0, 'call'), 'should return 0 for zero spot').toBe(0);
  });

  it('prices higher with higher volatility and with more time to expiry', () => {
    const low = bsPrice(100, 100, 0.05, 0.15, 1.0, 'call');
    const high = bsPrice(100, 100, 0.05, 0.30, 1.0, 'call');
    expect.soft(high, 'should increase with higher volatility').toBeGreaterThan(low);

    const short = bsPrice(100, 100, 0.05, 0.20, 0.25, 'call');
    const long = bsPrice(100, 100, 0.05, 0.20, 1.0, 'call');
    expect.soft(long, 'should increase with more time to expiry').toBeGreaterThan(short);
  });
});

// ---------------------------------------------------------------------------
// bsDelta
// ---------------------------------------------------------------------------

describe('bsDelta', () => {
  it('gives the ATM call and put their reference deltas, which differ by 1', () => {
    const callDelta = bsDelta(100, 100, 0.05, 0.20, 1.0, 'call');
    // ATM call delta is slightly > 0.5 due to drift
    expect.soft(callDelta, 'should be ~0.5 for ATM call').toBeCloseTo(0.6368, 2);

    const putDelta = bsDelta(100, 100, 0.05, 0.20, 1.0, 'put');
    expect.soft(putDelta, 'should be negative for ATM put').toBeLessThan(0);
    expect.soft(putDelta, 'should be negative for ATM put: ~-0.3632').toBeCloseTo(-0.3632, 2);

    expect.soft(callDelta - putDelta, 'call delta + |put delta| should equal 1').toBeCloseTo(1.0, 6);
  });

  it.each<[string, number, 'call' | 'put', number]>([
    ['deep ITM call: 1', 110, 'call', 1],
    ['deep OTM call: 0', 90, 'call', 0],
    ['deep ITM put: -1', 90, 'put', -1],
  ])('should return the intrinsic delta at expiry for a %s', (_name, spot, optionType, expected) => {
    expect(bsDelta(spot, 100, 0.05, 0.20, 0, optionType)).toBe(expected);
  });
});

// ---------------------------------------------------------------------------
// bsGamma
// ---------------------------------------------------------------------------

describe('bsGamma', () => {
  it('is positive and highest ATM, and zero at expiry', () => {
    const gamma = bsGamma(100, 100, 0.05, 0.20, 1.0);
    expect.soft(gamma, 'should be positive for ATM option').toBeGreaterThan(0);
    // Gamma = N'(d1) / (S * sigma * sqrt(T))
    expect.soft(gamma, 'should be ~0.0188 for ATM option').toBeCloseTo(0.0188, 3);

    const atm = bsGamma(100, 100, 0.05, 0.20, 0.25);
    const itm = bsGamma(120, 100, 0.05, 0.20, 0.25);
    const otm = bsGamma(80, 100, 0.05, 0.20, 0.25);
    expect.soft(atm, 'should be highest ATM: above ITM').toBeGreaterThan(itm);
    expect.soft(atm, 'should be highest ATM: above OTM').toBeGreaterThan(otm);

    expect.soft(bsGamma(100, 100, 0.05, 0.20, 0), 'should return 0 at expiry').toBe(0);
  });
});

// ---------------------------------------------------------------------------
// bsTheta (per calendar day)
// ---------------------------------------------------------------------------

describe('bsTheta', () => {
  it('is negative for long options, a small per-calendar-day amount, and zero at expiry', () => {
    const theta = bsTheta(100, 100, 0.05, 0.20, 1.0, 'call');
    expect.soft(theta, 'should be negative for long call').toBeLessThan(0);

    const putTheta = bsTheta(100, 100, 0.05, 0.20, 1.0, 'put');
    expect.soft(putTheta, 'should be negative for long put (usually)').toBeLessThan(0);

    expect.soft(bsTheta(100, 100, 0.05, 0.20, 0, 'call'), 'should return 0 at expiry').toBe(0);

    // Should be small daily magnitude
    expect.soft(Math.abs(theta), 'should be per calendar day (divided by 365): below 0.1').toBeLessThan(0.1);
    expect.soft(Math.abs(theta), 'should be per calendar day (divided by 365): above 0.001').toBeGreaterThan(0.001);
  });
});

// ---------------------------------------------------------------------------
// bsVega (per 1% IV move)
// ---------------------------------------------------------------------------

describe('bsVega', () => {
  it('is positive and highest ATM, and zero at expiry', () => {
    const vega = bsVega(100, 100, 0.05, 0.20, 1.0);
    expect.soft(vega, 'should be positive').toBeGreaterThan(0);

    const atm = bsVega(100, 100, 0.05, 0.20, 0.5);
    const itm = bsVega(120, 100, 0.05, 0.20, 0.5);
    const otm = bsVega(80, 100, 0.05, 0.20, 0.5);
    expect.soft(atm, 'should be highest ATM: above ITM').toBeGreaterThan(itm);
    expect.soft(atm, 'should be highest ATM: above OTM').toBeGreaterThan(otm);

    expect.soft(bsVega(100, 100, 0.05, 0.20, 0), 'should return 0 at expiry').toBe(0);
  });
});

// ---------------------------------------------------------------------------
// bsRho (per 1% rate move)
// ---------------------------------------------------------------------------

describe('bsRho', () => {
  it('is positive for a call, negative for a put, and zero at expiry', () => {
    const rho = bsRho(100, 100, 0.05, 0.20, 1.0, 'call');
    expect.soft(rho, 'should be positive for call').toBeGreaterThan(0);

    const putRho = bsRho(100, 100, 0.05, 0.20, 1.0, 'put');
    expect.soft(putRho, 'should be negative for put').toBeLessThan(0);

    expect.soft(bsRho(100, 100, 0.05, 0.20, 0, 'call'), 'should return 0 at expiry').toBe(0);
  });
});

// ---------------------------------------------------------------------------
// strategyPnlAtPrice — multi-leg P&L
// ---------------------------------------------------------------------------

describe('strategyPnlAtPrice', () => {
  const bullCallSpread: LegParams[] = [
    { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
    { strike: 105, optionType: 'call', position: 'short', premium: 2, iv: 0.23, quantity: 1 },
  ];

  it('pays the intrinsic-based P&L at expiry and values mid-life with BS prices', () => {
    // At t=0, bsPrice returns intrinsic
    const pnl = strategyPnlAtPrice(bullCallSpread, 110, 0, 0.05);
    // Long 100C: intrinsic 10 - premium 5 = 5
    // Short 105C: premium 2 - intrinsic 5 = -3
    // Total: 2
    expect.soft(pnl, 'should equal intrinsic-based P&L at expiry (t=0)').toBeCloseTo(2.0, 4);

    const pnlBelow = strategyPnlAtPrice(bullCallSpread, 90, 0, 0.05);
    // Both OTM: -5 + 2 = -3
    expect.soft(pnlBelow, 'should return max loss below lower strike at expiry').toBeCloseTo(-3.0, 4);

    // With time remaining, BS prices have extrinsic value
    const pnlMidLife = strategyPnlAtPrice(bullCallSpread, 100, 0.5, 0.05);
    const pnlExpiry = strategyPnlAtPrice(bullCallSpread, 100, 0, 0.05);
    // Mid-life P&L differs from expiry P&L due to time value
    expect.soft(pnlMidLife, 'should compute mid-life P&L using BS prices (not intrinsic)').not.toBeCloseTo(pnlExpiry, 2);
  });
});

// ---------------------------------------------------------------------------
// strategyGreekAtPrice — aggregate Greeks
// ---------------------------------------------------------------------------

describe('strategyGreekAtPrice', () => {
  const longCall: LegParams[] = [
    { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
  ];

  const shortCall: LegParams[] = [
    { strike: 100, optionType: 'call', position: 'short', premium: 5, iv: 0.25, quantity: 1 },
  ];

  it('signs the Greeks by side, sums gamma, scales by quantity, and nets a straddle', () => {
    const longDelta = strategyGreekAtPrice(longCall, 100, 0.5, 0.05, 'delta');
    expect.soft(longDelta, 'should return positive delta for long call').toBeGreaterThan(0);

    const shortDelta = strategyGreekAtPrice(shortCall, 100, 0.5, 0.05, 'delta');
    expect.soft(shortDelta, 'should return negative delta for short call').toBeLessThan(0);

    expect.soft(longDelta + shortDelta, 'should negate Greeks for short vs long').toBeCloseTo(0, 8);

    const gamma = strategyGreekAtPrice(longCall, 100, 0.5, 0.05, 'gamma');
    expect.soft(gamma, 'should sum gamma across legs').toBeGreaterThan(0);

    const singleLeg: LegParams[] = [
      { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
    ];
    const doubleLeg: LegParams[] = [
      { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 2 },
    ];
    const single = strategyGreekAtPrice(singleLeg, 100, 0.5, 0.05, 'delta');
    const double = strategyGreekAtPrice(doubleLeg, 100, 0.5, 0.05, 'delta');
    expect.soft(double, 'should respect quantity multiplier').toBeCloseTo(single * 2, 8);

    const straddle: LegParams[] = [
      { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
      { strike: 100, optionType: 'put', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
    ];
    // ATM straddle: call delta ≈ 0.55, put delta ≈ -0.45 → net ≈ 0.1
    // Not exactly zero, but close-ish
    const straddleDelta = strategyGreekAtPrice(straddle, 100, 0.5, 0.05, 'delta');
    expect.soft(Math.abs(straddleDelta), 'should return near-zero delta for a delta-neutral straddle').toBeLessThan(0.2);
  });
});
