# Cross-Engine Golden-Fixture Matrix

12 cells = 4 tickers (SPY, QQQ, AAPL, TSLA) × 3 nested windows (W6mo / W12mo / W24mo, all ending 2026-04-30). Each cell pins LEAN orders.json + state.csv + observations.csv as the reference; Engine Lab runs live at test time.

The matrix code is `app/lean_sidecar/parity_matrix/`. This README holds the gate order, the tolerances and the regeneration policy.

## Layout

- `_lean_data_capture/<TICKER>/` — shared 24mo minute capture per ticker (LEAN deci-cent zips). Three cells per ticker read from this single capture.
- `cells/<CELL_ID>/` — one directory per (ticker, window). Contains `manifest.json`, `attribution.md`, `lean/`, `reconciliation_pinned.json`.

## Gates and tolerances

Three gates run in order: observations, then per-bar state, then trades. A downstream gate is not evaluated when an upstream gate fails. A divergent minute stream upstream of consolidation explains everything downstream, so state and trades would be uninterpretable; a state divergence likewise explains any trade divergence.

**Gate 1 — observations** (`observations.csv`). `ts_ms_utc`, `open`, `high`, `low`, `close` and `volume` (as `Decimal` parsed from string), row count and row order are all exact. Both engines read the same minute zips, so any difference is an input-stream divergence, never a numerical one.

**Gate 2 — per-bar state** (`state.csv`, aligned by `ts_ms_utc`).
- `ts_ms_utc` is exact: it is the alignment key, and a mismatch fails at once.
- `close` is exact: both engines consume the same minute zips and consolidate deterministically.
- `cross_state` and `signal` are exact: they are enums.
- `ema_fast`, `ema_slow` and `rsi` use `atol=1e-9, rtol=0`, the strict-float default for indicators.
- Both engines emit state rows only after every indicator is ready, so warmup is excluded by construction and needs no tolerance of its own.
- State files must carry full-precision numeric strings. If an emitter rounds for display, fix the emitter; do not loosen the tolerance.

**Gate 3 — trades** (`app/lean_sidecar/cross_reconciler.py`).
- `qty_atol=0`: both engines size off the same cash and the same bar close at signal time.
- `fill_price_atol=$0.01`: LEAN's `ImmediateFillModel` fills at `bar.EndTime` / `bar.Close`, which Engine Lab's `signal_bar_close` mode matches.
- `commission_atol=$0.01` with `assert_fees=True` (Branch A): the trusted sample pins IBKR brokerage and the IBKR tiered formula is deterministic, so `COMMISSION_DRIFT` gates.

A cell passes only when all three gates pass.

## Regeneration

Triggers (only these):
1. LEAN container image digest changes.
2. Trusted-sample source changes.
3. Deliberate refresh after a parity audit changed the contract.

No quarterly regen. Freshness checks belong in a separate canary job, not here.

Workflow: `python scripts/regenerate_cross_engine_study.py --cell <id> | --ticker <T> | --all`. The script refuses to write a cell directory unless all three gates pass.

## Tests

- Smoke (every PR): `pytest -m cross_engine_smoke` — runs the four W6mo cells.
- Full (pre-push / nightly): `pytest -m 'cross_engine_smoke or slow' tests/research/parity/test_cross_engine_study.py` — runs all 12 cells. Plain `-m slow` would skip the W6mo cells because they carry only the `cross_engine_smoke` marker, not `slow`.

## Acceptance status

The matrix locks IBKR-margin brokerage as the contract:
`SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)`
on the LEAN side, `FillModel(fee_model=IbkrEquityCommissionModel())` +
`LeanSetHoldingsSizing(fee_model=...)` on the engine side. `assert_fees=True`
gates Gate 3.

Current state:
- **SPY W6mo** — regenerated and passing Gate 3 with zero gating divergences.
- **QQQ / AAPL / TSLA W6mo** — regenerated on 2026-05-23 after the engine
  gained the LEAN stale-signal fill policy for cross-session exits. All
  three pass Gate 3 with zero gating divergences under the IBKR-margin
  contract.
- **SPY / QQQ / AAPL / TSLA W12mo** — pinned on 2026-06-10 after the
  AppleHV SIGILL fix (PR #466) made the wide-window LEAN runs reachable on
  arm64. All four W12mo cells pass Gate 3 with zero gating divergences
  under the same IBKR-margin contract.

The smoke marker covers the four W6mo cells. The W12mo cells are
slow-marked; W24mo cells remain unpinned.

DIA was attempted for W12mo on 2026-06-10 and deferred. Gates 1 and 3 passed,
but Gate 2 failed on 15 of 6,477 state rows: when the last minute of a
15-minute window has no trade, LEAN's `TradeBarConsolidator` stamps the bar's
`EndTime` at the last traded minute, while Engine Lab stamps the clock-aligned
boundary. Matching it needs an Engine Lab consolidator change.
