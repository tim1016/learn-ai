# Reconciliation — QC AAPL Phase 3.5 Path A trade-level parity

**Status:** ✅ passed — single-fill scope. Multi-day round-trip P&L deferred (see "Open follow-ups" below).
**Date:** 2026-05-12
**Reference:** [Phase 3 design](../../superpowers/specs/2026-05-11-phase3-pnl-parity-design.md), [Phase 3.5 design](../../superpowers/specs/2026-05-11-phase35-path-a-intraday-fill-mode-design.md), [capture runbook](../qc-aapl-phase3-capture-runbook.md)
**Fixture:** `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/` (minute resolution, 2026-02-09 09:31 → 2026-02-11 16:00 NY, 1170 minute bars)
**Captured QC backtest:** "Formal Black Rabbit" (truncated by QC free tier's minute-data trailing window — see "Why only one fill" below; algorithm code at `qc_algorithm_screenshot.png`)

## What was reconciled

Our engine running the AAPL single-symbol `StrategySpec` with:
- `PredictionRef.lookup="next_after_bar_close"` (Path A data timing)
- `fill_mode="next_session_open"` (defer-only, NY-trading-date eligibility)
- `SetHoldings(1.0)` sizing
- `commission_per_order=0` (fees computed reconciler-side via `IbkrEquityCommissionModel`)

Run window: 2026-02-09 → 2026-02-12 (engine start_date / end_date). QC's backtest window (truncated by minute-data trailing-window cap — see "Why only one fill" below): 2026-02-10 → 2026-02-11.

## Outcome

QC's backtest produced **1 fill**: BUY 365 AAPL @ $273.238170408 on 2026-02-10 09:31 ET, fee $1.83.

Our engine produced **1 fill**: BUY 364 AAPL @ $273.178225656 on 2026-02-10 09:31 NY, fee (reconciler-side IBKR) ≈ $1.82.

The reconciler aligns these by `(trading_date, side)` → 1 pair, 0 divergences under the agreed tolerances.

## Divergence report

| Category | Count | Note |
|---|---|---|
| `DECISION_MISMATCH` | 0 | Both sides have a buy on 2026-02-10 |
| `DIRECTION_MISMATCH` | 0 | Both buys |
| `QUANTITY_MISMATCH` | 0 (within atol) | 365 vs 364: 1-share difference, absorbed by `qty_atol=2`. Root cause below. |
| `FILL_PRICE_DRIFT` | 0 (within atol) | $273.238 vs $273.178: $0.060 difference, absorbed by `fill_price_atol=$0.10`. Root cause below. |
| `COMMISSION_DRIFT` | 0 | Reconciler-side IBKR fee ≈ QC's recorded fee within $0.01 |
| `PNL_DRIFT` | n/a | No round-trip in single-fill scope |
| `FIXTURE_INSUFFICIENT` | 0 | Minute audit clean — QC's fill price $273.24 falls within the 09:31 minute bar's [low=273.05, high=275.11] |
| `ORDER_TYPE_MISMATCH` | 0 | Both market orders |

**Acceptance:** `report.status == "passed"`. One pinned aligned-fill row asserted in `test_qc_aapl_phase3_trade_parity.py::test_qc_aapl_phase3_trade_level_parity`.

## Tolerances accepted (and why)

| Tolerance | Default | Phase 3.5 value | Rationale |
|---|---|---|---|
| `fill_price_atol` | $0.01 | **$0.10** | QC's fill simulator is bid/ask-aware; our engine uses OHLC bars and fills at `bar.open`. Bid-ask spread for AAPL at 09:31 ET on 2026-02-10 was ~$0.11 (bid $273.128, ask $273.238). The $0.06 actual diff is dominated by this spread imprecision. $0.10 covers it with margin without admitting bar-level price drift. |
| `qty_atol` | 0 | **2** | Our engine's `SetHoldings(1.0)` sizes off the consolidated daily-bar close ($274.37) at signal time; QC sizes off the expected fill price ($273.24). For $100k initial cash this produces 364 vs 365 shares (1-share rounding difference). Documented as a known limitation; closing it requires our engine to look forward to the fill bar's price at sizing time, which conflicts with the consolidator-fire-on-rollover flow. |
| `commission_atol` | $0.01 | (unchanged) | IBKR tiered formula reproducible within $0.01 |
| `per_share_pnl_atol` | $0.01 | (unchanged) | Not exercised in single-fill scope |
| `pnl_floor_atol` | $0.01 | (unchanged) | Not exercised in single-fill scope |

## Engine design — Path A semantics

`FillMode.NEXT_SESSION_OPEN`:
- **Behavior**: defer the market order; pending-fills loop retries on each subsequent minute bar. The fill_model's eligibility check accepts when the candidate bar's NY-local trading date is strictly greater than `signal_bar.end_time.date()`.
- **Timing in daily-consolidator-over-minute-stream**: day-T daily-consolidated bar fires on the first minute of day-(T+1); strategy submits market order; order defers (date == day-T+1's first minute is the same date as signal_bar's end_time only when consolidator end_time was day-T 16:00 NY, so eligibility passes on the very next bar = day-(T+1) 09:31). Net fill: at the open of bar `[09:31, 09:32)` on day-(T+1), matching QC's MarketOrder timing exactly.

`PredictionRef.lookup="next_after_bar_close"`:
- **Behavior**: at end of day-T's daily-consolidated bar, the evaluator reads `prediction_set.next_after(bar.end_time_ms)` — the row with the smallest timestamp strictly greater than the bar's end. For our prediction-set artifact anchored at "T 16:00 NY", this returns the prediction "for day T+1".
- **Combination**: at end of day-T's daily bar (i.e., when consolidator fires on day-(T+1)'s first minute), evaluator reads prediction-for-T+1; order submits; NEXT_SESSION_OPEN defers one minute; fill lands at day-(T+1) 09:31 NY using prediction-for-T+1. Matches QC's "fire at start of day-T+1, fill at 09:31 ET using prediction-for-T+1" semantics.

## Open follow-ups

1. **Phase 3.5+ multi-day round-trip P&L** — **accepted limitation, not pursued.**

   **Why only one fill.** The QC tutorial algorithm uses `Resolution.MINUTE` for AAPL data. Per the [QC forum on free-plan data access](https://www.quantconnect.com/forum/discussion/19781/getting-data-with-free-plan/), free tier provides hour/daily data broadly but minute/second/tick data only on a "shorter trailing window." Empirically that trailing window is approximately 90 calendar days, advancing forward as time passes. The captured backtest ran with `set_start_date(2026, 2, 10)` / `set_end_date(2026, 2, 11)` because dates earlier than that had no minute-bar data available; QC simulated just those two trading days and produced a single entry fill. The exit signal (2026-02-20, first negative prediction in the prediction-set artifact) and re-entry (2026-02-21) fell outside that two-day window and were never simulated, so no round-trip P&L is available to reconcile against.

   (Historical note: earlier drafts of this section described the truncation as a "free-tier OOS reserve." That framing wasn't from QC's documentation — it was project-internal terminology that inverted cause and effect. The truer description is the minute-data trailing-window cap above, sourced from the QC forum.)

   **Decision recorded 2026-05-12.** We are not pursuing this work. Specifically: not purchasing the QC Researcher Seat (~$10/month) to unlock a longer minute-data trailing window, and not re-capturing at daily resolution on a shifted earlier window (which would validate a different engine code path — `FillMode.NEXT_BAR_OPEN` against QC's daily-bar fill semantics — instead of the `NEXT_SESSION_OPEN` minute-bar alignment that Phase 3.5 Path A actually validates; a different validation property, not a smaller one).

   **What this means for engine confidence — read carefully.** End-to-end engine validation against real QC output is **single-fill only**. The reconciler's multi-fill machinery (`_pair_round_trips`, `_classify_pnl_drift`, seq-aware alignment, 8-category taxonomy) is exercised by unit tests in `test_qc_reconciler.py` against hand-built synthetic fixtures — **not** against any real QC backtest. We therefore have **no empirical evidence** that our engine produces correct exit signals, correct round-trip P&L, or correct multi-fill sequencing under a real multi-trade truth set. What we have is: (a) one pinned aligned entry fill vs QC at minute resolution, and (b) unit-test-level confidence in the reconciler's classification logic. These are different claims and should not be conflated.

   **If multi-trade validation is ever needed**, the unblocking options are: (i) **paid-tier QC subscription** to unlock a longer minute-data trailing window, preserving the Phase 3.5 Path A validation property at full fidelity; (ii) **re-capture at daily resolution** on a shifted window — cheaper, but it validates a different fill-mode code path (`NEXT_BAR_OPEN` against daily bars) rather than the minute-bar `NEXT_SESSION_OPEN` path that's the centerpiece of Phase 3.5 Path A; (iii) **a different reference backtester** (e.g., a local LEAN run) that doesn't have free-tier data limits. None of these are scheduled.

2. **SetHoldings sizing alignment with QC** (1-share `qty_atol=2` accepted). The 1-share offset is bounded but documented; reducing `qty_atol` to 1 (still tolerant of 1-share rounding) is cosmetic. Eliminating it entirely requires our engine to use the expected fill price for sizing, which conflicts with the consolidator-fire-on-rollover flow (fill price isn't known until the next iteration). Worth revisiting if Phase 4 multi-symbol top-N ranking surfaces a similar issue.

3. **Bid/ask-aware fill model** (`fill_price_atol=$0.10` widened). Eliminating the $0.06 bid-ask gap requires capturing bid/ask alongside OHLC in the fixture and modeling the spread in our fill model. Significant work for a small precision gain; not pursued.

## Historical note

Replaced the Phase 3.0 daily/single-day fixture in place on 2026-05-12; git history (`git log -- PythonDataService/tests/fixtures/golden/qc-aapl-phase3/`) is the audit trail for the prior shape. Phase 3.0 was held open by a structural one-day fill-date offset between QC's intraday `set_holdings @ 09:31 ET` and our engine's `NEXT_BAR_OPEN` (which filled at the next daily bar's open). Phase 3.5 Path A closes this gap.

## How to re-run

```bash
podman exec polygon-data-service python -m pytest \
  /app/tests/research/parity/test_qc_aapl_phase3_trade_parity.py -v -s
```

The success report is rendered to
`PythonDataService/artifacts/reconciliations/qc-aapl-phase3-latest.md`
on every run (success or failure; gitignored). This committed file is
the human-authored interpretation.
