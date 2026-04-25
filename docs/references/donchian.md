# Donchian Channels — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.donchian(high, low, lower_length=..., upper_length=...)`.
No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `donchian`
- **Math source**: Richard Donchian's price-channel breakout system
  (1960s, foundational to systematic trend-following).

## Math summary
```
Lower_t = min(low_{t−lower_length+1} … low_t)
Upper_t = max(high_{t−upper_length+1} … high_t)
Mid_t   = (Lower_t + Upper_t) / 2
```

Pure price-extrema envelope — no statistical averaging or volatility
weighting. The lower and upper bands can use different lookbacks
(asymmetric channels), which is why pandas-ta and the Data Lab UI
expose them as two independent params.

The first `max(lower_length, upper_length) − 1` bars are `NaN`.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `lower_length` | int | **20** | [1, 200] |
| `upper_length` | int | **20** | [1, 200] |

Defaults match pandas-ta's defaults.

## Output
DataFrame with three columns. pandas-ta names them
`DCL_{lower}_{upper}`, `DCM_{lower}_{upper}`, `DCU_{lower}_{upper}`
(lower, mid, upper). The Data Lab dispatch lowercases to
`dcl_{lower}_{upper}`, `dcm_{lower}_{upper}`, `dcu_{lower}_{upper}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0. The math is integer-min/integer-max plus an arithmetic mean,
so output is bit-exact reproducible across platforms.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture; not strictly needed given the trivial math.
