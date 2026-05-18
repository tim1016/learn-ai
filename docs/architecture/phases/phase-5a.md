# Phase 5a progress (2026-05-17, follow-up PR — self-reconciler against IBKR commission model)


First slice of the reconciliation-grade work. Ships the primitive that
every other Phase 5 deliverable depends on: a categorized comparison
of recorded fees against the canonical `IbkrEquityCommissionModel`
(which has lived in `app/research/parity/ibkr_commission.py` since
Engine Lab's QC reconciler work — Phase 5a consumes it without
duplicating the formula).

- **API** — `POST /api/lean-sidecar/runs/{id}/reconcile` returns
  `RunReconciliationReportModel { run_id, algorithm_id,
  normalized_parser_version, total_fill_events, matched_count,
  divergent_count, commission_atol, total_recorded_fees,
  total_expected_ibkr_fees, divergences[] }`. Reads the **persisted**
  `result.json` for the run (`<workspace>/normalized/result.json`,
  written by the orchestrator at run time), walks filled events,
  computes the expected IBKR fee per event, classifies each as clean
  / `commission_drift` / `no_recorded_fee`. Tolerance is the
  numerical-rigor.md default ($0.01). The endpoint deliberately does
  not re-parse LEAN's raw output artifacts on each call — the pinned
  `parser_version` on disk is what the report is computed against, so
  a future parser bump cannot retroactively alter an old reconciliation
  result. The pin is echoed on the response as
  `normalized_parser_version`.
- **Reconciler module** — `app/lean_sidecar/reconciler.py` is pure
  functions over `NormalizedOrderEvent` iterables. Three exports:
  `FeeDivergenceCategory`, `FeeReconciliationReport`,
  `reconcile_against_ibkr`. The categories are a strict subset of the
  project-wide `DivergenceCategory` so consumers can lift them into
  the broader taxonomy without translation.
- **Decoupled from template choice.** A trusted-sample run that used
  LEAN's default brokerage will produce a report full of
  `commission_drift` rows — that's *expected* and informative (it
  shows the brokerage choice matters). The clean-vs-drift signal only
  becomes interpretable as "Engine-Lab-comparable" once the Phase 5b
  reconciliation-grade template pins IBKR brokerage explicitly.
- **Decimal hygiene on the wire.** All money values cross the API as
  strings (not floats) so JSON serialization is exact. The reconciler
  quantizes both recorded and expected fees to cents internally so the
  $0.01 tolerance is meaningful at the cent boundary.
- **What 5a does NOT do** — does not modify any run, does not include
  the reconciliation-grade template (Phase 5b), does not surface the
  report in the UI (Phase 5c), does not handle quote bars / factor
  files / benchmark staging (separate Phase 5b+ work items).
- **Test surface** — 19 unit tests on the pure reconciler (empty list,
  non-filled events excluded, status case insensitivity, clean run,
  drift detection, no-recorded-fee categorization, tolerance boundary,
  aggregate totals, negative quantity, custom atol, custom model, edge
  cases: zero qty, percentage cap, parametrized boundary classification);
  5 endpoint integration tests (clean run, drift surface, 404 on missing
  workspace, 404 on missing normalized, invalid run_id rejection).
  199 lean_sidecar tests pass + 1 skip.
