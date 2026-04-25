# Accumulation/Distribution Line — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.ad(high, low, close, volume)` via the generic
`calculate_dynamic_indicators` reflection path. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `ad`
- **Math source**: Marc Chaikin, Accumulation/Distribution Line
  (chartschool.stockcharts.com canonical specification).

## Math summary
For each bar:

1. Money Flow Multiplier:
   `MFM = ((close − low) − (high − close)) / (high − low)`
   When `high == low`, pandas-ta sets `MFM = 0` (avoiding division by zero).
2. Money Flow Volume: `MFV = MFM × volume`.
3. Accumulation: `AD_t = AD_{t-1} + MFV_t`, with `AD_0 = MFV_0`.

Cumulative and unbounded. Reacts only to the close's location within the
bar's H–L range, not to bar-over-bar price change.

## Parameters exposed in Data Lab
None. The full pandas-ta default path is taken.

## Output
Single Series. Column name in the generated dataset: `ad`.

## Tolerance
This is a transport pass-through; equivalence is *by reference* —
the dataset value at any bar is exactly what `pandas_ta.ad(...)` returns
for the same OHLCV slice. The pin is the pandas-ta version
(`requirements.txt` line 20: `pandas-ta==0.4.71b0`); a library upgrade
must be paired with a regression check.

## Tests
None specific to `ad` today. The dispatch path itself is covered by the
Data Lab dataset-generation integration tests under
`PythonDataService/tests/`.

## Open items
- No golden fixture. If an in-house port is later required (to drop the
  pandas-ta dependency), generate a fixture from the current pandas-ta
  output on a fixed SPY window and pin `atol=1e-9, rtol=0`.
