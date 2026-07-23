> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, ADR-0026, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** This extraction plan is historical implementation context, not current authority.

# Live-control-plane extraction plan

**Decision:** #1125 is containment, not a behavior rewrite. The router remains the HTTP boundary; source ownership, orchestration, and process-local runtime state move to services in independently mergeable slices. No slice may alter broker write semantics, halt behavior, or strategy/fill logic.

## Pilot completed in this PR

`DaemonDiagnosticsService` now owns authenticated daemon-health retrieval, browser-safe redaction, renewal response validation, and typed health-probe failures. The router owns only route declaration plus the established HTTP status translation. The unchanged router contract is pinned by the existing daemon-health, diagnose, and renewal cases; new service tests characterize redaction, transport classification, renewal validation, and invalid envelopes.

This is deliberately the pilot: it is coherent, has no broker write path, reuses an existing service, and proves that the router can be reduced without redesigning the whole control plane.

## Ordered facade extractions

| Order | Facade and current source | State ownership after extraction | Safe slice | Effort | Characterization net | Rollback boundary |
|---:|---|---|---|---|---|---|
| 1 | `LiveInstanceSurfaceRuntime` plus `LiveInstanceSurfaceSources`; router status helpers, 56-callable `LiveInstanceSurfaceDependencies`, and the 17 surface-hub functions | One object constructed and stopped by FastAPI lifespan, stored on `app.state`; it owns hub registry, fleet provider, roster hub, and visible-runs cache | First introduce the runtime as a delegating wrapper with current source functions. Then replace groups of source callbacks with typed source methods. Do not change snapshot schema or cadence. | L (2–3 PRs) | status payload fixture, `test_surface_hub.py`, stream/status/startup/shutdown cases in `test_live_instances.py` | Keep router compatibility wrappers until every lifespan and mutation caller uses the runtime |
| 2 | `LiveInstanceActivityService`; helpers and routes from `_read_parquet_rows` through `get_active_dates` | Service is read-only. It owns window resolution, WAL/parquet reads, repair projection composition, evidence merge, and date indexing; no global state moves. | Extract pure time/row helpers first, then a service with `chart_snapshot`, `activity`, and `active_dates` methods. Router keeps query parsing and `HTTPException` mapping. | M (2 PRs) | chart/activity/active-date, evidence, repair, closed-trade, and DST cases in `test_live_instances.py`; existing activity services | Re-export pure helpers while endpoint behavior is compared against existing fixture payloads |
| 3 | `LiveInstanceDeploymentService` and `LiveInstanceLifecycleService`; deploy/preflight/cohort plus start/stop/end-day/roster/retire flows | Explicit injected collaborators for host daemon, account truth, desired-state repo, roll-call offer repo, and mutation attempts. No mutable module singleton. | Split deploy admission/preflight first. Extract start eligibility and start-intent persistence next. Extract cohort coordinator wiring last. Keep all broker writes and safety gates byte-for-byte equivalent at the call boundary. | XL (4–6 PRs) | deploy/start/cohort/start-gate tests in `test_live_instances.py`; deploy-preflight, account truth, desired-state, lifecycle, and host-daemon tests | Keep route-level compatibility adapters and compare response models / typed errors after each action family |
| 4 | `FleetRosterService`; catalog, roll-call, deletion, account summaries, and fleet projection helpers | Read composition and lifecycle projection move to the service; producer lifecycle stays in `LiveInstanceSurfaceRuntime`. | Start with pure catalog-row composition and bulk snapshot input. Move roll-call and deletion only after the runtime seam exists. | L (2–3 PRs) | catalog/roll-call/deletion/account-summary tests, fleet stream tests, and provider tests | Maintain existing endpoint response models and deletion cleanup ordering |
| 5 | `LiveInstanceReconciliationService`; desired state, flatten/pause, reconcile, command, and emergency-flatten endpoints | Explicit mutation-attempt and command-channel collaborators; durable files remain their current canonical owners. | Move read/projection helpers first. Move one mutation family at a time with its exact regression set. | L (3–4 PRs) | desired-state, outcome-unknown, reconcile, command, and emergency-flatten tests | Do not move a broker mutation and its retry/receipt semantics in the same step as an unrelated cleanup |

## `run.py` split line

The physical boundary is at `build_parser()` / `main()` near the end of `app/engine/live/run.py`: parser construction and `argparse.Namespace` dispatch are CLI concerns. Existing tests already call `cmd_*` directly, so the essential next step is not more subprocess avoidance; it is to remove `argparse.Namespace` from domain actions, especially the large `cmd_start` body.

1. Create typed command-input dataclasses at the CLI boundary.
2. Move parser construction and `main(argv)` to a CLI module that only parses into those types and dispatches.
3. Extract ledger/preflight actions, start/runtime action, emergency-recovery actions, and desired-state actions into separate modules.
4. Preserve `python -m app.engine.live.run` as a compatibility entry point and retain thin `cmd_*` wrappers until callers migrate.
5. Keep the current direct action tests, then add unit tests against typed action inputs rather than subprocesses.

`cmd_start` must be extracted by responsibility (ledger/config validation, recovery wiring, broker/client construction, engine execution, terminal artifact writing), not by arbitrary line count. It shares safety-sensitive recovery behavior with `live_engine.py`, which remains out of scope for #1125.

## Completion rules for later slices

- Each extraction PR updates `docs/architecture/live-control-plane-domain-map.md` with moved ownership and retains the baseline inventory.
- Each PR is constrained to one facade and its tests.
- Router response models, paths, typed error codes, timestamp wire representation, durable artifacts, and broker-write ordering are compatibility contracts.
- A service must not import FastAPI. The router remains responsible for validation at HTTP boundaries and HTTP exception translation.
