# TSLA 24-Month Minute Capture -- Attribution

## Source

- **Provider**: Polygon.io Aggregates v2 API
- **Resolution**: 1-minute OHLCV trade bars
- **Adjustment mode**: raw (unadjusted)
- **Window**: 2024-06-03 to 2026-04-30

## Capture

- **Captured by**: Inkant Awasthi
- **Captured at (ms UTC)**: 1779421917519
- **Pipeline**: `PythonDataService/app/data_lake/ensure_data.py::ensure_data`
- **Script**: `PythonDataService/scripts/capture_24mo_minute_bars.py`
- **LEAN image digest**: `sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c`

## Contents

- `equity/usa/minute/tsla/` -- 479 daily trade-bar ZIP files (`YYYYMMDD_trade.zip`)
- `equity/usa/factor_files/tsla.csv` -- LEAN factor file (split/dividend adjustment factors)
- `equity/usa/map_files/tsla.csv` -- LEAN ticker map file (corporate action renames)
- `equity/usa/daily/tsla.zip` -- LEAN daily OHLCV bars (derived from minute bars)

## Intended use

Shared input fixture for the cross-engine parity matrix (Task 9).
The three study windows (W6mo, W12mo, W24mo) all read from this single capture.
The `data_contract_hash` in `manifest.json` is the authoritative integrity check:

```
2a25d5513f1e2bb821d224324346426e49c91ad55473b3f48493ad18ea10a264
```

## Regeneration policy

This fixture is regenerated only on deliberate trigger (a new `force_refresh=True` call
to `ensure_data`). The regenerating commit must update `manifest.json` with the new
`data_contract_hash` and `captured_at_ms_utc`, and explain the reason in the commit message.
