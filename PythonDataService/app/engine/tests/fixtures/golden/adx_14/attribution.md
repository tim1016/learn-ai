# ADX(14) golden fixture

## What this is
A 500-bar synthetic OHLC random-walk series and the corresponding ADX(14),
+DI(14), -DI(14) values produced by `app.engine.indicators.adx.AverageDirectionalIndex`.

## Reference source
Wilder, J. Welles, *New Concepts in Technical Trading Systems* (1978),
the canonical specification. The port mirrors LEAN's
`Indicators/AverageDirectionalIndex.cs` and TradingView Pine's `ta.dmi()`,
which both implement the same Wilder spec.

There is no external ground-truth CSV used for this fixture — it's a
regression snapshot of our own implementation, cross-validated in
`test_adx.py::test_matches_pandas_ta_adx_loose` against pandas-ta's
ADX to confirm we are within ~1 ADX point across the whole series.
The layer-1 hand-computed tests in the same file are the primary
correctness guarantee.

## Regeneration
Run (from the repo root, with python-service container up):

```
podman exec -w /app polygon-data-service python -m \
    app.engine.tests.fixtures.golden.adx_14.regenerate
```

Parameters:
- period = 14
- seed = 42
- count = 500 bars
- bar interval = 15 minutes
- base timestamp = 2024-01-02 14:30 UTC

## Tolerance
The layer-3 regression test (`test_golden_fixture_regression`) uses
`atol=1e-9, rtol=0` — bit-exact float comparison. If the math in
`adx.py` changes, this fixture must be regenerated and this
attribution updated with the reason.

## Files
- `input.csv` — timestamped OHLC bars
- `output.csv` — adx, plus_di, minus_di per bar (NaN during warmup)
- `regenerate.py` — reproducible regeneration script
