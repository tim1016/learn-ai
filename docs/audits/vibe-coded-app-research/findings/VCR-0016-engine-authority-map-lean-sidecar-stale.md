---
id: VCR-0016
severity: P2
status: remediated
area: documentation
canonical_file: docs/architecture/engine-authority-map.md:19
reference: docs/architecture/lean-sidecar-lab.md
first_seen: 2026-06-14
last_seen: 2026-06-14
remediated_in: "Phase 10 — engine-authority-map.md row 19 updated to 'shipped through Phase 5g.3 (2026-06-13)'; lean-sidecar-lab.md status header refreshed to Phase 5g.3"
lens: architectural-drift-registries
dedupe_with_F: none
confidence: high
---

## What

`engine-authority-map.md` row 19 (the "External LEAN compatibility / reference runs" row) labels the status of LEAN Lab sidecar as *"planned external-reference-runner — Phase 0 ADR only; code begins in Phase 1"*. This was accurate when the row was authored on 2026-05-17 (commit `ed09652d` Phase 0 ADR). Since then the entire Phase 1 (launcher service + image digest pin + workspace contract + LEAN data-folder fidelity), Phase 2 (Python API), Phase 3 (container execution boundary), Phase 4a-f (Frontend LEAN Lab + run-history sidebar + form rehydration + lean_error_categories), and Phase 5a-g (self-reconciler + reconciliation-grade template + minute-quote staging + window/bars manifest + determinism gate + cross-engine reconciler) have all shipped, as documented by the 15 phase progress notes in `docs/architecture/phases/` and confirmed by the present code at `PythonDataService/app/lean_sidecar/`, `PythonDataService/app/routers/lean_sidecar.py`, and `Frontend/src/app/components/lean-script-editor/`.

The engine-authority-map row is the single doc-of-record for engine ownership and status; its status column is now off by ~9 PRs. The doc's "Last reviewed: 2026-06-13" header is also misleading: it was touched by the live-sizing PR that updated row 33, not row 19.

`docs/architecture/lean-sidecar-lab.md:3` is similarly stale ("Phase 5b…") — also caught here.

## Where

- `docs/architecture/engine-authority-map.md:19` — status column text is stale.
- `PythonDataService/app/lean_sidecar/` — launcher + cross_reconciler + cross_runner + data_policy + diagnostics + lean_lint shipped.
- `PythonDataService/app/routers/lean_sidecar.py` — router exists and is wired into FastAPI app.
- `docs/architecture/phases/phase-5g-3.md` — most recent progress note.
- `docs/architecture/lean-sidecar-lab.md:3` — co-stale status header ("Phase 5b").

## Why this severity

PRD §7 P2: stale governance doc. A contributor reading row 19 to decide "do we already have a LEAN sidecar?" would conclude it is unstarted and either duplicate work or surface the discrepancy. No silent corruption; math is unaffected.

## Suggested resolution

Update row 19 status column text to *"shipped through Phase 5g.3 (cross-engine reconciler + UI). Current progress tracked in `docs/architecture/lean-sidecar-lab.md` and `docs/architecture/phases/`."* Update `lean-sidecar-lab.md:3` status header. Optionally roll up the phase-N progress notes into a single "shipped" bullet list.

## Provenance of the finding

Lens: `architectural-drift-registries` (workflow `wf_def78013-ce4`, structured-finding `engine-authority-map-lean-sidecar-status-stale`, verified 2/2 by adversarial pass).
