# Normalized Average True Range — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.natr(high, low, close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `natr`
- **Math source**: ATR normalized by close, expressed as percent.
  Common reference: tulipindicators.org `natr` and TA-Lib `NATR`.

## Math summary
With pandas-ta's defaults (`mamode="ema"`, `scalar=100`, `drift=1`):

```
TR_t   = max(high_t − low_t, |high_t − close_{t−1}|, |low_t − close_{t−1}|)
ATR_t  = EMA_length(TR)
NATR_t = scalar × ATR_t / close_t
       = 100 × ATR_t / close_t
```

Output is volatility expressed as a percentage of the close — a
scale-invariant volatility metric suitable for cross-asset comparison
or volatility-parity sizing. Unlike many ATR implementations that use
RMA / Wilder smoothing by default, **pandas-ta's `natr` uses an EMA**
on the True Range under its default `mamode="ema"`. This is a
non-obvious detail and matters for parity work.

The first `length − 1` bars are `NaN` from the EMA warmup; the very
first bar is also `NaN` because `TR` requires a prior close.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **14** | [1, 100] |

`mamode`, `scalar`, `drift`, `prenan`, and `talib` are not exposed.

## Output
Single Series. pandas-ta names it `NATR_{N}`; the Data Lab dispatch
lowercases to `natr_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Notes
- NATR is **magnitude only** — no directional bias. Treat it as a
  volatility envelope, not a trend signal.
- If parity with TA-Lib's `NATR` is ever required, note that TA-Lib
  uses Wilder's smoothing (RMA), not EMA. Switch `mamode="rma"` and
  expose it.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
