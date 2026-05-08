# BS-006 — Black-Scholes Vega (Call)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid sharing
the same input parameters as BS-001/BS-002/BS-003. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §19 (Greek Letters). Vega = S·e^(-qT)·N'(d1)·√T,
divided by 100 for per-1%-IV-move units.
Canonical: `app/services/bs_greeks.py::black_scholes_greeks(...).vega`.

**Layer 3 — Independent numerical oracle:** py_vollib==1.0.1.
`py_vollib.black_scholes.greeks.analytical.vega(flag='c', S, K, t, r, sigma)`.
GPL-licensed — test/generation-only.

## Formula

Vega = S·e^(-qT)·N'(d1)·√T / 100

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      N'(x) = standard normal PDF
(Division by 100 converts from per-unit-IV to per-1%-IV-move.)

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::black_scholes_greeks`
Signature: `black_scholes_greeks(spot, strike, ttm_years, volatility, rate, dividend, is_call)`
Returns BSGreeks.vega (per 1% move in implied volatility; positive for long calls).

## Oracle

Library: py_vollib==1.0.1
Citation: py_vollib.black_scholes.greeks.analytical.vega(flag, S, K, t, r, sigma)

## Unit Declaration

vega: per 1%-IV move (value already divided by 100 in both py_vollib and canonical).
A vega of 0.20 means the option gains approximately $0.20 per 1% rise in IV.
py_vollib and our canonical use identical units — ratio=1.000000 verified
empirically (2026-05-09) across all 180 cases. No unit conversion applied.

## Tolerance

atol=1e-10, rtol=0.0

Same rationale as BS-003: cross-library float64 comparison. Observed max
abs error across 180 cases is < 1e-15.
Note: vega is identical for calls and puts (put-call parity), so BS-006
implicitly covers put vega.

## Known Limitations

- Call only (though vega is put-call symmetric; see note above).
- Zero-dividend (q=0).
- No near-zero-TTM cases (vega → 0 near expiry; handled in solver tests).

## Regeneration

  python scripts/generate_fixtures.py --id BS-006 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: py_vollib==1.0.1
Script: scripts/fixture_generators/bs_greeks_extended.py
(initial generation)
