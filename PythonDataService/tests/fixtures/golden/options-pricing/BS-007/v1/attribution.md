# BS-007 — Black-Scholes Rho (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Rho (call) = K·T·e^(-rT)·N(d2),
divided by 100 for per-1%-rate-move units.
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).rho`.

**Layer 3 — Independent numerical oracle:** py_vollib==1.0.1.
`py_vollib.black_scholes.greeks.analytical.rho(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Rho (call) = K·T·e^(-rT)·N(d2) / 100

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      d2 = d1 - σ·√T
(Division by 100 converts from per-unit-rate to per-1%-rate-move.)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Returns BSGreeks.rho (per 1% move in the risk-free rate; positive for long calls).

## Oracle

Library: py_vollib==1.0.1
Citation: py_vollib.black_scholes.greeks.analytical.rho(flag, S, K, t, r, sigma)

## Unit Declaration

rho: per 1%-rate move (value already divided by 100 in both py_vollib and canonical).
A rho of 0.10 means the option gains approximately $0.10 per 1% rise in the risk-free rate.
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all 180 cases. No unit conversion applied.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across 180 cases is < 1e-15.

## Known Limitations

- Call only. Put rho is negative and covered by a future BS-007b fixture.
- Zero-dividend (q=0).
- Single risk-free rate [0.05]; rho sensitivity to rate level not swept.
- No near-zero-TTM cases (rho → 0 near expiry; handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-007 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: py_vollib==1.0.1
Script: scripts/fixture_generators/bs_greeks_extended.py
(initial generation)
