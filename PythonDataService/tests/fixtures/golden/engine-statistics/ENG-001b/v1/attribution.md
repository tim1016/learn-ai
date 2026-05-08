# ENG-001b — Sortino Ratio (Daily Returns, Annualized)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 5-element
daily return series. Same cases as ENG-001. See `engine_stats.py::CASES`.

**Layer 2 — Methodology provenance:** Bacon, *Practical Portfolio Performance
Measurement and Attribution* (2e), §8.3 (Sortino Ratio).
Canonical: `PythonDataService/app/engine/results/statistics.py::_sortino`.

**Layer 3 — Independent numerical oracle:** hand_computed — numpy mean/std(ddof=1) independent of canonical pure-Python loops.
numpy mean + downside variance with all-N denominator.

## Formula

Sortino = mean(r) / √(Σd² / N) · √periods_per_year

where:
  d = [r for r in returns if r < 0]  (downside returns, strict negative)
  N = len(returns)                    (ALL returns, NOT len(d))
  periods_per_year = 252

## Denominator Convention (IMPORTANT)

The canonical `_sortino` uses `len(returns)` (all N) in the downside-variance
denominator, not `len(downside)`. This is pinned here. Any change to this
convention requires regenerating this fixture with explicit justification.

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::_sortino`
Signature: `_sortino(returns, periods_per_year) -> float | None`
Key line: `downside_var = sum(r * r for r in downside) / len(returns)`

## Hand-Verification (exact arithmetic)

Case 1: r=[0.01, 0.02, -0.01, 0.03, 0.01]
  mean=0.012, downside=[-0.01]
  downside_var=(-0.01)²/5=0.0001/5=0.00002
  Sortino = (0.012/√0.00002) · √252

Case 2: r=[0.005, 0.015, -0.005, 0.010, 0.000]
  mean=0.005, downside=[-0.005]
  downside_var=(-0.005)²/5=0.000025/5=0.000005
  Sortino = (0.005/√0.000005) · √252

Case 3: r=[-0.02, 0.04, -0.01, 0.02, -0.005]
  mean=0.005, downside=[-0.02,-0.01,-0.005]
  Σd²=0.0004+0.0001+0.000025=0.000525
  downside_var=0.000525/5=0.000105
  Sortino = (0.005/√0.000105) · √252

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless (annualized Sortino ratio)

## Known Limitations

- 5-element series. Production Sortino requires a full equity curve.
- Denominator convention (all-N) differs from some textbook formulations.
- Edge cases (no downside, single return) covered in unit tests, not here.

## Regeneration

  python scripts/generate_fixtures.py --id ENG-001b --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-08
Oracle: hand_computed — numpy mean/std(ddof=1) independent of canonical pure-Python loops
Script: scripts/fixture_generators/engine_stats.py
Justification: Initial ENG-001b Sortino ratio fixture, hand-computed oracle
