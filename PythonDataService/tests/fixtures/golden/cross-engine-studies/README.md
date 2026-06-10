# Cross-Engine Golden-Fixture Matrix

12 cells = 4 tickers (SPY, QQQ, AAPL, TSLA) × 3 nested windows (W6mo / W12mo / W24mo, all ending 2026-04-30). Each cell pins LEAN orders.json + state.csv + observations.csv as the reference; Engine Lab runs live at test time.

Authoritative design: `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`.

## Layout

- `_lean_data_capture/<TICKER>/` — shared 24mo minute capture per ticker (LEAN deci-cent zips). Three cells per ticker read from this single capture.
- `cells/<CELL_ID>/` — one directory per (ticker, window). Contains `manifest.json`, `attribution.md`, `lean/`, `reconciliation_pinned.json`.

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
slow-marked per the design spec; W24mo cells remain unpinned.
