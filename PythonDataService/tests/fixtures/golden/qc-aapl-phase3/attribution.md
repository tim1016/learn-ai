# qc-aapl-phase3 fixture attribution

- **Tutorial**: https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
- **Captured by**: Tim
- **Captured at (UTC ms)**: 1778544000000  *(2026-05-12 00:00:00 UTC — taken from QC `state.StartTime` of the backtest blob; replace with actual capture timestamp if more precision needed)*
- **QC project id**: 31452310
- **QC backtest name**: "Hipster Yellow Bat"
- **QC algorithm id**: `748c9f9f400b777443a57289ba4468b7`

## Algorithm

`PrecomputedMlPredictionsAapl` — QC's precomputed-ML-predictions tutorial reduced to single-symbol AAPL. See `qc_algorithm_screenshot.png` for the verbatim `main.py` that produced this fixture. Key parameters:

- `add_equity("AAPL", Resolution.MINUTE)` — minute-resolution data
- `set_warmup(timedelta(days=3))` — warmup so day-1 price is known
- Schedule: `every_day` @ `09:31 ET` → `set_holdings(AAPL, 1.0 if pred > 0 else 0.0)`

## Window

- **Backtest start**: 2026-02-10
- **Backtest end**: 2026-02-10 (**single trading day** — Phase 3 fixture covers one fill only)
- **Initial cash**: $100,000
- **Brokerage model**: QC default (Interactive Brokers — confirm against the in-app "Algorithm Configuration" view)

## Captured fills (1 total)

| # | Time (UTC) | Side | Qty | Fill price | Fee | Value |
|---|---|---|---|---|---|---|
| 1 | 2026-02-10 14:31:00Z (09:31 ET) | BUY | 365 | $273.238170408 | $1.83 | $99,731.93 |

End equity: $100,067.46. Holdings at end: $99,801.22 (unrealized $107.57). No exit fill — position remained open at backtest end.

## FEE_PRESENCE_BRANCH

**Branch A** — `orderFeeAmount=1.83` is non-zero and present, so `assert_fees=True` is valid in the acceptance test.

## Price adjustment mode

QC reports `priceAdjustmentMode: 1` (Split-Adjusted) on the order. The `qc_price_history.csv` must be captured with the same adjustment to keep `_audit_fixture` clean. Default `qb.history(...)` returns split-adjusted prices.

## Schema notes

`qc_orders.json` was hand-derived from the QC backtest JSON (the API export route requires a paid token, unavailable on Tim's tier). The canonical schema is preserved: top-level `orders` array, each with `events` array containing `time` / `fillQuantity` / `fillPrice` / `direction` / `orderFeeAmount`.

`qc_equity.json` is truncated for readability (full curve has 80 minute-bars). Equity is diagnostic only; not asserted by the reconciler.

## Known limitations

- **1-day window** means no round-trip P&L → `PNL_DRIFT` cannot fire. Other gating categories (FILL_PRICE_DRIFT, QUANTITY_MISMATCH, COMMISSION_DRIFT, DECISION_MISMATCH, FILL_TIME, etc.) still validate.
- **Order was submitted during extended hours** (08:11 UTC) but **filled in regular hours** (09:31 ET / 14:31 UTC). QC's `OrderFillsDuringExtendedMarketHoursAnalysis` is misleading here — the *fill* is regular-session.
- **Phase 3.5 upgrade path**: capture a multi-week window (e.g. 2026-02-10 → 2026-03-12 matching the PR #215 prediction-set window) to exercise round-trip P&L.
