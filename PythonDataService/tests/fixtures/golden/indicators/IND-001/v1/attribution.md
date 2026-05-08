# IND-001 — Exponential Moving Average (period=3)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 8-bar price
series spanning monotone, alternating, and volatile patterns.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** LEAN `Indicators/ExponentialMovingAverage.cs`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
SMA-seeded warmup; k = 2/(1+period).

**Layer 3 — Independent numerical oracle:** Pure-Python formula without calling
the canonical `ExponentialMovingAverage` class. Seeding and recursion written
from first principles.

## Formula

k = 2/(1+3) = 0.5
Warmup (samples 1..3): EMA = mean(prices[0..i])  — running arithmetic mean
Post-warmup:                 EMA[n] = price[n]*0.5 + EMA[n-1]*0.5

## Critical Seeding Detail

The EMA seeds with the running arithmetic mean during warmup, not NaN.
At samples=3, EMA = SMA(all 3 warmup prices) — this is the canonical seed.
Post-warmup starts at sample 4.

## Hand-Verification (Case A: prices=[10.0, 12.0, 14.0, 16.0])

sample 1: EMA = 10.0   (mean of [p0])
sample 2: EMA = 11.0  (mean of [p0,p1])
sample 3: EMA = 12.0  (SMA seed = mean of [p0,p1,p2])
sample 4: EMA = 16.0*0.5 + 12.0*0.5 = 14.0

## Canonical Implementation

`PythonDataService/app/engine/indicators/ema.py::ExponentialMovingAverage`
`_compute_next_value` uses an embedded `SimpleMovingAverage` for warmup.

## Tolerance

atol=1e-9, rtol=0.0

## NaN Convention

v0..v7 in the output table contain float values for all bars (no NaN),
because the EMA always returns a value (warmup returns the running SMA).

## Regeneration

  python scripts/generate_fixtures.py --id IND-001 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: hand_computed — pure-Python SMA-seeded EMA formula without calling canonical
Script: scripts/fixture_generators/indicators.py
(initial generation)
