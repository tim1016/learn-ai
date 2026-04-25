# Money Flow Index — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.mfi(high, low, close, volume, length=...)`. No in-repo
port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `mfi`
- **Math source**: Quong & Soudack, *Volume-Weighted RSI: Money Flow*
  (Technical Analysis of Stocks & Commodities, 1989).

## Math summary
1. Typical price: `TP = (high + low + close) / 3`.
2. Raw money flow: `MF = TP × volume`.
3. Compare `TP` to the prior bar's `TP` (drift = 1, the pandas-ta
   default):
   - If `TP_t > TP_{t-1}`: positive money flow contributes `MF_t`.
   - If `TP_t < TP_{t-1}`: negative money flow contributes `MF_t`.
   - If equal: contributes zero to both.
4. Over the lookback window `N`:
   ```
   MFR = sum(positive MF over N) / sum(negative MF over N)
   MFI = 100 − 100 / (1 + MFR)
   ```

Bounded in `[0, 100]`. The first `N` bars are `NaN` because the rolling
sums require `N+1` typical-price observations (the comparison itself
needs a prior bar).

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **14** | [1, 100] |

`drift` (pandas-ta default 1) is not exposed.

## Output
Single Series. Column name: `mfi_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
