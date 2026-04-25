# Williams %R — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.willr(high, low, close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `willr`
- **Math source**: Larry Williams, *How I Made One Million Dollars Last
  Year Trading Commodities* (1973). Williams %R as commonly published.

## Math summary
```
HH_t  = max(high_{t−N+1} … high_t)
LL_t  = min(low_{t−N+1} … low_t)
WR_t  = −100 × (HH_t − close_t) / (HH_t − LL_t)
```

Bounded in `[−100, 0]` (inverted scale). A close at the period's high
yields `0`; a close at the period's low yields `−100`. The first
`N − 1` bars are `NaN`.

This is structurally a re-scaled, inverted Stochastic %K with the
same lookback semantics; it is a leading momentum oscillator suitable
for pullback-entry timing within a trend regime.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **14** | [1, 100] |

## Output
Single Series. pandas-ta names it `WILLR_{N}`; the Data Lab dispatch
lowercases to `willr_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
