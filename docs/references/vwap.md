# VWAP — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.vwap(high, low, close, volume)`. No in-repo port.

The Polygon aggregate response also carries a per-bar `vwap` field.
That column passes through unchanged on the OHLCV side; the **Data Lab
"vwap" indicator** is the *computed* pandas-ta version, anchored daily
to match the standard institutional benchmark.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `vwap`
- **Math source**: classical anchored VWAP (institutional execution
  benchmark). pandas-ta source uses HLC3 typical price by default and
  resets the cumulative on each anchor period.

## Math summary
With typical price `TP = (high + low + close) / 3` and an anchor period
(default `D` — calendar day in the index timezone):

```
VWAP_t = cumsum(TP × volume, within anchor) / cumsum(volume, within anchor)
```

Cumulative within each session, reset at the anchor boundary. Requires
the input DataFrame to carry a `DatetimeIndex` so pandas-ta can detect
session boundaries — the Data Lab dispatch ensures this.

The cumulative form means the line is hyper-sensitive at the open
(few observations) and asymptotically smooth toward the close.

## Parameters exposed in Data Lab
None. pandas-ta defaults are used:
- `anchor="D"` (daily reset)
- `bands=None` (no ±k·σ bands emitted)

## Output
Single Series. pandas-ta names it `VWAP_D`; the Data Lab dispatch
lowercases to `vwap_d` (or `vwap` in some downstream renamings).

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Notes
- Polygon's bar-level `vwap` field is a per-bar volume-weighted price
  computed by Polygon, **not** the cumulative session VWAP. That column
  is unrelated to this indicator and is not what the Data Lab "vwap"
  selector produces.
- Because the cumulative resets at the anchor, the value can fall
  outside any single bar's H–L range. This is correct.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
- Anchored re-resets (W, M) are not exposed in the Data Lab UI today.
