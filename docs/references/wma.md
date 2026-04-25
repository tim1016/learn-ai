# Weighted Moving Average — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.wma(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `wma`
- **Math source**: classical linearly-weighted moving average.

## Math summary
With weights `w_i = i` for `i = 1, 2, ..., N` (most recent bar gets
weight `N`, oldest bar gets weight `1`):

```
WMA_t = sum(w_i × close_{t−N+i}) / sum(w_i)
      = sum(w_i × close_{t−N+i}) / (N × (N+1) / 2)
```

The first `N−1` bars are `NaN`. pandas-ta accepts an `asc` parameter
(default `True`) controlling weight direction; the Data Lab path leaves
it at the default.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 500] |

## Output
Single Series. Column name: `wma_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
