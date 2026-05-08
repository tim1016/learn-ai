# BS-003 — Black-Scholes Delta (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid sharing
the same input parameters as BS-001/BS-002. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Delta for a call = e^(-qT)·N(d1).
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).delta`.

**Layer 3 — Independent numerical oracle:** py_vollib==1.0.1.
`py_vollib.black_scholes.greeks.analytical.delta(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Delta (call) = e^(-qT) · N(d1)
Delta (put)  = e^(-qT) · (N(d1) - 1)

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      disc_q = e^(-qT)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Note: argument ORDER differs from bs_european_price — volatility before rate.
Returns BSGreeks.delta (dimensionless, range [-1, 1]).

## Oracle

Library: py_vollib==1.0.1
Citation: py_vollib.black_scholes.greeks.analytical.delta(flag, S, K, t, r, sigma)

Unit agreement (2026-05-08): py_vollib delta and our canonical delta are
identical values — dimensionless N(d1)·disc_q. No unit conversion needed.

## QuantLib Canary for Delta

The existing `test_bs_cross_engine_parity.py` validates price parity between
our canonical and QuantLib at atol=1e-10. Delta parity is pending separate
cross-engine verification. This fixture pins our canonical against py_vollib
as the primary independent oracle for delta.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-001: cross-library float64 comparison. Observed max
abs delta error across 180 cases is < 1e-15.

## Units

delta: dimensionless (probability-like, range [-1, 1])
No unit declaration needed — delta has no canonical scaling ambiguity.

## Known Limitations

- Call only. Put delta in a future BS-003b fixture.
- Zero-dividend (q=0).
- No near-zero-TTM cases (handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-003 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: py_vollib==1.0.1
Script: scripts/fixture_generators/bs_greeks.py
Justification: Initial BS-003 call delta fixture using py_vollib oracle
