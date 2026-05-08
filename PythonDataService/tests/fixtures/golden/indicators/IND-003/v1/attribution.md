# IND-003 — Relative Strength Index (period=3, Wilder's smoothing)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3-case grid as IND-001.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** LEAN `Indicators/RelativeStrengthIndex.cs`
with `MovingAverageType.Wilders`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
Seed: simple mean of first period gain/loss deltas; then Wilder's smoothing.

**Layer 3 — Independent numerical oracle:** Pure-Python Wilder RSI formula without
calling `RelativeStrengthIndex` class.

## Formula

gain[i] = max(0, price[i] - price[i-1])
loss[i] = max(0, price[i-1] - price[i])

Seed (after period=3 deltas):
  avg_gain = mean(gain[0..2])
  avg_loss = mean(loss[0..2])

Wilder smoothing (post-seed):
  `avg_gain = (avg_gain * 2 + gain) / 3`
  `avg_loss = (avg_loss * 2 + loss) / 3`

RSI = 100 - 100/(1 + avg_gain/avg_loss)
Edge: if round(avg_loss, 10) == 0, RSI = 100

is_ready at samples >= 4 (one extra for first delta)

## NaN Convention

v0 (first sample — no delta) and v1..v2 (accumulating for seed) are NaN.
First non-NaN value appears at v3 (sample 4).

## Hand-Verification (Case B, first 5 bars: [50.0, 52.0, 50.0, 54.0, 52.0])

deltas (gains): [2.0, 0.0, 4.0]
deltas (losses): [0.0, 2.0, 0.0]
Seed avg_gain=2.000000, avg_loss=0.666667
RSI at v3 ≈ 75.000000

## Critical Seeding Convention (D-009)

First avg is simple mean of period deltas (not Wilder's of period+1).
This matches LEAN's seeding. Some textbooks differ.

## Canonical Implementation

`PythonDataService/app/engine/indicators/rsi.py::RelativeStrengthIndex`

## Tolerance

atol=1e-9, rtol=0.0

## Regeneration

  python scripts/generate_fixtures.py --id IND-003 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: hand_computed — pure-Python Wilder RSI without calling canonical
Script: scripts/fixture_generators/indicators.py
(initial generation)
