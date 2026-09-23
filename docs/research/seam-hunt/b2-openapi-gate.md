# B2: Does the OpenAPI contract gate fire?

Research ticket #2296, part of the seam-hunt map #2276. Baseline is `a14f1df1` (master, 2026-09-23). All `file:line` citations are at that SHA.

## Answer

**Yes, both halves of the gate fire.** I checked each one with a throwaway Pydantic change in a worktree and then reverted it (see §1.3).

- The Python half fails when a response model changes and the snapshot is not regenerated.
- The TypeScript half fails when the snapshot is regenerated but `broker.types.ts` is not.
- Both jobs run on every pull request, whatever paths it touches.

**What the gate does not do:**

1. It does not block a merge. Neither job is a required status check.
2. It does not cover the ingress the money path actually uses:
   - The fleet directory is not in the contract.
   - The clerk-scoped routes the browser calls are exported with an empty (`{}`) schema.
   - The clerk↔coordinator internal protocol is hidden from the schema.
   - SSE frames are not in the contract.
3. It does not check Angular templates.

The most serious hole is suspected gap G1: a rename of `effective_binding_generation` would silently switch off the binding-generation fence, and every PR check would stay green.

No P0 or P1 defect is **proven**, so no `bug` issues were filed. The proven facts are P2 or P3 (G3, G4, G7).

## 1. How the gate works

### 1.1 Python half: snapshot vs. a fresh export

- **Where CI runs it:** `.github/workflows/ci.yml:379-380`, the step "Verify committed OpenAPI contract". It runs `python scripts/export_openapi_contract.py --check`.
  - The step belongs to the `python-test-shard` matrix (`ci.yml:346`), so it runs in all 12 shards before any tests.
  - The `Python Tests` aggregator (`ci.yml:483-491`) fails if any shard fails.
- **What the exporter does:**
  - It imports `app.main` and serialises `app.openapi()` with `sort_keys=True, indent=2` (`PythonDataService/scripts/export_openapi_contract.py:42-65`).
  - It forces `ALPACA_FAULT_INJECTION_ENABLED=false` (`:52`) and `FLEET_ROLE=combined` (`:59`).
  - `_check` compares the committed file with the fresh export byte for byte and prints a unified diff (`:87-108`).
- **When it runs:** the workflow triggers are `pull_request` (no `paths:` filter) and `push` to master (`ci.yml:3-6`). The `python-test-shard` job has no `if:`. **The gate runs on every PR, not only on PRs that touch `PythonDataService/`.**

### 1.2 TypeScript half: `broker.types.ts` regenerated from the snapshot

- **Where CI runs it:** `ci.yml:143-158`, the `frontend-typecheck` job. It runs `npm run codegen:check` and then `npx tsc --noEmit`. The job has no `if:` and no path filter.
- **What `codegen:check` does** (`Frontend/package.json:18`): it runs `npm run codegen:openapi && git diff --exit-code -- src/app/api/broker.types.ts`.
- **What the generator runs** (`Frontend/tools/openapi-codegen/package.json:6`): `openapi-typescript <snapshot> --default-non-nullable=false -o src/app/api/broker.types.ts`. The version comes from the lockfile, which `npm ci` enforces.
- **`alpaca.types.ts` is not generated.** It holds hand-written aliases into `components['schemas'][...]` of `broker.types.ts` (`Frontend/src/app/api/alpaca.types.ts:1-9`).
  - It has no regen check of its own and does not need one.
  - If a schema it aliases is removed, `tsc --noEmit` fails on the indexed access. That is the second step of the same job.

### 1.3 Proof that the gate fires

These mutations were made locally in this worktree and never committed. The model under test was `BrokerOrder` in `PythonDataService/app/broker/contract/models.py:206`, which is on the money path (recent-orders table).

| # | Mutation | Command | Result |
|---|---|---|---|
| 0 | none (baseline) | `export_openapi_contract.py --check` | exit 0 |
| 1 | `filled_quantity: float` → `int` | `export_openapi_contract.py --check` | **exit 1**; the diff shows `"type": "number"` → `"integer"` |
| 1b | same, then regenerate the snapshot | `npm run codegen:check` | exit 0. This is correct: TS maps both types to `number`, so the TS gate has nothing to catch. |
| 2 | `filled_quantity: float` → `float \| None` | `export_openapi_contract.py --check` | **exit 1** |
| 2b | same, snapshot regenerated, `broker.types.ts` left stale | `npm run codegen:check` | **exit 1**; the diff shows `filled_quantity: number;` → `number \| null;` |

Afterwards all three files were restored with `git checkout --`. (`NUMBA_CACHE_DIR` pointed into `$TMPDIR` because the sandbox blocks numba's cache inside the main venv. This does not affect CI.)

**Conclusion: the gate fires in both halves.** It is not a check that cannot fire.

## 2. What the contract does not cover

I got this by enumerating every route of the imported app under `FLEET_ROLE=combined`, `fleet_coordinator` and `clerk_agent`, and scanning the committed snapshot.

- The committed snapshot has **376 operations**. **146 of them have an opaque 2xx body**: `{}`, a title-only object, or no content.
- The coordinator and clerk-agent roles mount **no path the combined export lacks**. The combined export is a superset of both.

Ranked by money-path relevance:

| Rank | Surface | Why the gate cannot see it | Cite |
|---|---|---|---|
| 1 | **Fleet directory** `GET /api/broker-clerks`, `/api/brokers/{broker}/clerks`, `/{clerk_id}`, `/aggregate/directory`, `/aggregate/attention`, `/audit/routing-receipts` | The handlers return `JSONResponse` built from dicts (`ClerkDescriptor.public_fields()`), so the schema is `{}`. The frontend type is hand-written. This payload carries `effective_binding_generation`, the input to the command fence (C1). | `app/routers/broker_clerks.py:97-103`, `app/broker/fleet/records.py:422-438`, `Frontend/src/app/fleet/fleet-directory.types.ts:22-39`, `fleet-directory.service.ts:116` |
| 2 | **86 fleet-routed clerk-scoped operations** `/api/brokers/{broker}/clerks/{clerk_id}/...`, including runs/start, runs/stop, cohort-flatten, manual-orders cancel, orders, account, live-verdict and custody | They are generated by `_make_operation_handler`, whose return type is `-> Response`, so every 2xx schema is `{}`. The body is the lane route's body passed through. The frontend binds each URL to a `components['schemas'][Name]` by hand, and no code types a request by `paths[...]` (grep finds no `paths[` outside `broker.types.ts`). | `app/routers/broker_clerks.py:356-462` |
| 3 | **Refusal and error bodies** on those routes (409/503/422 from `_refuse`, `_envelope_invalid`) | These are undeclared `JSONResponse`s. Only 4 of 376 operations declare a 409. Reason codes are separately pinned by the fleet refusal vocabulary snapshot job (`ci.yml:311-327`), but the envelope shape is not pinned. | `broker_clerks.py:81-91` |
| 4 | **Internal clerk↔coordinator protocol**: 7 routes, including sessions, sessions/observe, assignments reserve/confirm, lanes/confirm-quiet, history/batch and volume-expectation | The router sets `include_in_schema=False`. The handlers use `response_model=None` and return dicts or `Response`. Coordinator and clerks run in separately restarted processes, so the two sides can run different code versions. | `app/routers/internal_fleet.py:99, 274-440` |
| 5 | **SSE streams**: bot `live-stream`, `gallery/stream`, IBKR `evidence/stream`, option-chain/option-surface streams | These are `StreamingResponse`s with schema `{}`. A2 found the panel frame type is generated from the REST bootstrap model. A1 found `broker-models.ts` is hand-mirrored and currently matches. | `broker_v2_panel.py:478`, `broker_v2_gallery.py:259`, `broker.py:108,362,433` |
| 6 | IBKR option expirations/contracts | `response_model=dict` | `broker.py:277,321` |
| 7 | Research/job surfaces: `jobs-internal/*`, `edge/*`, `lean-sidecar/*`, `dataset/*`, `data-quality/*`, `chart/*`, `research/data-divergence/*` (the page route is `include_in_schema=False`) | `dict`, `JSONResponse`, `PlainTextResponse` or no model | `jobs.py`, `edge.py`, `lean_sidecar.py`, `dataset.py`, `research_divergence.py:50` |

**Guarded, as a check.** Every non-stream fleet catalog operation (85 total) resolves to a lane route with a real `response_model`. So the lane's body shape is inside the contract, and only the URL→schema binding in the frontend is hand-made. No money-path route uses `response_model_exclude_none/unset`, `model_serializer`, `field_serializer` or a custom JSON schema that could make the exported schema differ from the runtime body. (I grepped `app/`, and the only hits are two harmless `json_schema_extra` uses.)

## 3. Named suspected gaps

**G1: a directory field rename disables the binding-generation fence without failing any check (P1).**
- **Hypothesis:** Suppose `ClerkDescriptor.public_fields()` renames `effective_binding_generation` (`records.py:432`). Then every frontend call site reads `lane.effective_binding_generation ?? null` and gets `null`. For example:
  - `alpaca-desk-account-data.service.ts:77`
  - `bot-panel-shell.component.ts:216`
  - `operator-lens.component.ts:98`
  - `bots-list-page.component.ts:116`
- **Consequence:** `commandContextOf` omits `expected_effective_binding_generation` (`resource-target.ts:139-140`), and `routing.py:408-413` skips the fence whenever the field is `None`. All checks stay green:
  - the OpenAPI check (the directory is `{}`)
  - codegen:check
  - tsc (the TS type is hand-written)
  - the Python fleet tests, if they are updated alongside the rename

  Commands then dispatch without the fence, which is the C1 hazard reached by a refactor.
- **Prototype:** in a worktree, rename the key in `public_fields()` and update the Python tests. Then run `export_openapi_contract.py --check`, `npm run codegen:check`, `npx tsc --noEmit`, and the specs for the files above. Show that all pass. Then show that a fleet command from the desk carries no `expected_effective_binding_generation`.
- **Fix direction (not for this ticket):** give the directory a Pydantic `response_model` so it enters the snapshot, and alias `LaneDescriptor` to the generated schema.

**G2: frontend URL→schema binding for fleet-routed operations is hand-made (P2).**
- **Hypothesis:** A lane route switches `response_model` to a different class while the old class survives elsewhere in the schema, or a frontend service casts a clerk-scoped response to the wrong `components['schemas'][X]`. The snapshot and TS regen both pass, because the clerk-scoped operation is `{}`. The UI then reads fields that don't exist, and they show as `undefined`, typically rendered as "—" or as false.
- **Prototype:** point a lane route's `response_model` at a sibling class, regenerate, and run the gate plus tsc to show they are green. As a check that could replace this, derive a TS map `ClerkRouteResponse[operationId] = lane schema` from the catalog and fail when a frontend fetch's generic doesn't match.

**G3: the gate fires but cannot block a merge (P2, proven).**
- **Fact:** Master has no classic branch protection (`GET branches/master/protection` returns 404). The only required-status ruleset (id 19185467, "Require Backend Tests on master") requires `Backend Tests` only, which is the .NET suite the map marks as condemned. `Python Test Shard *`, `Python Tests` and `Frontend Type Check` are all advisory.
- **Current practice:** Of the last 25 merged PRs (back to 2026-09-21), none merged with a red Python shard or Frontend Type Check. So the gate is enforced by convention only.
- **Prototype:** none needed. The owner decides whether to add `Python Tests` and `Frontend Type Check` to the required checks.

**G4: role-divergence coverage is daily-only (P2, proven).**
- **Fact:** `tests/contracts/test_fleet_role_openapi_agreement.py` is the only check that a non-combined role (the production coordinator) describes its routes the way the committed contract does. It is `pytestmark = pytest.mark.slow` (`:110`), and `run_fast_tests` excludes slow tests. So a PR that makes a coordinator-only handler differ (the #2108 shape) merges green and turns red the next morning.
- **Prototype:** re-annotate `fleet_compatibility_reads` with `-> Any`, then run the fast gate (green) and the slow test (red).

**G5: skew between the clerk↔coordinator internal protocol versions is not checked (P2).**
- **Hypothesis:** A coordinator restarted on new code while a clerk still runs old code (the normal state after a merge, per the memory note "clerks need restart") exchanges `/internal/fleet/*` dicts that no snapshot pins. A renamed or removed key is read as absent, not refused.
- **Adjacent defect:** C4's proven #2320 is the same family: a 404 misread as "coordinator down".
- **Prototype:** run the internal router's request/response dicts from `HEAD~N` against `HEAD` for each route, and list the keys each side reads that the other no longer writes.

**G6: Angular templates are outside the PR type gate (P2).**
- **Fact:** `npx tsc --noEmit` (`ci.yml:158`) does not read `.html` templates. `strictTemplates` only runs under the Angular compiler.
- **Where templates do get checked:**
  - On PRs, only through `ng test`, and only for components a spec reaches. 125 of 319 components have no spec of their own; 36 of those are under broker, brokers or fleet.
  - `ng build` runs only in the daily `frontend-e2e.yml:59`.
- **Hypothesis:** A regen that renames a field read only in the template of a component no spec reaches passes PR CI and breaks that view.
- **Prototype:** pick a spec-unreachable broker component whose template reads a generated field, rename the field in the snapshot and regen, then run `codegen:check`, `tsc` and the frontend shards (green) and `ng build` (red).

**G7: `codegen:check` ignores untracked files (P3, proven by reading).**
- **Fact:** `git diff --exit-code -- src/app/api/broker.types.ts` does not see untracked files. A PR that deletes `broker.types.ts` gets it regenerated in CI as an untracked file, and both the diff and tsc pass.
- **Contrast:** the vocabulary snapshot jobs close exactly this hole with `git status --porcelain` (`ci.yml:302-310`).
- **Fix:** add the same porcelain check.

**Charted elsewhere, no new ticket:** SSE frame shapes (A1/A2, sound today); hand-written REST types other than the directory (B1).

## 4. Defects proven

- **P0/P1: none.** The gate fires, so no `bug` issue was filed.
- **P2, proven:** G3 (the gate is advisory, not a required check) and G4 (role agreement is checked daily, not per PR).
- **P3, proven:** G7 (untracked-file hole in `codegen:check`).
- **Suspected, needs a prototype:** G1 (P1), G2 (P2), G5 (P2), G6 (P2).
