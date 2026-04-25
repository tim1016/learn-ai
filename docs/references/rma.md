# RMA (Wilder's Smoothing) — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.rma(close, length=...)`. No in-repo port.

The same recursion is also reproduced inside the engine's own
indicators (notably `engine/indicators/adx.py` for DI/ADX smoothing
and `engine/indicators/supertrend.py` for ATR), but those are
self-contained — they do not call this path.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `rma`
- **Math source**: J. Welles Wilder, *New Concepts in Technical Trading
  Systems* (1978). The Wilder smoothing / RMA / SMMA recursion.

## Math summary
With smoothing factor `α = 1/N`:

```
RMA_0 not defined for the first N−1 bars (NaN).
RMA_{N−1} = SMA(close, N) at bar N−1   (SMA seed)
RMA_t = α × close_t + (1 − α) × RMA_{t−1}    for t ≥ N
```

This is structurally an EMA with `α = 1/N` instead of the standard
EMA's `α = 2/(N+1)`, so RMA lags more than EMA at the same `N`. It is
the smoother used inside RSI, ATR, ADX, and similar Wilder indicators.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 500] |

## Output
Single Series. pandas-ta names it `RMA_{N}`; the Data Lab dispatch
lowercases to `rma_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered indirectly by the ADX / Supertrend engine tests
which depend on the same recursion (their tolerance is `atol=1e-9`).

## Open items
- No standalone golden fixture for `rma`.
