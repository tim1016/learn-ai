# ENG-004 — CAGR (Compound Annual Growth Rate)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 equity-curve cases with
known initial/final values and trading_days. See `engine_stats_extended.py::CALMAR_CASES`.

**Layer 2 — Methodology provenance:** Standard finance definition.
CAGR = (final_equity / initial_cash)^(1/years) - 1
where years = trading_days / 252.

**Layer 3 — Independent numerical oracle:** numpy power function — different
floating-point path from canonical `(x)**(1/y)` in pure Python.

## Formula

years = trading_days / 252
CAGR  = (final_equity / initial_cash)^(1/years) - 1

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::compute_portfolio_statistics`
Key line: `ann_return = (final_equity / initial_cash) ** (1 / years) - 1`

## Hand-Verification

Case 1: initial=1000, final=1100, days=252 → years=1.0 → CAGR=0.10
Case 2: initial=1000, final=1200, days=252 → years=1.0 → CAGR=0.20
Case 3: initial=1000, final=1150, days=252 → years=1.0 → CAGR=0.15

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless annualized rate (0.10 = 10% CAGR)

## Regeneration

  python scripts/generate_fixtures.py --id ENG-004 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: numpy power (final/initial)^(1/years) - 1
Script: scripts/fixture_generators/engine_stats_extended.py
(initial generation)
