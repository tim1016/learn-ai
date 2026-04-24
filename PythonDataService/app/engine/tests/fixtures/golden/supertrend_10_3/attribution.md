# Supertrend(10, 3) golden fixture

## What this is
500-bar synthetic OHLC and the Supertrend line + direction produced by
`app.engine.indicators.supertrend.Supertrend(atr_period=10, multiplier=3)`.

## Reference source
Classical Supertrend (Olivier Seban, ~2006) with the modern Pine Script
and pandas-ta conventions:
- ATR uses Wilder's average-form smoothing.
- Basic bands from `hl2 = (high + low) / 2`.
- Direction flip + band clamping per pandas-ta
  `ta.supertrend(length, multiplier)`.

The `test_matches_pandas_ta_supertrend_strict` test pins our Supertrend
line to pandas-ta's at `atol=1e-9, rtol=0` across the full series, and
confirms the direction flags match at every bar where both sides have a
defined direction.

## Regeneration
```
podman exec -w /app polygon-data-service python -m \
    app.engine.tests.fixtures.golden.supertrend_10_3.regenerate
```

## Tolerance
`atol=1e-9, rtol=0` for the regression test. Regenerate only with
justification in `docs/references/supertrend.md`.

## Files
- `input.csv` — OHLC bars
- `output.csv` — supertrend, direction (1 = uptrend, -1 = downtrend)
- `regenerate.py`
