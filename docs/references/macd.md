# MACD — port attribution

## Target
`PythonDataService/app/engine/indicators/macd.py` — `MovingAverageConvergenceDivergence`
(classical MACD with SMA-seeded EMAs).

## Reference
Gerald Appel's MACD (systematized in *Technical Analysis of Stocks &
Commodities*, 1970s). Cross-reference implementations with identical
math:
- LEAN `Indicators/MovingAverageConvergenceDivergence.cs`
- TradingView Pine `ta.macd()` with SMA seeding
- pandas-ta `ta.macd(fast=12, slow=26, signal=9)`

## Math summary
- `fast_ema` = `ExponentialMovingAverage(fast_period)` on close
- `slow_ema` = `ExponentialMovingAverage(slow_period)` on close
- `macd_line` = `fast_ema − slow_ema`, emitted once both EMAs ready
  (samples ≥ slow_period)
- `signal_line` = `ExponentialMovingAverage(signal_period)` of `macd_line`
- `histogram` = `macd_line − signal_line`
- `current_value` = `macd_line` (LEAN convention)
- `is_ready` when the signal EMA is ready, i.e.
  `samples ≥ slow_period + signal_period − 1`
  (for 12/26/9 defaults → sample 34)

Both EMAs use the SMA-seed-then-`value*k + prev*(1−k)` recursion with
`k = 2/(1+period)`.

## Tolerance
- Hand-computed micro-tests: exact (Decimal arithmetic).
- Cross-reference vs pandas-ta: `atol=1e-9, rtol=0`.
- Golden-fixture regression: `atol=1e-9, rtol=0`.

## Tests
- `PythonDataService/app/engine/tests/test_macd.py`
  - `test_warmup_emits_macd_after_slow_signal_after_slow_plus_signal_minus_1`
  - `test_current_value_is_macd_line`
  - `test_reject_fast_ge_slow`
  - `test_matches_pandas_ta_macd_strict`
  - `test_golden_fixture_regression`
