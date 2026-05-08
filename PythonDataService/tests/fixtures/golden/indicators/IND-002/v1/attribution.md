# IND-002 — Simple Moving Average (period=3)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3-case grid as IND-001.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** LEAN `Indicators/SimpleMovingAverage.cs`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
Rolling window of maxlen=period; during warmup returns mean of all samples seen.

**Layer 3 — Independent numerical oracle:** Pure-Python list-based rolling window
without calling `SimpleMovingAverage` class.

## Formula

Warmup (samples 1..2): SMA = mean(prices[0..i])  — growing window
Post-warmup (samples 3+): SMA = mean(prices[i-3+1..i])  — fixed window

## Hand-Verification (Case A: prices=[10.0, 12.0, 14.0, 16.0])

sample 1: SMA = 10.0
sample 2: SMA = 11.0
sample 3: SMA = 12.0  (is_ready, full window)
sample 4: SMA = 14.0

## Canonical Implementation

`PythonDataService/app/engine/indicators/sma.py::SimpleMovingAverage`

## Tolerance

atol=1e-9, rtol=0.0

## NaN Convention

All bars produce a value (warmup is rolling mean, not NaN).

## Regeneration

  python scripts/generate_fixtures.py --id IND-002 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: hand_computed — pure-Python rolling window without calling canonical
Script: scripts/fixture_generators/indicators.py
(initial generation)
