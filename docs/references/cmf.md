# Chaikin Money Flow — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.cmf(high, low, close, volume, length=...)`. No in-repo
port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `cmf`
- **Math source**: Marc Chaikin, Chaikin Money Flow
  (chartschool.stockcharts.com canonical specification).

## Math summary
With Money Flow Multiplier and Money Flow Volume defined as in `ad.md`:

```
CMF_t = sum(MFV over last N bars) / sum(volume over last N bars)
```

The numerator and denominator are both rolling sums over the lookback
window `N`. Output is bounded in `[−1, +1]`. Bars before the window is
full are `NaN`.

CMF differs from AD by being a bounded, rolling oscillator instead of a
cumulative line; the underlying MFM / MFV math is shared.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **20** | [1, 100] |

The Data Lab default matches pandas-ta's default (`length=20`).

## Output
Single Series. Column name in the generated dataset: `cmf_length{N}`
(e.g. `cmf_length20` at the default).

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0. Library version is pinned in `requirements.txt`.

## Tests
None specific to `cmf` today. Covered by Data Lab dataset-generation
integration tests.

## Open items
- No golden fixture. If a port is required later, capture pandas-ta
  output on a fixed window and pin `atol=1e-9, rtol=0`.
