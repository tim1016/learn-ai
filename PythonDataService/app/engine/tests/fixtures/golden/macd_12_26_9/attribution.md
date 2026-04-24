# MACD(12, 26, 9) golden fixture

## What this is
A 500-bar synthetic close series (seed=42 random walk) and the MACD,
signal, and histogram values produced by
`app.engine.indicators.macd.MovingAverageConvergenceDivergence`.

## Reference source
Classical MACD definition (Gerald Appel, 1970s):
- fast EMA(12), slow EMA(26), signal = EMA(9) of (fast - slow).
- EMAs seeded with SMA of the first `period` samples.

Cross-reference implementations using the same spec:
- LEAN `Indicators/MovingAverageConvergenceDivergence.cs`
- TradingView Pine `ta.macd()` with SMA-seeded EMAs
- pandas-ta `ta.macd(fast=12, slow=26, signal=9)`

Because all three use identical EMA math and identical seeding, the
test `test_macd.py::test_matches_pandas_ta_macd_strict` pins our
output to pandas-ta's at `atol=1e-9, rtol=0` — bit-exact parity.

## Regeneration
```
podman exec -w /app polygon-data-service python -m \
    app.engine.tests.fixtures.golden.macd_12_26_9.regenerate
```

## Tolerance
`atol=1e-9, rtol=0` for the regression test — bit-exact against the
committed output. Regenerating requires justification in
`docs/references/macd.md`.

## Files
- `input.csv` — timestamped close series
- `output.csv` — macd, signal, histogram per row (NaN during warmup)
- `regenerate.py` — reproducible regeneration script
