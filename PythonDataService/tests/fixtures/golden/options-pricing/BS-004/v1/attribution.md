# BS-004 — Black-Scholes Gamma (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Gamma = e^(-qT)·N'(d1) / (S·σ·√T).
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).gamma`.

**Layer 3 — Independent numerical oracle:** py_vollib==1.0.1.
`py_vollib.black_scholes.greeks.analytical.gamma(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Gamma = e^(-qT) · N'(d1) / (S · σ · √T)

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      N'(x) = standard normal PDF = (1/√(2π)) · e^(-x²/2)
      disc_q = e^(-qT)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Note: argument ORDER differs from bs_european_price — volatility before rate.
Returns BSGreeks.gamma (per dollar per dollar; dimensionless rate-of-delta-change).

## Oracle

Library: py_vollib==1.0.1
Citation: py_vollib.black_scholes.greeks.analytical.gamma(flag, S, K, t, r, sigma)

## Unit Declaration

gamma: per dollar per dollar (dimensionless).
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all 180 cases. No unit conversion applied.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across 180 cases is < 1e-15.

## Known Limitations

- Call only. Put gamma equals call gamma (symmetric), so a future BS-004b is
  low-priority but not included here.
- Zero-dividend (q=0).
- No near-zero-TTM cases (handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-004 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: py_vollib==1.0.1
Script: scripts/fixture_generators/bs_greeks_extended.py
(initial generation)
