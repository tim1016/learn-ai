# Triple Exponential Moving Average — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.tema(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `tema`
- **Math source**: Patrick Mulloy, *Smoothing Data with Faster Moving
  Averages* (Technical Analysis of Stocks & Commodities, January 1994).

## Math summary
With `EMA_N(x)` = standard `α = 2/(N+1)` EMA, SMA-seeded:

```
EMA1 = EMA_N(close)
EMA2 = EMA_N(EMA1)
EMA3 = EMA_N(EMA2)
TEMA = 3 × EMA1 − 3 × EMA2 + EMA3
```

Algebraically, this equals `EMA1 + 3 × (EMA1 − EMA2) + (EMA3 − EMA2)`
— successive lag-correction terms. The first `3(N−1)` bars are `NaN`
because each EMA consumes a warmup tail.

TEMA is faster (and noisier) than DEMA at the same `N`. Quantitatively
useful as a near-zero-lag price proxy; structurally vulnerable to
whipsaw in low-liquidity regimes.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 500] |

## Output
Single Series. Column name: `tema_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
