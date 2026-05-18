# Phase 5g.3 progress (2026-05-18, separate PR — diff + endpoint wire-up)

Phase 5g.1 (PR #271) shipped the endpoint scaffold + Pydantic request/response shapes. Phase 5g.2 (PR #273) shipped the `cross_runner` primitive. Phase 5g.3 wires the two together and adds the diff: replaces the 501 in `POST /runs/{id}/cross-reconcile` with a real `CrossEngineReconciliationReportModel`, classifying disagreements per `DivergenceCategory`.

## What this PR adds

- **New module** `app/lean_sidecar/cross_reconciler.py`:
  - `CrossReconciliationTolerances` — `fill_price_atol=0.01`, `commission_atol=0.01`, `qty_atol=0` (defaults per `numerical-rigor.md`).
  - `compare_cross_engine(lean_events, engine_events, *, tolerances=None, assert_fees=False)` — adapts both sides to a common internal shape, pairs by `(NY-trading-date, side)`, classifies disagreements into the `DivergenceCategory` enum from `qc_reconciler.py` (kept in lockstep — single source of truth for the taxonomy).
  - `CrossReconciliationOutput` — router-agnostic comparator result: per-category counts, gating count, `passed` flag, divergence rows.
  - Default gating taxonomy (per D3): every category gating EXCEPT `COMMISSION_DRIFT` (diagnostic). `assert_fees=True` promotes `COMMISSION_DRIFT` to gating (Branch-A — meaningful only on Phase 5b reconciliation-grade templates).
  - Filters LEAN-side events to `status == "Filled"` — `Submitted` / `Canceled` lifecycle events have no Engine-Lab counterpart and would produce spurious `DECISION_MISMATCH` rows.
- **Endpoint wire-up** in `app/routers/lean_sidecar.py`:
  - `POST /runs/{id}/cross-reconcile` no longer returns 501. The endpoint loads the LEAN-Lab run's `manifest.json` (for symbol / dates / starting cash), reads the persisted `normalized/result.json`, calls `run_engine_lab_on_workspace` with the workspace path, then calls `compare_cross_engine` and folds the output into `CrossEngineReconciliationReportModel` (`schema_version=1` per D10).
  - `_extract_cross_run_inputs_from_manifest` is the manifest-reader seam. It handles two schema versions: newer manifests carry `parameters.symbol` directly; older ones may have only `bars_consumed_by_symbol` as a single-key dict (the fallback infers the symbol from that key when unambiguous). Dates fall back from `parameters.start_date/end_date` to `requested_window_ms` (int64 ms UTC → NY trading dates). Starting cash falls back from `parameters.starting_cash` to top-level `starting_capital`. Each missing-field branch raises HTTP 400 with `reason: manifest_incomplete` and a `missing_field` hint.
  - `_build_cross_engine_report` converts the comparator's internal divergences to wire-shaped `CrossEngineDivergenceModel` rows.
- **Error contract** (mirrors Phase 5a where possible, adds the cross-run-specific branches):
  - **404 `run_not_found`** — invalid run_id, or workspace dir absent.
  - **404 `normalized_missing`** — workspace exists but `result.json` is absent / malformed.
  - **404 `manifest_missing`** — workspace exists but `manifest.json` is absent / malformed.
  - **400 `manifest_incomplete`** — manifest present but missing symbol / dates / cash (with `missing_field` hint).
  - **400 `strategy_not_found`** — caller named an Engine-Lab class that does not resolve. `detail` carries the known list (already produced by `resolve_strategy_class`).
  - **400 `strategy_incompatible`** — strategy resolved but does not accept the `symbol` kwarg required by the Phase 5g.2 contract.
  - **404 `workspace_data_missing`** — workspace exists but `data/` subtree gone (e.g., pruned between LEAN-Lab run and this cross-run).

## Test surface

- **`tests/lean_sidecar/test_cross_reconciler.py`** — 12 new unit tests covering: clean path, empty inputs, DECISION_MISMATCH (only one side has fill), QUANTITY_MISMATCH, FILL_PRICE_DRIFT (above + at atol boundary), COMMISSION_DRIFT diagnostic by default vs gating with `assert_fees=true`, non-Filled status filtering, custom tolerances widening acceptance, NY-trading-date pairing across the UTC midnight boundary.
- **`tests/lean_sidecar/test_router_lean_sidecar.py`** — replaced the two Phase 5g.1 501-asserting tests with 200-asserting end-to-end tests + new error-branch tests. Renamed the existing `_write_manifest` test helper to `_write_cross_run_manifest` to avoid the name collision with the `TestRunsIndex` fixture helper. New tests:
  - `test_endpoint_runs_engine_and_returns_real_report` — stages the workspace + LEAN result.json (empty) + manifest + minute bars; Engine-Lab buy-and-hold emits one Buy; expects 200 with `engine_total_fills>=1`, one `decision_mismatch` row, `passed=False`.
  - `test_404_when_manifest_missing` / `test_400_when_manifest_incomplete` — exercises the two manifest-failure branches.
  - `test_400_when_strategy_class_unknown` — caller-supplied class doesn't resolve.
  - `test_assert_fees_true_promotes_commission_drift_to_gating` — the Branch-A flag plumbs through the request shape and round-trips on the response.

## What this PR does NOT do

- **No `PNL_DRIFT`** — Phase 5g.3 is a fill-level diff. Round-trip pairing + realized-P&L reconciliation are out of scope; `qc_reconciler._pair_round_trips` could be re-used in a future slice if needed.
- **No `FIXTURE_INSUFFICIENT`** — both engines ran on the same workspace data zips (D3 shared staged data), so price-explainability audits are not applicable. The category remains in the gating set so wiring it later preserves the invariant.
- **No frontend UI** — Phase 5g.4 (next) adds the "Cross-engine reconcile" button + report panel.

## Build sequence

Phase 5g.4 — frontend UI for the cross-engine reconcile report:
- Add a "Cross-engine reconcile" button on the run-detail panel that opens a strategy-class picker + `assert_fees` toggle + submit.
- Render the report: passed/failed badge, divergence-category histogram, drill-down on each `CrossEngineDivergenceModel` with side-by-side `CrossEngineFillSnapshotModel` panes.
- Honor `schema_version` per D10 — UI must fail-fast (red error pane) on an unrecognized version rather than silently misrender.
