# IND-003 — Relative Strength Index (period=3, Wilder's smoothing)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3-case grid as IND-001.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** Pinned LEAN
`Indicators/RelativeStrengthIndex.cs`, `Indicators/MovingAverageTypeExtensions.cs`,
`Indicators/WilderMovingAverage.cs`, and `Indicators/SimpleMovingAverage.cs`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
The factory maps `MovingAverageType.Wilders` to `WilderMovingAverage`.
The RSI emits a value even before its child averages are ready.

**Layer 3 — Independent numerical oracle:** Pure-Python Wilder RSI formula without
calling `RelativeStrengthIndex` class.

## Formula

gain[i] = max(0, price[i] - price[i-1])
loss[i] = max(0, price[i-1] - price[i])

First price sample: no child updates; both averages are zero, so RSI=100.

For deltas 1..2, each child Wilder average returns the running SMA
of the deltas seen so far.

From delta 3 onward:
  avg_gain = (avg_gain * 2 + gain) / 3
  avg_loss = (avg_loss * 2 + loss) / 3

RSI = 100 - 100/(1 + avg_gain/avg_loss)
Edge: if round(avg_loss, 10) == 0, RSI = 100

is_ready at samples >= 4 (one extra for first delta)

## Hand-Verification (Case B, first 5 bars: [50.0, 52.0, 50.0, 54.0, 52.0])

v0: no delta; avg=(0,0), RSI=100.
v1: gain=2; avg=(2,0), RSI=100.
v2: loss=2; running avg=(1,1), RSI=50.
v3: gain=4; Wilder avg=(2,2/3), RSI=75. This is the first ready bar.
v4: loss=2; Wilder avg=(4/3,10/9), RSI=600/11 ≈ 54.545455.

Fixture v1 incorrectly withheld all pre-ready RSI values and seeded with
the simple mean of all 3 deltas. This version corrects that mismatch.

## Canonical Implementation

`PythonDataService/app/engine/indicators/rsi.py::RelativeStrengthIndex`

## Tolerance

atol=1e-9, rtol=0.0

## Regeneration

  python scripts/generate_fixtures.py --id IND-003 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-10-23
Oracle: hand_computed — pure-Python Wilder RSI without calling canonical
Script: scripts/fixture_generators/indicators.py
Justification: Correct RSI warmup and Wilder seed to pinned LEAN behavior; v1 withheld values and used a full-period SMA seed
