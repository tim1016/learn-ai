# Hull Moving Average — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.hma(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `hma`
- **Math source**: Alan Hull, *Hull Moving Average*
  (alanhull.com canonical specification).

## Math summary
```
half      = floor(N / 2)
sqrt_len  = floor(sqrt(N))
HMA_t     = WMA(2 × WMA_half(close) − WMA_N(close), sqrt_len)
```

That is: a `sqrt(N)`-period WMA of the difference between twice the
half-period WMA and the full-period WMA. The double-WMA construction
projects price forward enough to compensate for the smoothing lag of
the outer WMA, yielding a curve that hugs price tightly while
remaining smooth.

The first `N + sqrt_len − 2` bars are `NaN` (combined warmup of the
inner WMAs and the outer wrapper).

pandas-ta's `hma` accepts a `mamode` keyword that swaps the inner MA;
the default and the Data Lab path use `mamode="wma"`, the canonical
Hull definition.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **9** | [1, 500] |

⚠️ **Default divergence**: pandas-ta's own default is `length=10`. The
Data Lab UI defaults to `length=9`. Intentional UX choice.

## Output
Single Series. pandas-ta names it `HMA_{N}` (no `mamode` suffix when
`mamode="wma"`); the Data Lab dispatch lowercases to `hma_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
