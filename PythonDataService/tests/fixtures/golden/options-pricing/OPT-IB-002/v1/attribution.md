# OPT-IB-002 — Implied Volatility: IBKR Reported vs NR/Brent BSM Solver

Generated: 2026-05-08
Oracle: vendor_observed — IBKR TWS API modelGreeks.impliedVol
Canonical: PythonDataService/app/volatility/solver.py::implied_volatility

## Formula

Our solver uses a three-stage cascade:
  1. Newton-Raphson with vega step (primary; quadratic convergence)
  2. QuantLib impliedVolatility (T ≥ 1 calendar day only)
  3. scipy.optimize.brentq fallback ([MIN_IV=0.005, MAX_IV=5.0])

Solves for σ such that BSM(S, K, T, r, q, σ) = ibkr_model_price.

Both the oracle and our solver invert the same price: modelGreeks.optPrice
(the option price IBKR's model used to back out impliedVol). Using mid-price
as input would compare two quantities from different prices and is incorrect.

Seed: Brenner-Subrahmanyam approximation σ₀ ≈ √(2π/T) · price/spot for ATM.

Reference: Hull §19.11 (IV); Brent (1973) §4; Brenner-Subrahmanyam (1988) FAJ.
Solver source: app/volatility/solver.py (canonical per docs/math-sources-of-truth.md).

## Input data provenance

Snapshot: snapshot_20260508_153906.arrow
Captured: 2026-05-08 15:39:06 UTC
Capture script: scripts/capture_ibkr_snapshot.py

Underlying: SPY
Expirations: 2026-05-15, 2026-05-18, 2026-05-19, 2026-05-20, 2026-05-21, 2026-05-22, 2026-05-29, 2026-06-05, 2026-06-12, 2026-06-18, 2026-06-26, 2026-06-30, 2026-07-17, 2026-07-31 (14 expiry/ies)
Contracts: 2332 (calls + puts, strikes within ±10% of spot)
Rate: 0.0525 (continuously compounded, ~Fed Funds at capture)
Dividend: 0.0130 (continuously compounded SPY trailing yield at capture)

## Oracle

IBKR's modelGreeks.impliedVol from a standard option market-data request
(reqMktData with empty genericTickList — TWS returns modelGreeks automatically
for options). modelGreeks.optPrice is the model price IBKR backed this IV from.
IBKR's methodology is proprietary; their model may use discrete dividends or
adjustments not present in our pure BSM. The tolerance floor is set accordingly.

## Oracle value range

IBKR IV range in this snapshot: [0.1080, 0.3195]

## Tolerance

atol=1e-3, rtol=0.0

Both IBKR and our solver invert BSM against modelGreeks.optPrice; the 1e-3
floor accounts for IBKR's proprietary model adjustments (discrete dividends,
calibration) vs our pure continuous-dividend BSM. Contracts with bid < $0.05,
ask ≤ 0, crossed quotes, or IBKR IV outside [0.05, 2.0] excluded at capture.

## Justification

Initial IBKR IV fixture, snapshot 20260508_153906

## SHA-256

input.arrow:  2353ab56a3e88a6c5b27f213e95c29db511d6f5917c54ead19b19ea5564e79b2
output.arrow: 17c0ed5eea02ba866d6ba5a30d7d88500639d61ab9cf8be0c63cc5915f9f20bd
