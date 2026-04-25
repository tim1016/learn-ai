# Momentum — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.mom(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `mom`
- **Math source**: classical price-difference momentum.

## Math summary
```
MOM_t = close_t − close_{t−N}
```

Absolute price difference over `N` bars — unbounded, in price units.
The first `N` bars are `NaN`.

Because the output scale is in price units rather than a ratio or
oscillator, MOM cannot be compared across instruments; use ROC for
cross-asset momentum.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 100] |

## Output
Single Series. Column name: `mom_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0. The math is a single subtraction, so output is bit-exact
reproducible.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture; not strictly needed given the trivial math.
