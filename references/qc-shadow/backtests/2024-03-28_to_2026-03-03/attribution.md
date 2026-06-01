# Attribution — Virtual Orange Snake (full-history native-feed run)

| Field | Value |
|---|---|
| QC Cloud backtest id | `d2fe45a7142e88575f6fbd75229f8681` |
| QC backtest name | Virtual Orange Snake |
| QC project name | Calm Sky Blue Shark |
| LEAN engine | v2.5.0.0.17756 |
| Run date (UTC) | 2026-06-01 (`state.StartTime` 2026-06-01T00:14:52Z) |
| Status | Completed (no runtime error) |
| Backtest window | 2024-03-28 → 2026-03-03 (`algorithmConfiguration`) |
| First / last fill | 2024-04-15T13:45:00Z / 2026-03-02T18:00:00Z |
| Symbol | SPY |
| Order count | 112 fills → 56 long round-trips |
| QC reported net profit / win rate | 10.850% / 70% |
| Audit copy | `references/qc-shadow/SpyEmaCrossoverAlgorithm.py` |
| Audit copy SHA-256 | `cfc7f18877b8dcf9b99af4bb26e4f36f0b7ac6799fa5f4d6dc286945653d6078` |
| Source results SHA-256 | `19ecbac978a797153e0a7d1dcc44e2c5606006e70f4da73cdde502e573fe8bd5` |

## How `trades.csv` was generated

The 112 filled orders in the QC results export were sorted by `lastFillTime`
and paired buy(`direction=0`)→sell(`direction=1`) into 56 long round-trips
(the strategy is long-only; every entry closes flat before the next). Columns
follow `references/qc-shadow/README.md` § "Schema details — trades.csv":
`entry_time_ms,exit_time_ms` are int64 ms UTC (canonical), prices are QC fill
prices, `pnl_points = exit_price − entry_price`. Derived round-trip win rate
(39/56 ≈ 69.6%) cross-checks against QC's reported 70%.

## What is intentionally NOT here

This is a **native-feed, full-history** run, not one of the README's named
fixtures (`lean-parity-fixture` Test 1, `2025-08-01_to_2025-11-01` Test 2), and
no reconciliation test is wired to this directory yet.

- **`indicators.csv` — absent.** This run did not `Plot()` ema5/ema10/rsi, so
  the indicator series are not in the results export. Producing it requires a
  QC re-run with the indicators plotted (or another source). Until then a full
  Test-1/Test-2 reconciliation cannot consume this export.
- **`bars.csv` — absent.** Per-bar OHLCV for Test 2 was not exported.

`trades.csv` here is a committed, hashed receipt anchoring the QC backtest id
`d2fe45a7142e88575f6fbd75229f8681` to its fill log; it is not yet a parity
fixture.
