# OPT-IB-002 IBKR Snapshots

Captured option market data snapshots for fixture OPT-IB-002 (IBKR IV vs Newton-Raphson/Brent solver).

## How snapshots are captured

```
python scripts/capture_ibkr_snapshot.py --output scripts/ibkr_snapshots/opt_ib_002/
```

Requires a running IBKR TWS or IB Gateway (paper or live) on localhost:7497 (paper) or 7496 (live).

## File naming

`snapshot_YYYYMMDD_HHMMSS.arrow` — UTC timestamp of capture.

## What the snapshot contains

One row per option contract. Columns:

| Column | Type | Description |
|--------|------|-------------|
| symbol | string | Underlying symbol (e.g. "SPY") |
| right | string | "C" or "P" |
| strike | float64 | Strike price |
| expiry_ms | int64 | Expiry close: 16:00 ET on expiry date, as ms UTC |
| snapshot_ms | int64 | Moment of capture as ms UTC |
| spot | float64 | Underlying last/close price at capture |
| bid | float64 | Option bid |
| ask | float64 | Option ask |
| mid | float64 | (bid + ask) / 2 |
| ibkr_model_price | float64 | IBKR modelGreeks.optPrice — the model price IBKR used to back out ibkr_iv |
| ibkr_iv | float64 | IBKR modelGreeks implied volatility |
| ttm_years | float64 | (expiry_ms - snapshot_ms) / (365.25 × 24 × 3600 × 1000) |
| rate | float64 | Risk-free rate used (continuously compounded) |
| dividend | float64 | Dividend yield used (continuously compounded) |
| is_call | bool | True = call, False = put |

## Committed snapshots

| File | Date | Symbol | Contracts | Notes |
|------|------|--------|-----------|-------|
| (none yet) | — | — | — | Run capture_ibkr_snapshot.py to populate |
