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
- Full (pre-push / nightly): `pytest -m slow tests/research/parity/test_cross_engine_study.py` — runs all 12.
