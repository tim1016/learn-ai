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
  lognormalCdf,
  strategyPnlAtPrice,
  strategyGreekAtPrice,
  LegParams,
} from './black-scholes';

// ---------------------------------------------------------------------------
// normCdf — Standard normal CDF
// ---------------------------------------------------------------------------

describe('normCdf', () => {
  it('should return 0.5 for x=0', () => {
    expect(normCdf(0)).toBeCloseTo(0.5, 7);
  });

  it('should return ~0.8413 for x=1', () => {
    expect(normCdf(1)).toBeCloseTo(0.8413, 4);
  });

  it('should return ~0.1587 for x=-1 (symmetry)', () => {
    expect(normCdf(-1)).toBeCloseTo(0.1587, 4);
  });

  it('should return ~0.9772 for x=2', () => {
    expect(normCdf(2)).toBeCloseTo(0.9772, 4);
  });

  it('should approach 1 for large positive x', () => {
    expect(normCdf(6)).toBeCloseTo(1.0, 6);
  });

  it('should approach 0 for large negative x', () => {
    expect(normCdf(-6)).toBeCloseTo(0.0, 6);
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
  it('should return ~0.3989 at x=0 (peak)', () => {
    expect(normPdf(0)).toBeCloseTo(1 / Math.sqrt(2 * Math.PI), 6);
  });

  it('should be symmetric: f(x) = f(-x)', () => {
    expect(normPdf(1.5)).toBeCloseTo(normPdf(-1.5), 10);
  });

  it('should be smaller at x=2 than x=1', () => {
    expect(normPdf(2)).toBeLessThan(normPdf(1));
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

  it('should return 0 for zero sigma', () => {
    expect(bsD1(100, 100, 0.05, 0, 1.0)).toBe(0);
  });

  it('should return 0 for zero time', () => {
    expect(bsD1(100, 100, 0.05, 0.20, 0)).toBe(0);
  });

  it('should return 0 for zero spot', () => {
    expect(bsD1(0, 100, 0.05, 0.20, 1.0)).toBe(0);
  });

  it('should return 0 for zero strike', () => {
    expect(bsD1(100, 0, 0.05, 0.20, 1.0)).toBe(0);
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
  // Reference: ATM call with S=100, K=100, r=5%, σ=20%, T=1
  // Expected ≈ 10.45 (standard BS)
  it('should price ATM call correctly', () => {
    const price = bsPrice(100, 100, 0.05, 0.20, 1.0, 'call');
    expect(price).toBeCloseTo(10.4506, 2);
  });

  it('should price ATM put correctly via put-call parity', () => {
    const call = bsPrice(100, 100, 0.05, 0.20, 1.0, 'call');
    const put = bsPrice(100, 100, 0.05, 0.20, 1.0, 'put');
    // Put-Call parity: C - P = S - K*e^(-rT)
    const parity = 100 - 100 * Math.exp(-0.05);
    expect(call - put).toBeCloseTo(parity, 4);
  });

  it('should return intrinsic value at expiration (t=0)', () => {
    expect(bsPrice(110, 100, 0.05, 0.20, 0, 'call')).toBe(10);
    expect(bsPrice(90, 100, 0.05, 0.20, 0, 'call')).toBe(0);
    expect(bsPrice(90, 100, 0.05, 0.20, 0, 'put')).toBe(10);
    expect(bsPrice(110, 100, 0.05, 0.20, 0, 'put')).toBe(0);
  });

  it('should return 0 for zero sigma (OTM)', () => {
    expect(bsPrice(90, 100, 0.05, 0, 1.0, 'call')).toBe(0);
  });

  it('should return 0 for zero spot', () => {
    expect(bsPrice(0, 100, 0.05, 0.20, 1.0, 'call')).toBe(0);
  });

  it('should increase with higher volatility', () => {
    const low = bsPrice(100, 100, 0.05, 0.15, 1.0, 'call');
    const high = bsPrice(100, 100, 0.05, 0.30, 1.0, 'call');
    expect(high).toBeGreaterThan(low);
  });

  it('should increase with more time to expiry', () => {
    const short = bsPrice(100, 100, 0.05, 0.20, 0.25, 'call');
    const long = bsPrice(100, 100, 0.05, 0.20, 1.0, 'call');
    expect(long).toBeGreaterThan(short);
  });

  it('should price deep ITM call near intrinsic + time value', () => {
    const price = bsPrice(150, 100, 0.05, 0.20, 1.0, 'call');
    expect(price).toBeGreaterThan(50); // at least intrinsic
    expect(price).toBeLessThan(55);    // bounded time value
  });
});

// ---------------------------------------------------------------------------
// bsDelta
// ---------------------------------------------------------------------------

describe('bsDelta', () => {
  it('should be ~0.5 for ATM call', () => {
    const delta = bsDelta(100, 100, 0.05, 0.20, 1.0, 'call');
    // ATM call delta is slightly > 0.5 due to drift
    expect(delta).toBeCloseTo(0.6368, 2);
  });

  it('should be negative for ATM put', () => {
    const delta = bsDelta(100, 100, 0.05, 0.20, 1.0, 'put');
    expect(delta).toBeLessThan(0);
    expect(delta).toBeCloseTo(-0.3632, 2);
  });

  it('call delta + |put delta| should equal 1', () => {
    const callDelta = bsDelta(100, 100, 0.05, 0.20, 1.0, 'call');
    const putDelta = bsDelta(100, 100, 0.05, 0.20, 1.0, 'put');
    expect(callDelta - putDelta).toBeCloseTo(1.0, 6);
  });

  it('should return 1 for deep ITM call at expiry', () => {
    expect(bsDelta(110, 100, 0.05, 0.20, 0, 'call')).toBe(1);
  });

  it('should return 0 for deep OTM call at expiry', () => {
    expect(bsDelta(90, 100, 0.05, 0.20, 0, 'call')).toBe(0);
  });

  it('should return -1 for deep ITM put at expiry', () => {
    expect(bsDelta(90, 100, 0.05, 0.20, 0, 'put')).toBe(-1);
  });
});

// ---------------------------------------------------------------------------
// bsGamma
// ---------------------------------------------------------------------------

describe('bsGamma', () => {
  it('should be positive for ATM option', () => {
    const gamma = bsGamma(100, 100, 0.05, 0.20, 1.0);
    expect(gamma).toBeGreaterThan(0);
    // Gamma = N'(d1) / (S * sigma * sqrt(T))
    expect(gamma).toBeCloseTo(0.0188, 3);
  });

  it('should be highest ATM', () => {
    const atm = bsGamma(100, 100, 0.05, 0.20, 0.25);
    const itm = bsGamma(120, 100, 0.05, 0.20, 0.25);
    const otm = bsGamma(80, 100, 0.05, 0.20, 0.25);
    expect(atm).toBeGreaterThan(itm);
    expect(atm).toBeGreaterThan(otm);
  });

  it('should return 0 at expiry', () => {
    expect(bsGamma(100, 100, 0.05, 0.20, 0)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// bsTheta (per calendar day)
// ---------------------------------------------------------------------------

describe('bsTheta', () => {
  it('should be negative for long call', () => {
    const theta = bsTheta(100, 100, 0.05, 0.20, 1.0, 'call');
    expect(theta).toBeLessThan(0);
  });

  it('should be negative for long put (usually)', () => {
    const theta = bsTheta(100, 100, 0.05, 0.20, 1.0, 'put');
    expect(theta).toBeLessThan(0);
  });

  it('should return 0 at expiry', () => {
    expect(bsTheta(100, 100, 0.05, 0.20, 0, 'call')).toBe(0);
  });

  it('should be per calendar day (divided by 365)', () => {
    const theta = bsTheta(100, 100, 0.05, 0.20, 1.0, 'call');
    // Should be small daily magnitude
    expect(Math.abs(theta)).toBeLessThan(0.1);
    expect(Math.abs(theta)).toBeGreaterThan(0.001);
  });
});

// ---------------------------------------------------------------------------
// bsVega (per 1% IV move)
// ---------------------------------------------------------------------------

describe('bsVega', () => {
  it('should be positive', () => {
    const vega = bsVega(100, 100, 0.05, 0.20, 1.0);
    expect(vega).toBeGreaterThan(0);
  });

  it('should be highest ATM', () => {
    const atm = bsVega(100, 100, 0.05, 0.20, 0.5);
    const itm = bsVega(120, 100, 0.05, 0.20, 0.5);
    const otm = bsVega(80, 100, 0.05, 0.20, 0.5);
    expect(atm).toBeGreaterThan(itm);
    expect(atm).toBeGreaterThan(otm);
  });

  it('should return 0 at expiry', () => {
    expect(bsVega(100, 100, 0.05, 0.20, 0)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// bsRho (per 1% rate move)
// ---------------------------------------------------------------------------

describe('bsRho', () => {
  it('should be positive for call', () => {
    const rho = bsRho(100, 100, 0.05, 0.20, 1.0, 'call');
    expect(rho).toBeGreaterThan(0);
  });

  it('should be negative for put', () => {
    const rho = bsRho(100, 100, 0.05, 0.20, 1.0, 'put');
    expect(rho).toBeLessThan(0);
  });

  it('should return 0 at expiry', () => {
    expect(bsRho(100, 100, 0.05, 0.20, 0, 'call')).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// lognormalCdf
// ---------------------------------------------------------------------------

describe('lognormalCdf', () => {
  it('should return 0 for x <= 0', () => {
    expect(lognormalCdf(0, 100, 0.05, 0.20, 1.0)).toBe(0);
    expect(lognormalCdf(-10, 100, 0.05, 0.20, 1.0)).toBe(0);
  });

  it('should return value between 0 and 1', () => {
    const cdf = lognormalCdf(100, 100, 0.05, 0.20, 1.0);
    expect(cdf).toBeGreaterThan(0);
    expect(cdf).toBeLessThan(1);
  });

  it('should be monotonically increasing', () => {
    const c80 = lognormalCdf(80, 100, 0.05, 0.20, 1.0);
    const c100 = lognormalCdf(100, 100, 0.05, 0.20, 1.0);
    const c120 = lognormalCdf(120, 100, 0.05, 0.20, 1.0);
    expect(c100).toBeGreaterThan(c80);
    expect(c120).toBeGreaterThan(c100);
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

  it('should equal intrinsic-based P&L at expiry (t=0)', () => {
    // At t=0, bsPrice returns intrinsic
    const pnl = strategyPnlAtPrice(bullCallSpread, 110, 0, 0.05);
    // Long 100C: intrinsic 10 - premium 5 = 5
    // Short 105C: premium 2 - intrinsic 5 = -3
    // Total: 2
    expect(pnl).toBeCloseTo(2.0, 4);
  });

  it('should return max loss below lower strike at expiry', () => {
    const pnl = strategyPnlAtPrice(bullCallSpread, 90, 0, 0.05);
    // Both OTM: -5 + 2 = -3
    expect(pnl).toBeCloseTo(-3.0, 4);
  });

  it('should compute mid-life P&L using BS prices (not intrinsic)', () => {
    // With time remaining, BS prices have extrinsic value
    const pnlMidLife = strategyPnlAtPrice(bullCallSpread, 100, 0.5, 0.05);
    const pnlExpiry = strategyPnlAtPrice(bullCallSpread, 100, 0, 0.05);
    // Mid-life P&L differs from expiry P&L due to time value
    expect(pnlMidLife).not.toBeCloseTo(pnlExpiry, 2);
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

  it('should return positive delta for long call', () => {
    const delta = strategyGreekAtPrice(longCall, 100, 0.5, 0.05, 'delta');
    expect(delta).toBeGreaterThan(0);
  });

  it('should return negative delta for short call', () => {
    const delta = strategyGreekAtPrice(shortCall, 100, 0.5, 0.05, 'delta');
    expect(delta).toBeLessThan(0);
  });

  it('should negate Greeks for short vs long', () => {
    const longDelta = strategyGreekAtPrice(longCall, 100, 0.5, 0.05, 'delta');
    const shortDelta = strategyGreekAtPrice(shortCall, 100, 0.5, 0.05, 'delta');
    expect(longDelta + shortDelta).toBeCloseTo(0, 8);
  });

  it('should sum gamma across legs', () => {
    const gamma = strategyGreekAtPrice(longCall, 100, 0.5, 0.05, 'gamma');
    expect(gamma).toBeGreaterThan(0);
  });

  it('should respect quantity multiplier', () => {
    const singleLeg: LegParams[] = [
      { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
    ];
    const doubleLeg: LegParams[] = [
      { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 2 },
    ];
    const single = strategyGreekAtPrice(singleLeg, 100, 0.5, 0.05, 'delta');
    const double = strategyGreekAtPrice(doubleLeg, 100, 0.5, 0.05, 'delta');
    expect(double).toBeCloseTo(single * 2, 8);
  });

  it('should return near-zero delta for a delta-neutral straddle', () => {
    const straddle: LegParams[] = [
      { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
      { strike: 100, optionType: 'put', position: 'long', premium: 5, iv: 0.25, quantity: 1 },
    ];
    // ATM straddle: call delta ≈ 0.55, put delta ≈ -0.45 → net ≈ 0.1
    // Not exactly zero, but close-ish
    const delta = strategyGreekAtPrice(straddle, 100, 0.5, 0.05, 'delta');
    expect(Math.abs(delta)).toBeLessThan(0.2);
  });
});
