# Simple Moving Average — port attribution

## Target
Two paths exist in the repo, and they are not the same code:

1. **Data Lab path** — `PythonDataService/app/services/dataset_service.py`
   dispatches to `pandas_ta.sma(close, length=...)`.
2. **Engine path** — `PythonDataService/app/engine/indicators/sma.py`
   contains an in-repo Wilder-style streaming SMA used by strategies and
   composite indicators (e.g. as a baseline in MACD seeding). Tests in
   `app/engine/tests/test_sma.py`.

This document covers the **Data Lab path** (pandas-ta).

## Reference
- **Library**: pandas-ta v0.4.71b0, function `sma`
- **Math source**: arithmetic mean over a rolling window — the
  industry-standard SMA, no implementation variation.

## Math summary
```
SMA_t = (1/N) × sum(close_{t−N+1} … close_t)
```

Implemented in pandas-ta as `close.rolling(N).mean()`. The first `N−1`
bars are `NaN`.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **20** | [1, 500] |

⚠️ **Default divergence**: pandas-ta's own default is `length=10`. The
Data Lab UI defaults to `length=20`. Both produce mathematically valid
SMAs at their respective windows; the divergence is a UX choice and is
intentional.

## Output
Single Series. Column name: `sma_length{N}`.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0.

## Tests
- Data Lab path: covered by dataset-generation integration tests.
- Engine path: `PythonDataService/app/engine/tests/test_sma.py`
  (separate code, separate test).

## Open items
- No golden fixture for the Data Lab path.
- The two SMA implementations should produce identical numbers on the
  same input; a regression test pinning `engine.SMA == pandas_ta.sma`
  on a fixed series at `atol=1e-9, rtol=0` would harden the contract
  if/when needed.
