# Supertrend — port attribution

## Target
`PythonDataService/app/engine/indicators/supertrend.py` — `Supertrend`
(ATR-based trailing-stop with direction flip + band clamping).

## Reference
Olivier Seban's classical Supertrend formula with the pandas-ta /
Pine Script `ta.supertrend(factor, atrPeriod)` convention.

## Math summary
See module docstring. Key points:
- Wilder (RMA) smoothing for ATR.
- `hl2` source (not `close`).
- Band clamp only in direction-preserved branch.
- Direction initialised to `+1` (uptrend) on the first ready bar.
- `current_value` is the supertrend line itself (`lower_band` when
  bullish, `upper_band` when bearish); `is_long` exposes direction
  as a bool.

## Tolerance
- Hand-computed micro-tests: exact (Decimal).
- Cross-reference vs pandas-ta `ta.supertrend`: `atol=1e-9, rtol=0`
  on the line; exact-equal on direction at bars where both sides are
  defined (pandas-ta defers direction by one bar).
- Golden-fixture regression: `atol=1e-9, rtol=0` on the line.

## Tests
`PythonDataService/app/engine/tests/test_supertrend.py`:
- `test_warmup_emits_first_value_at_atr_period`
- `test_direction_flips_when_close_breaks_upper_band`
- `test_uptrend_lower_band_never_retraces_down`
- `test_matches_pandas_ta_supertrend_strict`
- `test_golden_fixture_regression`
