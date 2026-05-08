# iv30 — SPY Options Chain Snapshot (2024-12-20)

## Source

- **Market data provider:** Polygon.io
- **Endpoints used:**
  - `/v3/reference/options/contracts` — contract metadata (strike, expiry, type)
  - `/v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}` — daily OHLCV close prices
- **Capture date:** 2024-12-20
- **Underlying:** SPY
- **Capture script:** `scripts/capture_iv30_snapshot.py` (capture logic integrated into `tests/volatility/test_vix_replication.py::conftest`)

## Files

| File | Description |
|------|-------------|
| `spy-2024-12-20-chain.parquet` | Polygon SPY options chain snapshot: 881 contracts with strike, expiry_days, contract_type, close |
| `spy-2024-12-20-chain.meta.json` | Capture metadata + reference IV30 values computed at capture time |

## Reference values (from `spy-2024-12-20-chain.meta.json`)

| Field | Value |
|-------|-------|
| Spot | 591.15 |
| Risk-free rate | 4.24% (FRED) |
| Dividend yield | 1.20% (Polygon TTM) |
| VIX-style IV30 (ACT/365) | 0.17305 (17.3%) |
| Parametric IV30 (SVI fit) | 0.15584 (15.6%) |
| Below-30d expiry | 28 days |
| Above-30d expiry | 35 days |
| Contracts in window | 881 |

## Methodology

The VIX-style IV30 is computed using the CBOE VIX 2019 variance-replication formula interpolated between the 28-day and 35-day expiries. The parametric IV30 is computed via an SVI surface fit.

References:
- CBOE VIX 2019 whitepaper "VIX Index Calculation: Step-by-Step"
- Demeterfi, Derman, Kamal, Zou (1999) Goldman Sachs Quantitative Strategies Research Notes
- Gatheral (2004) SVI parameterization

## Governance

This fixture is stored in Parquet format and is **not registered in `manifest.json`** (see `README.md` → "Directories Outside Manifest Governance" → `iv30/`). It is governed by Git history. To regenerate:

1. Run the capture script against Polygon for the target date.
2. Verify the `vix_style_iv30_act365` value in the new `meta.json` is within reasonable range of the stored value (±50 bps for a 2024-12-20 re-capture is a red flag; investigate market data differences).
3. Replace both files and update this `attribution.md` with the new capture date and reference values.

## Why this date

2024-12-20 was chosen because:
- Standard options expiry calendar (no early close, no holiday)
- Moderate IV environment (~17% IV30) — not a crisis, not ultra-low-vol
- Polygon data available and complete for SPY at this date
- Two near-term expiries bracket the 30-day interpolation point cleanly (28d and 35d)
