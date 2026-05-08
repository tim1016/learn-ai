# BS-005 — Black-Scholes Theta (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Theta (call) = -[S·σ·e^(-qT)·N'(d1)/(2√T)]
- r·K·e^(-rT)·N(d2) + q·S·e^(-qT)·N(d1), divided by 365 for per-calendar-day units.
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).theta`.

**Layer 3 — Independent numerical oracle:** py_vollib==1.0.1.
`py_vollib.black_scholes.greeks.analytical.theta(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Theta (call) = [-S·σ·e^(-qT)·N'(d1)/(2√T) - r·K·e^(-rT)·N(d2) + q·S·e^(-qT)·N(d1)] / 365

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      d2 = d1 - σ·√T
      N'(x) = standard normal PDF

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Returns BSGreeks.theta (per calendar day; negative for long calls due to time decay).

## Oracle

Library: py_vollib==1.0.1
Citation: py_vollib.black_scholes.greeks.analytical.theta(flag, S, K, t, r, sigma)

## Unit Declaration

theta: per calendar day (value already divided by 365 in both py_vollib and canonical).
A theta of -0.05 means the option loses approximately $0.05 per calendar day.
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all 180 cases. No unit conversion applied.
Values are negative for calls (long calls lose value to time decay).

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across 180 cases is < 1e-15.

## Known Limitations

- Call only. Put theta differs; a future BS-005b covers put theta.
- Zero-dividend (q=0).
- No near-zero-TTM cases (theta blows up near expiry; handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-005 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: py_vollib==1.0.1
Script: scripts/fixture_generators/bs_greeks_extended.py
(initial generation)
