# Phase 5g progress (2026-05-18 onward — cross-engine reconciler)

Phase 5a was *self*-reconciliation: this LEAN-Lab run's recorded fees vs
the canonical `IbkrEquityCommissionModel`. Phase 5g is *cross-engine*
reconciliation: diff this LEAN-Lab run's fills against the Engine Lab's
fills for the caller-named strategy class on the same workspace data.

Phase 5g unblocked by mission-critical doc D3 (resolved 2026-05-18):

- Pairing is **caller-supplied** — no auto-derivation. The request names
  the Engine Lab strategy class to diff against.
- Default gating taxonomy is **strict**: every `DivergenceCategory` is
  gating EXCEPT `COMMISSION_DRIFT`, which defaults to diagnostic. The
  caller may opt in via `assert_fees=true` to promote it (Branch-A
  semantics — only meaningful on Phase 5b reconciliation-grade templates
  where IBKR fees are pinned on both sides).
- Data source is **shared staged data** — the Engine Lab runs against
  the same `workspace/data` zips LEAN saw, not its native fixtures.

The slice is split into four PRs:

1. **Phase 5g.1** — endpoint scaffold + Pydantic request/response shape
   (no engine-lab call yet)
2. **Phase 5g.2** — Engine Lab cross-run primitive that accepts a
   workspace path
3. **Phase 5g.3** — diff against `DivergenceCategory`; honor
   `assert_fees` Branch-A semantics
4. **Phase 5g.4** — frontend UI ("Cross-engine reconcile" button on the
   run detail panel)

## Current status (2026-06-10)

Phase 5g.3 is wired in `app/routers/lean_sidecar.py`: `POST
/api/lean-sidecar/runs/{run_id}/cross-reconcile` now loads the LEAN
normalized result, extracts symbol/date/cash inputs from the run
manifest, runs the caller-named Engine Lab strategy on the same staged
workspace data, and returns a real
`CrossEngineReconciliationReportModel`. The old 501 scaffold contract
below is retained as Phase 5g.1 history only.

## Phase 5g.1 (this PR) — endpoint scaffold + request/response shape

The cross-reconcile endpoint exists and validates the request, but the
engine-lab call is the Phase 5g.2 slice. Scaffold returns 501 with a
structured detail so the frontend can already differentiate "feature
landed but unwired" from "feature absent".

- **Endpoint**: `POST /api/lean-sidecar/runs/{run_id}/cross-reconcile`.
  Path-param `run_id` carries the LEAN-Lab workspace slug; request body
  carries the Engine Lab strategy class + opt-in `assert_fees`.
- **Request shape** (`CrossReconcileRequestModel`):
  - `engine_lab_strategy_class: str` (required, 1–200 chars) — no
    auto-derivation per D3.
  - `assert_fees: bool` (default false) — when true, COMMISSION_DRIFT
    joins the gating set. Only meaningful on reconciliation-grade
    templates (Phase 5b).
  - `extra="forbid"` per the rest of the lean_sidecar request models —
    a typo on `assert_fees` must 422, not silently default.
- **Response shape** (`CrossEngineReconciliationReportModel`):
  - `schema_version: int = 1` per D10. The Phase 5g.4 UI MUST fail-fast
    on an unrecognized version rather than silently misrender.
  - `run_id`, `engine_lab_strategy_class`, `assert_fees` echoed back so
    a stored report carries its own context.
  - `lean_total_fills`, `engine_total_fills`, `matched_count`,
    `divergent_count`, `gating_divergent_count`, `passed: bool`.
  - `counts_by_category: dict[DivergenceCategory, int]` for at-a-glance
    diff shape.
  - `divergences: list[CrossEngineDivergenceModel]` — each carries
    `category`, the NY trading date, a free-form `detail`, and optional
    per-side fill snapshots (one side is null for `decision_mismatch`).
- **Divergence category Literal** kept in lockstep with
  `research.parity.qc_reconciler.DivergenceCategory`. The router does
  not import the enum at runtime — copying the eight values into a
  `Literal` keeps the wire enumeration pinned without making the router
  depend on the parity package. A lint-time consistency check is
  feasible later; the test surface here uses real values so a divergence
  drift would surface in CI on Phase 5g.3.
- **501 contract**: detail carries `reason: "engine_lab_not_wired"`,
  echoes the request's `engine_lab_strategy_class` + `assert_fees`, and
  pins `schema_version: 1`. A frontend can branch on the reason without
  parsing prose, and the user-facing error is honest about which slice
  hasn't landed.

### Test surface

- `test_returns_501_with_structured_reason_when_run_and_result_present`
  — the contract test. When workspace + result.json are both present
  (i.e., the request is fully valid), the scaffold returns 501 with the
  exact echoed-fields shape Phase 5g.2+ promises to maintain on the 200
  path.
- `test_assert_fees_true_echoed_on_scaffold_response` — the Branch-A
  flag must round-trip through the request shape before Phase 5g.3
  uses it to flip COMMISSION_DRIFT gating.
- `test_404_when_workspace_missing`, `test_404_when_normalized_missing`,
  `test_404_when_result_json_malformed`, and
  `test_invalid_run_id_rejected_at_cross_reconcile` mirror the Phase 5a
  self-reconciler endpoint's negative paths — same `reason` codes so a
  frontend that handles one branch handles both.
- `test_422_when_strategy_class_missing` and
  `test_422_when_strategy_class_empty_string` lock in the D3 "no
  auto-derivation" rule.
- `test_422_when_extra_fields_passed` locks in `extra='forbid'`.
- `test_response_model_exposed_in_openapi_schema` — Phase 5g.4 can
  codegen against the wire shape now; this test fails the day a future
  edit accidentally drops the response_model from the route decorator.

### What this PR does NOT do

- No engine-lab call. The endpoint always 501s on the happy path. Phase
  5g.2 replaces the `raise HTTPException(501, ...)` with the actual diff.
- No backward-compatibility shim for legacy clients — the endpoint is
  new in this PR; the only consumer is Phase 5g.4 once it lands.
- No frontend UI. Phase 5g.4 adds the "Cross-engine reconcile" button +
  panel.
