# ENG-001b — Sortino Ratio (Daily Returns, Annualized)

**Status:** active · v1 (2026-05-08)
**Category:** engine-statistics
**Canonical:** `PythonDataService/app/engine/results/statistics.py::_sortino`

## What this pins

The annualized Sortino ratio as implemented in `_sortino`, validated against an independent numpy computation across 3 synthetic 5-element return series.

## Oracle

hand_computed — `numpy.mean` + downside variance (all-N denominator) annualized by `numpy.sqrt(252)`.

## Formula

```
Sortino = mean(r) / √(Σd² / N) · √periods_per_year

where:
  d = [r for r in returns if r < 0]   (strict negative only)
  N = len(returns)                      (ALL N, not len(d))
```

## Denominator Convention (Important)

The canonical `_sortino` uses `len(returns)` (all N) as the downside-variance denominator, not `len(downside)`. This is an implementation choice that differs from some textbook formulations. This fixture **pins that convention**. Any change to the denominator requires regenerating this fixture with justification.

## Input Cases (hand-verifiable)

| Case | Returns | mean | downside | downside_var |
|------|---------|------|----------|-------------|
| 1 | [0.01, 0.02, -0.01, 0.03, 0.01] | 0.012 | [-0.01] | 0.0001/5 = 0.00002 |
| 2 | [0.005, 0.015, -0.005, 0.010, 0.000] | 0.005 | [-0.005] | 0.000025/5 = 0.000005 |
| 3 | [-0.02, 0.04, -0.01, 0.02, -0.005] | 0.005 | [-0.02,-0.01,-0.005] | 0.000525/5 = 0.000105 |

## Agreement

Max observed abs error: < 1e-15.  
Pinned tolerance: `atol=1e-9, rtol=0.0`.

## Edge Cases (in test suite, not fixture)

- No downside returns → `_sortino` returns `None`.
- Single return → `_sortino` returns `None`.
- For low-downside series (Case 1), Sortino > Sharpe (confirmed by cross-fixture check).

## Regeneration

```
python scripts/generate_fixtures.py --id ENG-001b --force \
  --justification "<reason>"
```
