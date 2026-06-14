---
id: VCR-0017
severity: P2
status: open
area: dead-code
canonical_file: multiple
reference: PRD §8.6
first_seen: 2026-06-14
last_seen: 2026-06-14
lens: dead-bloated-code-docs
dedupe_with_F: none
confidence: high
---

## What

Rollup of dead/orphan/stale code and docs the workflow lens identified via static reference search. Each candidate carries a cleanup recommendation (deletion / archive / consolidate / needs-owner-review), confidence, affected files, and risk.

### A — `authors` / `books` scaffold subtree (Frontend) — **high confidence deletion**

The `authors/` + `books/` component dirs plus their services + GraphQL queries + frontend types are completely orphaned: no backend resolver, no route in `app.routes.ts`, no DbContext entry. The original use is a tutorial / scaffold artifact, not a feature.

- `Frontend/src/app/components/authors/`
- `Frontend/src/app/components/books/`
- `Frontend/src/app/services/authors.service.ts`, `books.service.ts`
- `Frontend/src/app/graphql/authors.ts`, `books.ts`
- Stale GraphQL `Author` / `Book` types in `Frontend/src/app/graphql/types.ts`

**Cleanup path**: deletion. **Confidence**: high. **Risk**: nil — no consumer. **PR**: single delete commit; remove the GraphQL types in the same diff.

### B — `run-comparison` component (Frontend) — **high confidence deletion**

`run-comparison/` is a dead older sibling of routed `runs-compare/`. The `runs-compare/` route at `/runs/compare` is the canonical comparison surface (consumes `/api/runs/compare` REST via `RunsCompareService`, fed by `Backend/Controllers/CompareController`). `run-comparison/` uses an Apollo `compareBacktestRuns` GraphQL query whose **backend resolver does not exist** (referenced only in an archived plan doc + the component's own spec). If mounted, the screen would 500 at runtime.

- `Frontend/src/app/components/run-comparison/`
- `Frontend/src/app/graphql/compare-backtest-runs.ts` (if present)

**Cleanup path**: deletion. **Confidence**: high. **Risk**: nil — no route, no caller.

### C — `validation_study.py` router (Python) — **medium confidence deletion**

`PythonDataService/app/routers/validation_study.py` (1054 lines) is **not registered** in `main.py` via `app.include_router(...)`. Its sole consumers (`trade_comparison.py`, `validation_service.py`) are exclusively imported by this orphan router. Three files (~1500 lines total) are unreachable from any FastAPI route.

**Cleanup path**: deletion (preferred) or needs-owner-review if any researcher imports `validation_service` directly via REPL. **Confidence**: medium pending owner check. **PR**: single delete commit.

### D — Frontend `PolygonService` — **high confidence deletion**

`Frontend/src/app/services/polygon.service.ts` is fully unreferenced except by its own spec. The direct browser → Polygon path was replaced by the PythonDataService proxy long ago.

**Cleanup path**: deletion + spec deletion. **Confidence**: high.

### E — `broker-user-manual.html` / `.pdf` — **archive candidate**

Per PRD §6.3, the new operator manual is the canonical replacement. The HTML (~44 KB, 12 sections, detailed checklists and dangers callouts) has unique content worth migrating before deletion; the PDF is a generated artifact.

**Cleanup path**:
1. Migrate any unique safety / pre-flight / troubleshooting content into the new operator manual (deferred to manual tick).
2. Archive both files under `docs/archive/` with a status banner.
3. Replace any stale architecture-doc references that still cite them.

**Confidence**: high (deletion-after-migration). **Risk**: information loss if migration is skipped.

### F — `lean-script-editor` (Frontend) — **NOT dead**

Active — referenced by `lean-engine` component. Listed here for symmetry with the dead-code sweep but cleared.

### G — Frontend stubs `strategy-finder-stub`, `volatility-stub`

Routed COMING-SOON placeholders, deliberately inert. Not dead. Should carry a tracker link in the placeholder template (see VCR-P3-rollup).

### H — Root-level scratch / binary clutter

`crudops.sql`, `order_store.sql`, two stale `.docx`, `dependency-audit.xlsx`, `247-Critical-feedback.md`, `analysis-hardening-gap-report.docx` are root-level files that don't belong in the source tree.

**Cleanup path**: archive under `docs/archive/scratch/` or delete after owner review. **Confidence**: medium pending owner check.

### I — Stale phase notes — **NOT dead**

`docs/architecture/phases/phase-1a..5g.md` (15 files) are actively referenced from `docs/architecture/lean-sidecar-lab.md:506-524` as a phase progress log. Not dead. (However see VCR-0016 — the `engine-authority-map.md` row pointing to these is stale.)

## Where (summary)

| Candidate | Files | Cleanup | Confidence | PR sequence hint |
|---|---|---|---|---|
| A — authors/books scaffold | `Frontend/src/app/components/authors/`, `books/`, services, GraphQL types | delete | high | 1 |
| B — run-comparison | `Frontend/src/app/components/run-comparison/`, GraphQL query | delete | high | 1 |
| C — validation_study.py | `PythonDataService/app/routers/validation_study.py` + 2 consumers | delete | medium | 2 (after owner) |
| D — Frontend PolygonService | `Frontend/src/app/services/polygon.service.ts` + spec | delete | high | 1 |
| E — broker-user-manual.html/.pdf | `docs/broker-user-manual.{html,pdf}` | archive after manual tick | high | 3 (after manual) |
| H — root scratch | 6 files at repo root | archive or delete | medium | 2 (after owner) |

## Why this severity

PRD §7 P2: moderate maintainability / dead-code / duplicate-code. No silent corruption, no trading impact. Repo bloat slows future contributors and hides real signals in lint/test runs.

## Suggested resolution (NOT auto-applied)

Single sweep PR for the high-confidence deletions (A, B, D). Owner-review PR for medium-confidence items (C, H). Defer E until the new operator manual ships in the manual-tick deliverable.

Add a CI check: `rg "from app.routers" PythonDataService/app/main.py` vs `ls PythonDataService/app/routers/*.py` to surface future unregistered routers automatically.

## Provenance of the finding

Lens: `dead-bloated-code-docs` (workflow `wf_def78013-ce4`). Each candidate's static-reference status confirmed by the lens's `rg` sweep; no runtime route probes per the agreed defaults.
