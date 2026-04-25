# Double Exponential Moving Average — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.dema(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `dema`
- **Math source**: Patrick Mulloy, *Smoothing Data with Faster Moving
  Averages* (Technical Analysis of Stocks & Commodities, January 1994).

## Math summary
Let `EMA_N(x)` denote the `length=N` EMA of series `x` with the standard
multiplier `α = 2/(N+1)`, seeded by SMA over the first `N` values.

```
EMA1 = EMA_N(close)
EMA2 = EMA_N(EMA1)
DEMA = 2 × EMA1 − EMA2
```

The smoothed-lag term `EMA2` is subtracted to "shift the curve left"
in time, reducing lag relative to a single EMA at the same `N`. The
first `2(N−1)` bars are `NaN` (each EMA contributes `N−1` warmup bars,
and the second consumes the first's output).

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 500] |

## Output
Single Series. Column name: `dema_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
