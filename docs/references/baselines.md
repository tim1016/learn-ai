# Null-baseline analysis

**Concept**: Generate N alternative strategies on the parent run's symbol / window / cost model, run each through the canonical engine, and rank the parent's metric values against the resulting null distribution. Answers "did the parent strategy beat *random*?"

**Canonical implementation**: `PythonDataService/app/research/baselines/` and `app/routers/baselines.py`. The methods, the Phipson-Smyth p-value and the per-method `sample_count` defaults are documented there.

## The buy-and-hold tautology

`StrategySpec` has no "always true" primitive. To run a single-trade buy-and-hold without modifying the schema, the generator builds an entry condition `BarProperty: property=range, op=">=", value=0.0` — tautologically true because the OHLC invariant `high >= low` (validated at engine ingestion) makes `range >= 0` always satisfied. The exit uses `BarsSinceEntry: op=">=" value=999_999`, an unreachable threshold. Result: enter on bar 1, hold through the engine's `on_end_of_algorithm` flush.

**Known limitation:** the engine's `on_end_of_algorithm` calls `ctx.liquidate(symbol)` which submits a pending order, but the main bar loop has already exited so the closing fill is never drained. The position is correctly tracked through equity (`RunMetrics.total_return_pct` and `max_drawdown_pct` are computed from the real equity curve), but `RunLedger.trade_log` ends up empty for buy-and-hold. `RunMetrics.total_trades = 0` and `exposure_pct = 0.0` are artefacts of this. Null-distribution aggregation works on the equity-derived metrics, which are correct, so the baseline still answers the right question. Fixing the engine flush is tracked as a follow-up; not blocking for null-baseline research.

## Parent window is reproduced exactly

Each child baseline runs on the parent's `(symbol, start_ms, end_ms, fill_mode, commission_per_order, slippage_per_share, random_seed)` — *only the strategy logic varies* across baselines. The runner converts `parent.start_ms` and `parent.end_ms` directly to NY-local dates via `datetime.fromtimestamp(ms / 1000, tz=NY).date()` with **no day adjustment**. Pinned by `test_child_run_window_matches_parent_exactly`.

This is unlike walk-forward, whose `_ms_to_inclusive_end_date` *does* subtract one calendar day — but only because WF's split policies emit half-open `[start_ms, end_ms)` fold boundaries (fold N+1's `start_ms` equals fold N's `end_ms`, and the engine's date filter is inclusive on both ends, so the day-overlap has to be removed). Phase A's `RunLedger.end_ms` is the NY-midnight of the *inclusive* end date — same convention as the input `end_date` to `RunRequest` — so passing it through requires no shift. **Different convention; do not import the WF helper here.**

Why this is in the doc: an early version (between PR #114's merge and PR #117's fix) copied the WF day-shift trick into the baseline runner, shaving a calendar day off every baseline window. The bug shipped briefly to master between 2026-05-07T02:18Z (PR #114 merge) and 2026-05-07T02:48Z (PR #117 merge) — roughly 30 minutes of master. Any baseline analyses persisted in that window are window-shifted by one day relative to their parents and should be regenerated if the final-day return was material.
