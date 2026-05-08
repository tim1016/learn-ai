# ENG-001 — Sharpe Ratio (Daily Returns, Annualized)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 5-element
daily return series spanning typical win/loss patterns. Hand-designed for
exact verifiability. See `engine_stats.py::CASES`.

**Layer 2 — Methodology provenance:** Sharpe (1994), "The Sharpe Ratio",
Journal of Portfolio Management 21(1) §IV.
Formula: mean(r) / std(r, ddof=1) · √periods_per_year
Canonical: `PythonDataService/app/engine/results/statistics.py::_sharpe`.

**Layer 3 — Independent numerical oracle:** hand_computed — numpy mean/std(ddof=1) independent of canonical pure-Python loops.
`numpy.mean` + `numpy.std(ddof=1)`, annualized by `numpy.sqrt(252)`.

## Formula

Sharpe = mean(r) / std(r, ddof=1) · √periods_per_year

where ddof=1 (sample standard deviation, N−1 denominator)

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::_sharpe`
Signature: `_sharpe(returns, periods_per_year) -> float | None`
Uses pure-Python `sum(...)` loops — different path, same formula.

## Hand-Verification (exact arithmetic)

Case 1: r=[0.01, 0.02, -0.01, 0.03, 0.01]
  mean=0.012, deviations=[-0.002, 0.008, -0.022, 0.018, -0.002]
  Σ(dev²)=0.00088, var(ddof=1)=0.00022
  Sharpe = (0.012/√0.00022) · √252

Case 2: r=[0.005, 0.015, -0.005, 0.010, 0.000]
  mean=0.005, deviations=[0, 0.01, -0.01, 0.005, -0.005]
  Σ(dev²)=0.00025, var(ddof=1)=0.0000625
  Sharpe = (0.005/√0.0000625) · √252

Case 3: r=[-0.02, 0.04, -0.01, 0.02, -0.005]
  mean=0.005, deviations=[-0.025, 0.035, -0.015, 0.015, -0.010]
  Σ(dev²)=0.0024, var(ddof=1)=0.0006
  Sharpe = (0.005/√0.0006) · √252

## Tolerance

atol=1e-9, rtol=0.0

Rationale: numpy vs pure-Python float64 on the same formula.
Observed max abs error: < 1e-15.

## Units

dimensionless (annualized Sharpe ratio)

## Known Limitations

- 5-element series. Short-series Sharpe is statistically unreliable in practice.
- Tests the formula kernel, not the full equity-curve pipeline.
- Zero-std and single-element edge cases are covered in unit tests, not here.

## Regeneration

  python scripts/generate_fixtures.py --id ENG-001 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — numpy mean/std(ddof=1) independent of canonical pure-Python loops
Script: scripts/fixture_generators/engine_stats.py
Justification: Initial ENG-001 Sharpe ratio fixture, hand-computed oracle
