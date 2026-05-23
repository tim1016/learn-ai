# Cross-Engine Golden-Fixture Matrix — Design

**Status:** Approved design (2026-05-21). Implementation plan to follow.
**Owner:** Tim
**Slice:** 1 of N — tickers × time windows. Future slices add adjustment modes, alternate strategies, alternate brokerage / fill models.

## Goal

Expand the existing LEAN-Sidecar-vs-Engine-Lab cross-engine parity check from a single (SPY, 5-day, EMA-crossover) cell to a **matrix of 12 cells = 4 tickers × 3 nested time windows**, with each cell's LEAN output pinned as a golden fixture. The pinned fixtures are the reference against which future Engine Lab changes are validated.

The matrix is the *outcome* layer. The mechanism the matrix exists to validate is the *causes* layer: every per-bar indicator value (`ema_fast`, `ema_slow`, `rsi`), every crossover-state transition, every signal, and every fill must match LEAN within explicit tolerance. Trade-only parity tells us *that* something broke; per-bar state parity tells us *where*.

This slice does not touch portfolio valuation, cash accounting, or equity-curve reconciliation — those land in a later slice when multi-position or portfolio-math logic gets changes.

## Scope

### In scope (this slice)

- Strategy harness: the existing trusted sample `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py` — EMA(5)/EMA(10) crossover with RSI(14, Wilders) gate on 15-min consolidated bars, `EXIT_BARS=5` time-stop. Strategy constants are pinned in the template source.
- Runtime parameters per cell: `symbol`, `start_date`, `end_date`, `bar_minutes=15`, `session=regular`, `adjustment=raw`, `starting_cash=100000`.
- Brokerage: Interactive Brokers, margin account, `ImmediateFillModel`, `IbkrEquityCommissionModel`. Branch A — fees recorded — so `COMMISSION_DRIFT` is gating.
- 12 golden cells; each cell's `lean/` outputs pinned, Engine Lab runs live at test time.
- Three parity gates per cell: observations identical, per-bar state identical (within tolerance), trade-level reconciler `passed`.

### Explicitly out of scope (deferred to future slices)

- Adjustment-mode variation (`raw` vs `adjusted`). The current trusted-sample template `raise`s on anything other than `raw`. Splits / dividends therefore can't be validated this slice. Tickers in this matrix are picked to be split-free across the full 24-month window.
- Alternate strategies (RSI mean-reversion, MACD crossover, multi-symbol top-N).
- Alternate fill models (LEAN `EquityFillModel`, bid/ask-aware fills) and alternate brokerage models.
- Equity-curve / mark-to-market parity. Trade + indicator + observation parity is sufficient for this slice; portfolio MTM is a deterministic function of those three and adds duplicate coverage today.
- Nightly cron / scheduled-job wiring. No cron is registered in this repo yet (per memory); this study runs on demand and in CI per § "CI / test strategy."

## Matrix

### Tickers (4)

| Ticker | Why | Splits in 24mo window |
|---|---|---|
| **SPY** | Broad-market baseline. Deepest liquidity. Control cell. | None |
| **QQQ** | Tech-ETF baseline. Different volatility profile from SPY. | None |
| **AAPL** | Large-cap single name. Dividend-paying. Validates non-ETF microstructure. | None (last split Aug 2020) |
| **TSLA** | High-vol single name with frequent gaps and occasional halts. Stress-tests indicator stability and fill ordering through gaps. | None (last split Aug 2022) |

NVDA was considered but excluded — its 10-for-1 split on 2024-06-10 lands inside the 24-month window, and the `raw` adjustment mode required by the strategy template would produce a 90% gap-down at the split, firing spurious signals. NVDA returns when adjustment-mode variation lands in a later slice.

### Windows (3, nested, shared end date)

All windows end **2026-04-30**. Start dates are Mondays so warmup begins on a clean session.

| Window | Start | End | Trading days | 15-min bars after RTH filter | Expected trades (EMA-5/10, rough) |
|---|---|---|---|---|---|
| **W6mo** | 2025-11-03 | 2026-04-30 | ~125 | ~3,250 | ~15–30 |
| **W12mo** | 2025-05-01 | 2026-04-30 | ~252 | ~6,550 | ~30–60 |
| **W24mo** | 2024-06-03 | 2026-04-30 | ~480 | ~12,480 | ~60–120 |

Polygon Starter plan caps history at 2 years (today is 2026-05-21), so 24 months is the maximum reachable depth. The W24mo start date sits just inside that limit with a working margin.

### Cells

**4 tickers × 3 windows = 12 cells.** Each cell is one independent LEAN sidecar run (approach (b1) — see § "Run topology"). All three windows for a given ticker read from the same shared 24-month minute capture.

## Run topology — three independent LEAN runs per ticker

Each `(ticker, window)` cell triggers an **independent** LEAN sidecar run with:

- `StartDate` = window start
- `EndDate` = 2026-04-30
- All other parameters identical across cells for a given ticker

The alternative — one 24mo LEAN run sliced into 6mo/12mo views — was rejected because strategy state (`in_trade`, `bars_held`, `prev_fast`, `prev_slow`) is path-dependent: a sliced view inherits the 24mo run's state at the sub-window's start date, which is not what a researcher starting fresh at 2025-05-01 would experience. Indicator values converge to the same trajectory after ~70 bars regardless of start date, but the strategy state machine does not reset, so sliced views and independent runs produce different trade sequences during the first weeks of any sub-window.

### Shared-tail invariant (diagnostic, not gating)

When two nested runs (e.g., W12mo and W24mo) reach their shared 6-month tail, they should — after a burn-in — produce identical trade sequences and identical per-bar indicator values. This is a free invariant from the nested-window structure.

- **Primary gate per cell:** LEAN-vs-Engine parity *within that cell*, run independently. Zero gating-category divergences + zero state per-bar failures + observations identical.
- **Shared-tail check:** diagnostic only. Burn-in defined operationally as **70 consolidated bars from the sub-window start, or until both runs return to `in_trade=False` simultaneously, whichever is longer.** After burn-in, disagreement between LEAN-on-W12mo and LEAN-on-W24mo (or any nested pair) is logged for inspection but does not fail the gate. Same for Engine Lab on the same pair. The point is to *characterize* warmup behavior, not gate on it.

## Storage layout

```
PythonDataService/tests/fixtures/golden/cross-engine-studies/
├── README.md                                       # Index, refresh policy, layout
├── _lean_data_capture/                              # One shared 24mo minute capture per ticker
│   ├── SPY/
│   │   ├── attribution.md                          # Polygon fetch metadata
│   │   ├── manifest.json                           # capture_ms_utc, span, source, data_contract_hash
│   │   └── equity/usa/minute/spy/20240603_trade.zip, ...   # LEAN deci-cent zips
│   ├── QQQ/
│   ├── AAPL/
│   └── TSLA/
└── cells/
    ├── SPY_W24mo_2024-06-03_to_2026-04-30/
    │   ├── attribution.md
    │   ├── manifest.json
    │   ├── lean/
    │   │   ├── orders.json
    │   │   ├── state.csv                           # Full-precision Decimal strings
    │   │   └── observations.csv
    │   └── reconciliation_pinned.json              # Written iff all gates pass at capture
    ├── SPY_W12mo_2025-05-01_to_2026-04-30/
    ├── SPY_W6mo_2025-11-03_to_2026-04-30/
    └── … (12 cells)
```

Cell directory name format: `<TICKER>_<WINDOW_LABEL>_<START_YYYY-MM-DD>_to_<END_YYYY-MM-DD>/`.

### Shared `_lean_data_capture/` — one capture per ticker, not per cell

The 24mo minute capture for a given ticker is fetched once and read by all three of that ticker's LEAN runs. The cell manifest's `lean_data_capture_ref` + `data_contract_hash` give per-cell isolation without duplicating zips. Per-cell duplication was rejected: ~3× the storage and a new drift surface if captures fall out of sync.

## Cell `manifest.json` schema (v1)

```jsonc
{
  "schema_version": 1,
  "cell_id": "SPY_W24mo_2024-06-03_to_2026-04-30",
  "ticker": "SPY",
  "window": {
    "label": "W24mo",
    "start_date": "2024-06-03",
    "end_date": "2026-04-30",
    "session": "regular",
    "trading_days_expected": 480
  },
  "strategy": {
    "trusted_sample": "ema_crossover",
    "trusted_sample_source_sha256": "<sha256 of EMA_CROSSOVER_SOURCE>",
    "parameters_constants": {
      "FAST_PERIOD": 5, "SLOW_PERIOD": 10, "RSI_PERIOD": 14,
      "EXIT_BARS": 5, "GAP_MIN": 0.20, "RSI_LO": 50, "RSI_HI": 70
    },
    "runtime_parameters": {
      "bar_minutes": 15,
      "adjustment": "raw",
      "starting_cash": 100000
    }
  },
  "data": {
    "lean_data_capture_ref": "_lean_data_capture/SPY",
    "data_contract_hash": "<sha256 of capture manifest>"
  },
  "broker": {
    "brokerage_model": "InteractiveBrokersBrokerage",
    "account_type": "Margin",
    "fill_model": "ImmediateFillModel",
    "fee_model": "IbkrEquityCommissionModel"
  },
  "lean_runtime": {
    "container_image_digest": "docker.io/quantconnect/lean@sha256:<digest>"
  },
  "artifacts": {
    "orders_sha256": "<sha256 of lean/orders.json>",
    "state_sha256": "<sha256 of lean/state.csv>",
    "observations_sha256": "<sha256 of lean/observations.csv>",
    "reconciliation_sha256": "<sha256 of reconciliation_pinned.json>"
  },
  "state_csv_schema": {
    "columns": ["ts_ms_utc", "close", "ema_fast", "ema_slow", "rsi", "cross_state", "signal"],
    "column_types": {
      "ts_ms_utc": "int64",
      "close": "decimal_string",
      "ema_fast": "decimal_string",
      "ema_slow": "decimal_string",
      "rsi": "decimal_string",
      "cross_state": "string_enum:above|below|equal",
      "signal": "string_enum:HOLD|ENTER|EXIT"
    }
  },
  "timezone": "America/New_York",
  "timestamp_convention": "int64_ms_utc",
  "fixture_git_commit": "<git HEAD at capture>",
  "python_data_service_commit": "<git HEAD at capture, redundant with fixture_git_commit when monorepo>",
  "generator_script_sha256": "<sha256 of regenerate_cross_engine_study.py at capture>",
  "captured_by": "<git user.name>",
  "captured_at_ms_utc": 1779849600000
}
```

Future schema changes bump `schema_version` and gain a migration note in `attribution.md`. Pinning `state_csv_schema` in the manifest means any unannounced schema change to the emitter fails the parity test intentionally.

## Tolerances and acceptance gates

Three gates run in order. Earlier gates failing implies later gates would be uninterpretable, so a downstream gate is not evaluated when an upstream gate fails.

### Gate 1 — Observations parity (input-stream check)

Engine-live `observations.csv` vs pinned LEAN `observations.csv`:

| Field | Tolerance |
|---|---|
| `ts_ms_utc` | Exact equality (`int64`) |
| `open`, `high`, `low`, `close`, `volume` | Exact equality as `Decimal` parsed from string |
| Row count and order | Exact equality |

If observations diverge, the consolidated state is uninterpretable — a divergent minute stream upstream of consolidation explains everything downstream. Gate 2 and Gate 3 are skipped, the failure report names Gate 1.

### Gate 2 — Per-bar state parity (causes layer)

Engine-live `state.csv` vs pinned LEAN `state.csv`, aligned by `ts_ms_utc`:

| Field | Tolerance | Notes |
|---|---|---|
| `ts_ms_utc` | Exact equality | Alignment key; mismatch fails immediately |
| `close` | Exact equality as `Decimal` from string | Both engines consume the same minute zips and consolidate deterministically |
| `ema_fast` | `atol=1e-9, rtol=0` | LEAN uses SMA-seeded EMA — verify Engine matches (see § "Implementation verification") |
| `ema_slow` | `atol=1e-9, rtol=0` | Same |
| `rsi` | `atol=1e-9, rtol=0` | `app/engine/indicators/rsi.py` is a hand-rolled Wilder port pinned to LEAN commit `7986ed0aade3ae5de06121682409f05984e32ff7` and uses `Decimal` arithmetic, so this tolerance is comfortable |
| `cross_state` | Exact equality | Enum: `above` / `below` / `equal` |
| `signal` | Exact equality | Enum: `HOLD` / `ENTER` / `EXIT` |

Both engines emit `state.csv` rows only after all three indicators are `IsReady=True`, so warmup bars are excluded by construction. No separate warmup tolerance is needed.

State files must emit **full-precision numeric strings**. If the existing emitter rounds for display, fix the emitter — do not loosen the tolerance.

### Gate 3 — Trade-level parity (outcomes layer)

Engine-live fills vs pinned LEAN fills, via the existing `app/lean_sidecar/cross_reconciler.py` with `CrossReconciliationTolerances`:

| Tolerance | Value | Notes |
|---|---|---|
| `fill_price_atol` | `$0.01` | LEAN `ImmediateFillModel` fills at `bar.EndTime`/`bar.Close`, matching Engine Lab's `signal_bar_close` mode |
| `commission_atol` | `$0.01` | IBKR tiered formula is deterministic; both engines pin IBKR brokerage |
| `qty_atol` | `0` (strict) | Both engines size off the same cash + bar-close at signal time |
| `assert_fees` | `True` | Branch A: LEAN trusted sample pins IBKR brokerage, so `COMMISSION_DRIFT` is gating |

Gating divergence categories: `DECISION_MISMATCH`, `DIRECTION_MISMATCH`, `QUANTITY_MISMATCH`, `FILL_PRICE_DRIFT`, `ORDER_TYPE_MISMATCH`, `PNL_DRIFT`, `FIXTURE_INSUFFICIENT`, plus `COMMISSION_DRIFT` (Branch A).

### Acceptance per cell

A cell **passes** iff all three gates pass: observations identical, zero state-row failures, cross-reconciler report `status == "passed"`.

### Slice acceptance — W6mo (current state, 2026-05-22)

The IBKR-margin brokerage contract is now locked end-to-end:

- LEAN template (`PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`) calls `SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)` before `AddEquity`.
- Cell manifest `broker` block declares `brokerage_model: InteractiveBrokersBrokerage` and `fee_model: InteractiveBrokersFeeModel`.
- Engine side wires `FillModel(fee_model=IbkrEquityCommissionModel(), fill_stale_signal_at_current_open=True)` and `LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())` via the cross-runner; `cell_runner.run_cell_gates` defaults `assert_fees=True`.

**Cells passing Gate 3 under this contract:**

- **SPY_W6mo_2025-11-03_to_2026-04-30** — passing; 20 LEAN trades, zero gating divergences.
- **QQQ_W6mo_2025-11-03_to_2026-04-30** — passing; 66 LEAN trades, zero gating divergences.
- **AAPL_W6mo_2025-11-03_to_2026-04-30** — passing; 20 LEAN trades, zero gating divergences.
- **TSLA_W6mo_2025-11-03_to_2026-04-30** — passing; 74 LEAN trades, zero gating divergences.

The previous QQQ / AAPL / TSLA blocker was the cross-session exit-fill semantic:
stale consolidated bars emitted after an overnight/weekend gap were filled at
the old consolidated close in Engine Lab but at the current minute open in
LEAN. Engine Lab now keeps the historical `SIGNAL_BAR_CLOSE` default and
enables the LEAN-compatible stale-signal policy only for matrix runs.

## Regeneration policy

### Triggers (only these)

1. LEAN container image digest changes.
2. Trusted-sample source changes (strategy constants or strategy logic — not runtime parameter passthrough).
3. Deliberate refresh after an audit changed the contract.

No quarterly regeneration. No time-based refresh. A golden fixture should be boring and frozen; time-based refreshes quietly change the target. Freshness concerns go to a separate canary / audit job, not to fixture regeneration.

### Regeneration workflow

Script: `PythonDataService/scripts/regenerate_cross_engine_study.py`.

CLI: `--cell <cell_id>` for one cell, `--all` for the full matrix, `--ticker <TICKER>` for one ticker's three cells.

Per-cell sequence:

1. **Pre-flight:** verify the shared `_lean_data_capture/<TICKER>/` exists and its `manifest.json` `data_contract_hash` matches the capture's content hash. If absent or stale, refuse to proceed (capture refresh is its own workflow, not piggy-backed onto cell regen).
2. **Run LEAN sidecar** for `(ticker, window)` against the shared capture. Collect `orders.json`, `state.csv`, `observations.csv`, `result.json`.
3. **Stage outputs** into a temporary directory (do *not* touch the committed cell directory yet).
4. **Run Engine Lab live** against the same capture with the same runtime parameters.
5. **Run all three gates** (observations, state, trade-level) staged-vs-Engine.
6. **On pass:** write `reconciliation_pinned.json`, replace the committed cell directory atomically, update the manifest with new artifact hashes, capture timestamp, generator script hash, and `fixture_git_commit`.
7. **On fail:** emit the failure report to a sibling `.failed/` directory, exit non-zero, leave the committed cell directory **untouched**.

This makes "cell directory present in git" equivalent to "all three gates passed at capture time" — the invariant the regeneration workflow exists to preserve.

PR discipline: every regeneration PR description names which of the three triggers applies and includes the script output. Mirror of the `numerical-rigor.md` rule for any golden-fixture regen.

## CI / test strategy

Approach (b) — split by window length:

- **Smoke (4 cells, normal CI on every PR):** SPY_W6mo, QQQ_W6mo, AAPL_W6mo, TSLA_W6mo. Marked `@pytest.mark.cross_engine_smoke`. ~5–10 s per cell → ~30–40 s added to every PR run. Surfaces parity regressions on any indicator across every ticker without manual intervention.
- **Slow (8 cells, run pre-push and on explicit invocation):** all W12mo + W24mo cells. Marked `@pytest.mark.slow` per the existing convention (`-k "not slow"` is the fast filter per `.claude/CLAUDE.md` and `pytest.ini`). Run via `podman exec polygon-data-service python -m pytest tests/ -v -m cross_engine_study` or as part of a project-scope test run before pushing.

Iteration command for the smoke set alone: `pytest -m cross_engine_smoke`.

No nightly cron is registered; when one is wired, it should include the slow set. That wiring is a separate work item.

## Engine-side requirements (live test runtime)

Engine Lab reads minute bars from the same `_lean_data_capture/<TICKER>/` directory the LEAN sidecar read from. This is wired via the existing data-lake / `LEAN_DATA_WRITE_ROOT` plumbing; the parity test sets the data root to the capture directory before invoking Engine Lab.

Engine Lab's emitted `observations.csv` and `state.csv` use the same column schema as the LEAN trusted sample (`ts_ms_utc`, `close`, `ema_fast`, `ema_slow`, `rsi`, `cross_state`, `signal`) with full-precision Decimal string formatting. Schema drift between the two emitters is detected by Gate 2's exact column-list check against the pinned `state_csv_schema`.

## Implementation verification — gotchas to confirm before generating fixtures

Items to verify during implementation, before running the regeneration script for the first cell. None are expected to block; each is a "this could surface as a Gate 2 failure on day 1 if we don't check."

1. **State.csv emitter precision** — confirm the existing LEAN trusted sample and the Engine Lab state emitter both write Decimal in full precision (no `f"{x:.6f}"`-style rounding). If either rounds, fix the emitter.
2. **LEAN `ExponentialMovingAverage` seeding** — LEAN seeds EMA from the SMA of the first N samples. Verify `app/engine/indicators/exponential_moving_average.py` matches this convention (not pandas-ta's "first-value seeded" variant).
3. **RSI Wilder smoothing parity** — `app/engine/indicators/rsi.py` is already pinned to LEAN's Wilder implementation per its provenance block. Confirm no other code path bypasses this module.
4. **`cross_state` definition agreement** — both engines must use the same `above` / `below` / `equal` rule. Trusted sample uses `>`, `<`, and `else equal`; Engine Lab must match.
5. **Consolidator alignment** — LEAN's `TradeBarConsolidator(timedelta(minutes=15))` and Engine Lab's 15-min consolidator must agree on bar boundaries (exchange-aligned `:00`/`:15`/`:30`/`:45`). A 1-minute offset surfaces as universal Gate 1 failure.
6. **LEAN container image digest** — pin a specific `docker.io/quantconnect/lean@sha256:<digest>` and record it in every cell's `manifest.json`. An unpinned tag breaks the regeneration determinism premise.

## Out-of-scope future slices (named so this slice doesn't drift into them)

- **Slice 2 — adjustment mode:** allow `adjustment=adjusted` in the trusted-sample template (currently `raise`s); add NVDA across its split, plus a corp-action-bearing AAPL window if any lands.
- **Slice 3 — alternate strategies:** RSI mean-reversion, MACD crossover, Bollinger mean-reversion. Each gets its own trusted sample, matching Engine Lab spec, and its own ticker-window matrix.
- **Slice 4 — alternate fill models:** LEAN `EquityFillModel` bid/ask-aware fills; matching Engine Lab fill model port; tolerances revisit.
- **Slice 5 — equity-curve / portfolio MTM parity:** add per-bar equity curve to the pinned outputs; add Gate 4 for portfolio-math regressions. Triggered when multi-position or cash-accounting logic is touched.
- **Nightly cron wiring:** schedule the full matrix nightly once a cron substrate exists.

## References

- Existing trusted sample: `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`
- Existing matching Engine Lab spec: `PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json`
- Cross-engine reconciler: `PythonDataService/app/lean_sidecar/cross_reconciler.py`
- Divergence taxonomy: `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy"
- Precedent fixture pattern: `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/` + `docs/references/reconciliations/qc-aapl-phase3.md`
- Fill-model parity reference: `docs/references/fill-model-parity-spike-2026-05-19.md`
- Data lake design: `docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md` (and Slice 1a–1c plans)
- LEAN Sidecar Lab ADR: `docs/architecture/lean-sidecar-lab.md`
