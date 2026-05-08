# ENG-005 — Calmar Ratio

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3 cases as ENG-004.
Initial cash, final equity, trading_days, and 5-point equity curve.

**Layer 2 — Methodology provenance:** Young (1991) "The Calmar Ratio: A Smoother
Tool". Calmar = CAGR / max_drawdown_pct.

**Layer 3 — Independent numerical oracle:** Composed from ENG-002 and ENG-004
numpy paths: `_oracle_cagr / _oracle_mdd`.

## Formula

years   = trading_days / 252
CAGR    = (final_equity / initial_cash)^(1/years) - 1
max_dd  = max_t[(peak_t - equity_t) / peak_t]
Calmar  = CAGR / max_dd

## Hand-Verification

Case 1: MDD=0.072727, CAGR=0.100000 → Calmar≈1.375000
Case 2: MDD=0.100000, CAGR=0.200000 → Calmar=2.000000
Case 3: MDD=0.150000, CAGR=0.150000 → Calmar=1.000000

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::compute_portfolio_statistics`
Key lines: `calmar = ann_return / max_dd` (requires max_dd > 0 and years > 0).

## Input Columns

initial_cash, final_equity, trading_days, e0..e4 (equity curve points)

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless (annualized CAGR per unit of max drawdown)

## Regeneration

  python scripts/generate_fixtures.py --id ENG-005 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: _oracle_cagr / _oracle_mdd (numpy paths)
Script: scripts/fixture_generators/engine_stats_extended.py
(initial generation)
