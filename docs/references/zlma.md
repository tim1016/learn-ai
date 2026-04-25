# Zero-Lag Moving Average — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.zlma(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `zlma`
- **Math source**: John Ehlers & Ric Way, *Zero Lag (Well, Almost)*
  (Stocks & Commodities V. 28:11, 2010).

## Math summary
```
lag       = floor(0.5 × (N − 1))
deLagged  = 2 × close − close.shift(lag)
ZLMA_t    = EMA_N(deLagged)
```

The "de-lagged" series adds the current momentum vector
(`close − close.shift(lag)`) to the current price, projecting forward
by the EMA's nominal lag. Wrapping with a standard `α = 2/(N+1)` EMA
then smooths that projected series. The output tracks price extrema
without the temporal displacement of a vanilla EMA.

The first `lag + N − 1` bars are `NaN`.

pandas-ta exposes `mamode` to swap the smoother; the default and the
Data Lab path use `mamode="ema"`.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 500] |

## Output
Single Series. pandas-ta names it `ZL_EMA_{N}` (or `ZL_<mamode>_<N>`
in general); the Data Lab dispatch lowercases to `zl_ema_{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
