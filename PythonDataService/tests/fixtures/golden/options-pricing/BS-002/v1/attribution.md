# BS-002 — Black-Scholes European Put Price

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 180-case grid of
(spot, strike, ttm_years, rate, vol) spanning OTM/ATM/ITM and short/long
maturities. No real market data. See `bs_price.py::SPOTS/STRIKES/TTMS/VOLS`.

**Layer 2 — Methodology provenance:** Hull, *Options Futures and Other
Derivatives* (10e), §15.8, equations 15.20/15.21 (continuous dividend yield).
Canonical implementation: `PythonDataService/app/services/bs_greeks.py::bs_european_price`.

**Layer 3 — Independent numerical oracle:** py_vollib==1.0.1.
`py_vollib.black_scholes.black_scholes(flag='p', S, K, t, r, sigma)`.
GPL-licensed. Used here in fixture generation only — never imported in app/.

## Formula

C = S·e^(-qT)·N(d1) - K·e^(-rT)·N(d2)   (call)
P = K·e^(-rT)·N(-d2) - S·e^(-qT)·N(-d1) (put)

where d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
      d2 = d1 - σ·√T

## Canonical Implementation

`PythonDataService/app/services/bs_greeks.py::bs_european_price`
Signature: `bs_european_price(spot, strike, ttm_years, rate, volatility, is_call, dividend=0.0)`

## Oracle

Library: py_vollib==1.0.1
Citation: py_vollib.black_scholes.black_scholes(flag, S, K, t, r, sigma); flag='c' for call. GPL-licensed library — test/generation-only.

Unit agreement check (2026-05-08): both py_vollib and our canonical return
dollar price with identical precision — no unit conversion required.

## QuantLib Cross-Check

The existing `test_bs_cross_engine_parity.py` (360-case grid, atol=1e-10)
proves our canonical matches QuantLib. It is the QuantLib canary for
BS-001. Separate QuantLib values are not stored in this fixture to avoid
date-arithmetic TTM rounding (QuantLib uses serial-day resolution;
bs_european_price uses continuous float TTM).

## Tolerance

atol=1e-10, rtol=0.0

Rationale: Cross-library comparison between scipy-based closed form and
py_vollib. Both use IEEE 754 float64 arithmetic on the same BSM formula.
Observed max abs error across the 180-case grid is < 1e-14.
Floor set to 1e-10 per conventions.py (1e-12 excluded due to CI/dev
platform divergence for transcendental functions).

## Input Grid

180 cases.
  spot: [80.0, 90.0, 100.0, 110.0, 120.0]
  strike: [90.0, 100.0, 110.0]
  ttm_years: [0.08333333333333333, 0.25, 0.5, 1.0]
  rate: [0.05]
  vol: [0.15, 0.2, 0.3]
  dividend: 0.0
  is_call: False

## Known Limitations

- Zero-dividend only (q=0). Non-zero dividend covered in a future fixture.
- No near-expiry or zero-vol edge cases (those are tested in the solver tests).
- QuantLib date-arithmetic rounding not captured in this fixture.

## Regeneration

  python scripts/generate_fixtures.py --id BS-002 --force \
    --justification "<reason>"

Then promote by editing manifest.json active_version.
Verify: python -m pytest PythonDataService/tests/fixtures/test_options_pricing_fixtures.py -v

## Generation Metadata

Generated: 2026-05-08
Oracle version: py_vollib==1.0.1
Script: scripts/fixture_generators/bs_price.py
(initial generation)
