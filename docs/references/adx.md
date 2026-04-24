# ADX — port attribution

## Target
`PythonDataService/app/engine/indicators/adx.py` — `AverageDirectionalIndex`
(Wilder's DMI/ADX with +DI and -DI sub-indicators).

## Reference
Wilder, J. Welles, *New Concepts in Technical Trading Systems*, Trend
Research (1978). The canonical specification.

Cross-reference implementations that implement the same spec:
- LEAN Algorithm Framework — `Indicators/AverageDirectionalIndex.cs`
- TradingView Pine — `ta.dmi(length)`

## Math summary
See the module docstring in `adx.py` for the full recursion. Two
smoothing modes in play:

1. **Sum-form Wilder smoothing** for +DM, -DM, TR:
   - initial (dm_samples == period): sum of first `period` values
   - ongoing: `s_new = s_old − s_old/period + current`
2. **Average-form Wilder smoothing** for ADX:
   - initial (dx_samples == period): mean of first `period` DX values
   - ongoing: `ADX_new = (ADX_old × (period − 1) + DX_new) / period`

Warmup is `samples >= 2 × period`. For the default period=14, first ADX
is emitted at bar 28.

## Tolerance
The hand-computed unit tests are exact (Decimal arithmetic). The
golden-fixture regression test pins the committed 500-bar synthetic
output at `atol=1e-9, rtol=0`. The parallel pandas-ta sanity check
tolerates up to 1.0 ADX point of divergence, which is documented in
the test as an expected seeding difference (pandas-ta seeds DMI with
an SMA rather than the classic Wilder-sum).

## Open items
- No TradingView-derived fixture yet. When a user-exported TV ADX CSV
  is available for a known SPY window, add it as a secondary fixture
  and upgrade the cross-reference test to a `atol=1e-6` (or tighter)
  match against it.
- DX bit-exact parity with LEAN's actual C# output on a known SPY
  window — contingent on executable LEAN access.

## Tests
- `PythonDataService/app/engine/tests/test_adx.py`
  - Layer 1: hand-computed micro-tests (`test_warmup_*`, `test_dm_priority_*`, `test_pure_uptrend_*`)
  - Layer 2: `test_matches_pandas_ta_adx_loose`
  - Layer 3: `test_golden_fixture_regression`
