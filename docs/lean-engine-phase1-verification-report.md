# LEAN Engine — Phase 1 Verification Report

**Date:** 2026-04-09
**Scope:** Verify the new in-process backtest engine at `PythonDataService/app/engine/` against the Phase 1 done-definition from `docs/archive/plans/lean-engine-implementation-plan.md` (archived).
**Result:** Phase 1 bit-exact parity is confirmed. Three gaps are flagged below before Phase 2 begins.

---

## 1. Checks performed

Five independent checks, each designed to exercise a different layer of the new engine.

### Check 1 — Bit-exact validation test

Command:

```
cd PythonDataService
python -m app.engine.tests.test_spy_validation
```

Output:

```
Loaded 63 expected trades from fixture
Running backtest (this may take a few minutes)...
Engine produced 63 trades
Final equity: $110332.98
Net profit: $10332.98
Total fees: $126.00

======================================================================
PASS: All trades match LEAN reference bit-exactly.
```

All 63 trades from LEAN's `SpyEmaCrossoverAlgorithm-log.txt` reproduce exactly: entry/exit timestamps, prices (2dp), EMA5/EMA10 (4dp), RSI (2dp), PnL points (2dp), PnL percent (6dp). **Primary Phase 1 done-definition is met.**

### Check 2 — HTTP API endpoints via FastAPI TestClient

Booted a minimal FastAPI app with just `app.routers.engine` mounted under `/api/engine` (the full `app.main:app` fails to import in this verification environment because it pulls in the `polygon` SDK and requires `POLYGON_API_KEY` — not a regression, just pre-existing dependencies).

Four endpoints exercised:

| Request | Status | Result |
|---|---|---|
| `GET /api/engine/strategies` | 200 | `{"strategies": ["spy_ema_crossover"]}` |
| `POST /api/engine/backtest` with valid body | 200 | 63 trades, final_equity $110,332.98, win_rate 69.84% |
| `POST /api/engine/backtest` with `strategy_name: "bogus"` | 404 | `Unknown strategy 'bogus'. Registered: ['spy_ema_crossover']` |
| `POST /api/engine/backtest` with `fill_mode: "instant"` | 400 | `Unknown fill_mode 'instant'. Expected signal_bar_close or next_bar_open.` |

The extended statistics block on the backtest response populated as expected:

| Metric | Value |
|---|---|
| Total trades | 63 |
| Wins / losses | 44 / 19 |
| Win rate | 69.84% |
| Profit factor | 2.6483 |
| Max drawdown | 1.27% |
| Sharpe ratio | 2.0514 |
| Sortino ratio | 4.0558 |
| Calmar ratio | 3.9663 |
| MAE / MFE | `null` (not yet instrumented — engine does not capture intra-trade bars) |

All responses round-trip through Pydantic validators without errors. The JSON shape matches `EngineBacktestResponse`.

### Check 3 — Polygon → LEAN export round trip

Exercised `POST /api/engine/export-lean` with `app.services.dataset_service` and `app.services.polygon_client` monkey-patched to return synthetic minute bars (6 bars spanning two trading dates). Verified:

1. The endpoint returns `success: true` with `days_written: 2`.
2. Two zip files were written at `equity/usa/minute/spy/20240410_trade.zip` and `equity/usa/minute/spy/20240411_trade.zip`.
3. `LeanMinuteDataReader` reads back all 6 bars in the correct chronological order across both days.
4. Timestamps round-trip as Eastern Time; volumes and OHLC round-trip as Decimals.

**Caveat:** one of the returned closes displayed as `515.0999` rather than `515.10`. This is because the synthetic input used `515.05 + 0.05` in float, which is `515.0999999999...` in IEEE 754. The exporter faithfully preserves whatever precision the caller provides. Real Polygon responses arrive as dicts with already-rounded prices, so this will not occur in production — but it is worth noting that the exporter does not silently "fix" input precision.

**What this check does NOT cover:** a real Polygon call. The Polygon REST client and API key are not available in this verification environment. Before trusting the full flow end-to-end, `POST /api/engine/export-lean` needs to be hit once against real Polygon for a small date range (e.g., one week of SPY), followed by an engine backtest over that same window to confirm the round-trip produces the same trades as reading LEAN's own data for the overlap.

### Check 4 — Manual trade comparison (1 / 32 / 63)

Pulled three trades from three different points in the backtest and compared them against the extracted LEAN fixture row-by-row.

| # | Source | Entry | Entry Px | Exit | Exit Px | EMA5 | EMA10 | RSI | PnL pts | PnL % | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1  | engine  | 2024-04-11 12:00 | 515.34 | 2024-04-11 13:15 | 516.97 | 514.1906 | 513.9322 | 57.33 | 1.63  | 0.3163% | WIN  |
| 1  | LEAN    | 2024-04-11 12:00 | 515.34 | 2024-04-11 13:15 | 516.97 | 514.1906 | 513.9322 | 57.33 | 1.63  | 0.3163% | WIN  |
| 32 | engine  | 2025-05-27 09:45 | 585.60 | 2025-05-27 11:00 | 588.04 | 581.9541 | 581.2256 | 65.00 | 2.44  | 0.4166% | WIN  |
| 32 | LEAN    | 2025-05-27 09:45 | 585.60 | 2025-05-27 11:00 | 588.04 | 581.9541 | 581.2256 | 65.00 | 2.44  | 0.4166% | WIN  |
| 63 | engine  | 2026-03-25 09:45 | 658.90 | 2026-03-25 11:00 | 658.69 | 655.6635 | 655.1098 | 62.39 | -0.21 | -0.0319% | LOSS |
| 63 | LEAN    | 2026-03-25 09:45 | 658.90 | 2026-03-25 11:00 | 658.69 | 655.6635 | 655.1098 | 62.39 | -0.21 | -0.0319% | LOSS |

All three trades match on every field. This gives direct visual confidence in the bit-exact check — the automated test is comparing the right fields, not just happening to match zeros.

### Check 5 — NEXT_BAR_OPEN fill mode

Ran the same strategy through the engine with `fill_mode: "next_bar_open"` to confirm the alternate fill path executes cleanly.

| | SIGNAL_BAR_CLOSE | NEXT_BAR_OPEN |
|---|---|---|
| Total trades | 63 | 63 |
| Final equity | $110,332.98 | $109,430.07 |
| Net profit | $10,332.98 | $9,430.07 |
| Total fees | $126.00 | $126.00 |

The NEXT_BAR_OPEN path runs end-to-end without errors and produces a ~$900 different final equity, as expected — fills happen at the following minute bar's open instead of the signal bar's close, so prices differ slightly on every trade.

**Important issue discovered during this check — see Gap 1 below.**

---

## 2. Gaps identified

### Gap 1 — NEXT_BAR_OPEN reports inconsistent statistics

When the fill mode is `next_bar_open`, the backtest reports two different pictures of the same run:

1. **`result.net_profit` and `result.final_equity`** reflect the portfolio's actual fills. These update as the fill model books trades at the next bar's open, so they correctly show $9,430 in net profit.

2. **`statistics.*` (profit_factor, win_rate, sharpe, etc.)** are computed from `strategy.trade_log`, which is populated inside the strategy using *signal bar close prices*, not actual fill prices. The entry and exit prices in each logged trade are the bar closes the strategy observed when making its decision — not the prices the portfolio actually filled at.

The result: in NEXT_BAR_OPEN mode, `statistics.profit_factor`, `statistics.max_drawdown_pct`, and `statistics.sharpe_ratio` are identical to the SIGNAL_BAR_CLOSE numbers (2.6483, 1.27%, 2.05), even though the portfolio's net profit differs by nearly $1,000. An external reader of the API response would reasonably conclude the stats describe the portfolio — but they don't.

This is not a bit-exactness problem — the strategy's decision-making and the portfolio's accounting are both correct — it's a reporting inconsistency. Two options for the fix:

- **Option A:** Populate `_LoggedTrade` from `OrderEvent`s (actual fill prices) instead of from the signal bar inside the strategy. This makes the trade log reflect reality, and statistics will match the portfolio.
- **Option B:** Maintain both — keep the signal-bar trade log as-is, and add a parallel `portfolio_trade_log` derived from fills. Report statistics from the latter.

Option A is simpler and what LEAN's own behavior looks like. I'd recommend it.

### Gap 2 — NEXT_BAR_OPEN secondary validation not run against LEAN

The Phase 1 done-definition included "secondary validation passes within tolerance" — meaning a second run of LEAN with a next-bar-open fill model, then a tolerance-based comparison of portfolio statistics (not bit-exact, since fills will legitimately differ). This has not been done. The NEXT_BAR_OPEN code path runs cleanly but has no reference point to confirm its numbers are right.

To close this: run LEAN with `ImmediateFillModel` vs `FillModelPythonWrapper`-equivalent, produce a statistics snapshot, and compare against the engine's NEXT_BAR_OPEN response within ~0.1% tolerance on each metric. This is blocked on fixing Gap 1 first, otherwise the comparison would be apples-to-oranges.

### Gap 3 — Decimal performance

Open Question #1 from the implementation plan. Check 1 (the full 2-year SPY backtest) takes roughly 2–3 minutes on Decimal. That is fine for validation but painful for iterative strategy development — porting a second strategy and debugging it turn-by-turn would be slow.

No decision has been made yet on whether to stay on Decimal everywhere, or move to a hybrid approach (Decimal for cash/position bookkeeping, float64 for indicator inner loops, with the SPY validation test as the regression gate). I'd suggest making this decision *before* Phase 2, because migrating indicators later would mean re-validating every strategy.

---

## 3. Phase status summary

| Phase | Status | Notes |
|---|---|---|
| **Phase 1** — SPY validation | ✅ Primary done-definition met | Gap 2 (NEXT_BAR_OPEN secondary validation) still open; Gap 1 must be fixed first |
| **Phase 2** — Generalization | 🟡 Partial — 1/3 complete | `/engine/backtest` endpoint done; existing strategies not ported; Postgres writeback not done |
| **Phase 3** — Realism | 🟡 Partial — 1/3 complete | Extended statistics done (Sortino, Calmar, profit factor); slippage and commission models not started |
| **Phase 4** — Framework | ⬜ Not started | Alpha/Portfolio/Risk module split, scheduled events, multi-symbol |
| **Phase 5** — Data infrastructure | ⬜ Not started | Map files, factor files, symbol change tracking |

---

## 4. Recommendation

Close out Phase 1 completely before starting Phase 2. Specifically:

1. **Fix Gap 1** — switch `_LoggedTrade` population to use `OrderEvent` fills. This is a ~30-line change in `app/engine/strategy/base.py` plus a small update to `SpyEmaCrossoverAlgorithm`. The SIGNAL_BAR_CLOSE validation must continue to pass bit-exactly after the change.

2. **Fix Gap 2** — run LEAN once with an alternate fill model and capture its portfolio statistics. Add a `test_spy_next_bar_open_validation` that compares the engine's NEXT_BAR_OPEN response within tolerance.

3. **Decide on Gap 3** — spend ~1 hour benchmarking a hybrid float64-indicator variant against the bit-exact Decimal reference on the SPY validation set. If the hybrid passes within tolerance and is substantially faster, lock it in before Phase 2. Otherwise, accept the Decimal runtime and move on.

After those three items, Phase 2 work (porting existing strategies + Postgres writeback + frontend toggle) can begin with a clean slate.
