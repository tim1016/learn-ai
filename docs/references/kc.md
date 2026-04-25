# Keltner Channels — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.kc(high, low, close, length=..., scalar=...)`. No
in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `kc`
- **Math source**: Chester Keltner's volatility-envelope construction,
  modernised by Linda Raschke to use ATR rather than the original
  high–low average.

## Math summary
With pandas-ta's default `mamode="ema"` and `tr=True` (True Range, not
the simple high-low difference):

```
Basis_t = EMA_length(close)
ATR_t   = RMA(true_range, length)        (Wilder smoothing of TR)
Upper_t = Basis_t + scalar × ATR_t
Lower_t = Basis_t − scalar × ATR_t
```

True Range = `max(high − low, |high − prev_close|, |low − prev_close|)`.
The ATR smoothing here uses pandas-ta's RMA path internally even though
the basis EMA uses standard `α = 2/(N+1)` smoothing.

Warmup: the basis EMA is ready after `length−1` bars; the RMA-smoothed
ATR adds another `length−1` warmup, so the channel is `NaN` until both
are ready.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **20** | [1, 200] |
| `scalar` | float | **1.5** | [0.5, 5.0] |

⚠️ **Default divergence on `scalar`**: pandas-ta's own default is
`scalar=2`. The Data Lab UI defaults to `scalar=1.5` — narrower
channels, more breakouts. Intentional UX choice.

`mamode` (default `"ema"`) and `tr` (default `True`) are not exposed.

## Output
DataFrame with three columns. pandas-ta names them
`KCLe_{length}_{scalar}`, `KCBe_{length}_{scalar}`,
`KCUe_{length}_{scalar}` (lower, basis, upper — the trailing `e` marks
the EMA basis). The Data Lab dispatch lowercases the column names but
preserves the suffixes.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
- The Bollinger / Keltner "squeeze" pattern uses both this and `bbands`;
  the dedicated `squeeze` indicator already encodes that signal.
