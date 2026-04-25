# Ehlers Fisher Transform — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.fisher(high, low, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `fisher`
- **Math source**: John F. Ehlers, *Using the Fisher Transform*
  (Stocks & Commodities V. 20:11, 2002).

## Math summary
The Fisher Transform coerces a near-uniform input distribution into a
near-Gaussian output, sharpening turning points where price visits
range extrema.

Step 1 — normalize HL2 to `[−1, +1]` over the rolling window:
```
hl2_t   = (high_t + low_t) / 2
HH_t    = max(hl2 over last N)
LL_t    = min(hl2 over last N)
raw_t   = 2 × (hl2_t − LL_t) / (HH_t − LL_t) − 1
```

Step 2 — recursively smooth with a fixed weighting (per Ehlers):
```
val_t   = 0.66 × raw_t + 0.67 × val_{t−1}
val_t   = clip(val_t, −0.999, +0.999)        (avoid log of ±1)
```

Step 3 — apply the Fisher transform with one bar of memory:
```
fisher_t        = 0.5 × ln((1 + val_t) / (1 − val_t)) + 0.5 × fisher_{t−1}
fisher_signal_t = fisher_{t−signal}                                   (shift by signal periods)
```

Output approximately ranges in `[−5, +5]` in normal market regimes;
extremes are unbounded mathematically but rare in practice. The first
`N − 1` bars are `NaN`.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **9** | [1, 100] |

`signal` (pandas-ta default `1`) is not exposed in the Data Lab UI;
the signal column is therefore the Fisher line shifted by one bar.

## Output
DataFrame with two columns. pandas-ta names them
`FISHERT_{length}_{signal}` (Fisher line) and
`FISHERTs_{length}_{signal}` (signal line). The Data Lab dispatch
lowercases to `fishert_{length}_{signal}` and
`fisherts_{length}_{signal}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0. The recursive smoothing constants `0.66` / `0.67` and the
`±0.999` clip are pandas-ta-specific implementation choices that
deviate from some published Fisher Transform recipes — any future
in-house port must reproduce these exactly.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
- The recursive `0.66` / `0.67` constants are slightly unusual (they
  do not sum to 1); Ehlers' original publication uses `2/3` and `1/3`.
  Worth documenting if a port is undertaken.
