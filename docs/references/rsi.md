# Relative Strength Index — port attribution

## Target
Two paths exist in the repo, and they are not the same code:

1. **Data Lab path** — `PythonDataService/app/services/dataset_service.py`
   dispatches to `pandas_ta.rsi(close, length=...)`.
2. **Engine path** — `PythonDataService/app/engine/indicators/rsi.py`
   contains an in-repo Wilder-port RSI used by strategies. Tests in
   `app/engine/tests/test_rsi.py`.

This document covers the **Data Lab path** (pandas-ta).

## Reference
- **Library**: pandas-ta v0.4.71b0, function `rsi`
- **Math source**: J. Welles Wilder, *New Concepts in Technical Trading
  Systems* (1978).

## Math summary
With pandas-ta's default `mamode="rma"`, `scalar=100`, `drift=1`:

```
delta_t  = close_t − close_{t−1}
gain_t   = max(delta_t, 0)
loss_t   = max(−delta_t, 0)

avg_gain = RMA_length(gain)        (Wilder smoothing, α = 1/N)
avg_loss = RMA_length(loss)

RS_t  = avg_gain_t / avg_loss_t
RSI_t = 100 − 100 / (1 + RS_t)
      = scalar × avg_gain / (avg_gain + avg_loss)        (algebraically equivalent)
```

Bounded in `[0, 100]`. The first `length` bars are `NaN` (one bar lost
to the diff, then `length − 1` to seed RMA).

When `avg_loss == 0`, pandas-ta returns `RSI = 100`; when both are
zero, the result is `NaN` then forward-stable.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **14** | [1, 100] |

`scalar`, `mamode`, `drift`, `talib` are not exposed.

## Output
Single Series. pandas-ta names it `RSI_{N}`; the Data Lab dispatch
lowercases to `rsi_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Notes
- The engine RSI (`engine/indicators/rsi.py`) follows the same Wilder
  recursion. Cross-checking the two implementations on an identical
  input series should yield equality at `atol=1e-9, rtol=0` after the
  initial `length` warmup bars.
- The Data Lab path does **not** apply a 3×length warmup mask the way
  `services/ta_service.py` does for its own RSI route; raw pandas-ta
  output is emitted starting at bar `length`.

## Tests
- Data Lab path: covered by dataset-generation integration tests.
- Engine path: `PythonDataService/app/engine/tests/test_rsi.py`.

## Open items
- No standalone golden fixture for the Data Lab path.
- Add a parity test pinning `engine.RSI == pandas_ta.rsi` on a fixed
  series at `atol=1e-9, rtol=0` once the engine RSI is consumed by
  Data Lab.
