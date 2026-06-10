# Cross-engine W12mo slice — LEAN vs Engine Lab 1-year parity

**Date:** 2026-06-10

**Purpose:** Pin the 1-year (W12mo) row of the cross-engine golden-fixture matrix that was designed in May 2026 but blocked on wide-window AppleHV runs. PR #466 unblocked arm64; this slice generates the four W12mo cells and proves Engine Lab matches LEAN bar-for-bar and fill-for-fill over the full year.

Reference design: `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`.

## Inputs

- **Window:** 2025-05-01 → 2026-04-30 (251 trading days, regular session)
- **Tickers:** SPY, QQQ, AAPL, TSLA — the four already in `TICKERS`
- **Strategy:** EMA(5)/EMA(10) crossover with RSI(14, Wilders) gate on 15-min consolidated bars, EXIT_BARS=5 time-stop. Strategy source: `app/lean_sidecar/trusted_samples/ema_crossover.py` (`EMA_CROSSOVER_SOURCE`).
- **Runtime parameters:** `bar_minutes=15`, `session=regular`, `adjustment=raw`, `starting_cash=100000`, `sizing_model=lean_set_holdings`.
- **Brokerage contract:** Interactive Brokers, margin account, `ImmediateFillModel`, `IbkrEquityCommissionModel`. `assert_fees=True` (Branch A — `COMMISSION_DRIFT` is gating).
- **LEAN image (pinned at capture):** `sha256:0b8d4e381b63daaa4cebbea7af294cc5b140793a6fd13f8c9cfd63ef2a2fb24d` — the arm64 derivative committed in PR #466 (`learn-ai/lean-sandbox:arm64-dotnet109`). The W12mo cells' `lean_runtime.container_image_digest` field records this as `localhost/learn-ai/lean-sandbox@sha256:…` (the actual image the launcher ran), addressed in the same PR by deriving the prefix from `LEAN_IMAGE_REPO` in `scripts/regenerate_cross_engine_study.py`. The W6mo cells were captured against upstream `docker.io/quantconnect/lean` and keep that prefix.
- **Capture data:** the existing `_lean_data_capture/<TICKER>/` directories from the May 2026 capture commit (3f4991cb). No re-capture; the W12mo window (2025-05-01 → 2026-04-30) is a strict subset of the captured 24-month window.

## Command

```
cd PythonDataService
./.venv/bin/python -m uvicorn app.lean_sidecar.launcher.app:app \
    --host 0.0.0.0 --port 8090 &
for cell in \
    SPY_W12mo_2025-05-01_to_2026-04-30 \
    QQQ_W12mo_2025-05-01_to_2026-04-30 \
    AAPL_W12mo_2025-05-01_to_2026-04-30 \
    TSLA_W12mo_2025-05-01_to_2026-04-30 ; do
  ./.venv/bin/python scripts/regenerate_cross_engine_study.py --cell "$cell"
done
```

Each cell's regen sequence:
1. Stage the capture into a workspace under `DEFAULT_ARTIFACTS_ROOT`.
2. Launch the arm64 derivative LEAN container against the workspace.
3. Read LEAN's `observations.csv`, `state.csv`, and `result.json` order events.
4. Run Engine Lab live against the same capture via `run_engine_lab_on_workspace`.
5. Run the three parity gates (`run_cell_gates`).
6. On pass: write `reconciliation_pinned.json`, atomically replace the committed cell directory, update the manifest.

## Result — all 4 W12mo cells pass Gate 3

| Cell | LEAN order events | LEAN fills | Engine Lab fills | gating divergences |
|---|---|---|---|---|
| `SPY_W12mo_2025-05-01_to_2026-04-30` | 144 | 72 | 72 | 0 |
| `QQQ_W12mo_2025-05-01_to_2026-04-30` | 224 | 112 | 112 | 0 |
| `AAPL_W12mo_2025-05-01_to_2026-04-30` | 72 | 36 | 36 | 0 |
| `TSLA_W12mo_2025-05-01_to_2026-04-30` | 272 | 136 | 136 | 0 |

Each cell's `reconciliation_pinned.json`:

```json
{
  "captured_at_ms_utc": ...,
  "status": "passed",
  "trade_summary": {
    "gating_divergent_count": 0,
    "passed": true
  }
}
```

Per-cell wall-clock (arm64 derivative LEAN + Engine Lab) was ~30 s. Total for the four-cell slice: ~120 s of LEAN compute, ~12 s of Engine Lab compute.

## Tests

```
cd PythonDataService
./.venv/bin/python -m pytest tests/research/parity/test_cross_engine_study.py \
    -v -m 'cross_engine_smoke or slow'
```

Result: `8 passed, 4 skipped in 9.48s`.

The 8 passing cells are the four W6mo (smoke) + the four newly-pinned W12mo (slow). The 4 skipped are the W24mo cells, which remain unpinned — see § "Out of scope" in the design spec.

## Trade summary — matching fills per cell

Both engines emit the **same fill count** per cell:

- SPY W12mo: 72 fills (engine) = 72 fills (LEAN)
- QQQ W12mo: 112 fills (engine) = 112 fills (LEAN)
- AAPL W12mo: 36 fills (engine) = 36 fills (LEAN)
- TSLA W12mo: 136 fills (engine) = 136 fills (LEAN)

Every fill matches LEAN's reference within tolerance: `fill_price_atol=$0.01`, `commission_atol=$0.01`, `qty_atol=0` (strict), `assert_fees=True`. `gating_divergent_count=0` on all four cells. Per the divergence taxonomy in `.claude/rules/numerical-rigor.md`, no `DECISION_MISMATCH`, `DIRECTION_MISMATCH`, `QUANTITY_MISMATCH`, `FILL_PRICE_DRIFT`, `COMMISSION_DRIFT`, `PNL_DRIFT`, `ORDER_TYPE_MISMATCH`, or `FIXTURE_INSUFFICIENT`.

## DIA — attempted, blocked, deferred

The request that motivated this slice asked for SPY/QQQ/TSLA/DIA. DIA was attempted with new infrastructure (capture script + matrix.py extension + 479 minute zips fetched from Polygon over the 24-month window). Gate 3 (trades) and Gate 1 (observations) both passed for DIA W12mo, but Gate 2 (per-bar state) failed with **15 `ts_ms_utc` mismatches out of 6477 state rows** (0.23%). Every mismatch was the same shape: LEAN emitted a bar-end timestamp 60000 ms (one minute) earlier than Engine Lab at the same row index, with no value mismatches on `close`/`ema_fast`/`ema_slow`/`rsi`/`cross_state`/`signal`.

Reading the LEAN trusted sample's `OnConsolidatedBar` and the Engine Lab consolidator side-by-side, the divergence is a `TradeBarConsolidator` semantic difference at sparse-trade boundaries: LEAN emits a *partial* bar with `EndTime` set to the actual last-trade minute (e.g., `13:59`) when no trades arrive in the last minute of the 15-minute window; Engine Lab emits a *clock-aligned* bar with `EndTime` set to the bar boundary (e.g., `14:00`). DIA exhibits this divergence 15 times in the W12mo window because it has lower per-minute liquidity than SPY/QQQ/AAPL/TSLA, where the last-minute-of-bar always has a trade and both engines converge on the clock-aligned `EndTime`.

Fixing this is out of scope for this PR. It needs an Engine Lab consolidator change to mirror LEAN's "actual-last-trade-time as EndTime when no terminal-minute trades" behavior. The DIA capture data and the precise list of 15 divergent timestamps have been kept locally under `/tmp/dia-capture-staged/` and `/tmp/dia-failed-report/` to accelerate the follow-up.

## Why this matters

The W12mo slice closes a real gap. Until this slice landed, the matrix tests skipped W12mo cells with "fixture missing", which meant a regression to any indicator, the consolidator, the fill model, the brokerage model, or the position-sizing primitive that only surfaced over a 1-year window would not be caught in CI. The W6mo slice covers ~125 trading days; the W12mo slice doubles that span and surfaces multi-quarter behavior (dividend pay dates, earnings releases, Fed meetings) that the shorter window misses. Pinning these cells turns those longer-horizon parity bugs into a blocking CI failure when they appear.

## References

- Design spec: `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`
- Matrix README: `PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md`
- Prior reconciliation: `docs/references/reconciliations/lean-vs-python-spy-ema-6day-2026-06-10.md` (PR #466 prerequisite)
- Divergence taxonomy: `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy"
