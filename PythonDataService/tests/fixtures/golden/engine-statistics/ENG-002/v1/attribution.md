# ENG-002 — Maximum Drawdown

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 5-point equity
curves spanning typical drawdown patterns. Hand-designed for exact verifiability.
See `engine_stats_extended.py::MDD_CURVES`.

**Layer 2 — Methodology provenance:** Bacon, *Practical Portfolio Performance
Measurement and Attribution* (2e), §8.2. Max drawdown = max over all t of
(peak_t - equity_t) / peak_t.

**Layer 3 — Independent numerical oracle:** numpy `maximum.accumulate` (running
max) + vectorized `(running_max - equity) / running_max` — different path from
canonical pure-Python loop with early exit.

## Formula

max_drawdown = max_t[(peak_t - equity_t) / peak_t]  where peak_t > 0
Returns a positive fraction (0.20 = 20% drawdown)

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::_max_drawdown`
Signature: `_max_drawdown(curve: Sequence[float]) -> float`
Uses pure-Python loop with running peak.

## Hand-Verification

Case 1: curve=[100, 110, 95, 105, 100]
  Running max: [100, 110, 110, 110, 110]
  Max DD at index 2: (110-95)/110 = 15/110 ≈ 0.136364

Case 2: curve=[100, 90, 80, 95, 100]
  Running max: [100, 100, 100, 100, 100]
  Max DD at index 2: (100-80)/100 = 0.20

Case 3: curve=[100, 120, 130, 110, 125]
  Running max: [100, 120, 130, 130, 130]
  Max DD at index 3: (130-110)/130 = 20/130 ≈ 0.153846

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless fraction (0.20 = 20% max drawdown)

## Regeneration

  python scripts/generate_fixtures.py --id ENG-002 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: numpy accumulate-max + vectorized fraction
Script: scripts/fixture_generators/engine_stats_extended.py
(initial generation)
