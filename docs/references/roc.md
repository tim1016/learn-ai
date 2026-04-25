# Rate of Change — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.roc(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `roc`
- **Math source**: classical rate-of-change percent oscillator.

## Math summary
With pandas-ta's default `scalar=100`:

```
ROC_t = scalar × (close_t − close_{t−N}) / close_{t−N}
      = 100 × (close_t / close_{t−N} − 1)
```

Standardized momentum, expressed as a percent return over `N` bars.
Unbounded but scale-invariant — comparable across instruments. The
first `N` bars are `NaN`.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 100] |

`scalar` (default `100`) is not exposed.

## Output
Single Series. Column name: `roc_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
