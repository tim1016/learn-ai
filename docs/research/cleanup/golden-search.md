# Clean-up — Golden Search: engine, routes and page (#3156)

Findings for ticket [#3156](https://github.com/tim1016/learn-ai/issues/3156) under map [#3146](https://github.com/tim1016/learn-ai/issues/3146) (tracker #3145). **Read at `cb667942`** (origin/master, 2026-10-14 ~14:00 CDT; the ticket was charted at `862b50d3`). This file lives on the throwaway branch `research/cleanup-golden-search` and is never merged. The delete-only cut is [#3215](https://github.com/tim1016/learn-ai/pull/3215) (branch `chore/3156-golden-search-dead-machinery`).

## Result

- **No P0, no P1 confirmed; nothing filed.** Ten readers looked for a qualified version approved on evidence that does not prove it, a stale "ready", a fail-open gate and a swallowed exception. The closest are P2s with repros I re-ran: a Golden Validation History badge and catalog button that read "Accepted" over a review that no longer applies (F4 C4; every gate reads `review_is_current`, the string is display only), the decision summary's "Neighbor sensitivity: meets" when no neighbor was tested (P3 C1), a failed run listed as "0 trades" and sorted first (P4 C1), and `POST /preflight` answering 500 for a huge month count (P6 C1). One more is a hypothesis I read live and could not confirm: the shared worker pool pauses on 90 % of the cgroup's `memory.current`, which counts page cache (P2 C7). In the running `learn-ai-research-1` container the idle reading is 250 MB of 16 GiB (file cache 6 MB), so it does not fire at rest; behaviour under a real study is not measured.
- **One cut PR, #3215:** 5 Python pieces and 6 Frontend ones, 17 files, +5 −79, no contract change, no touched Python file in the research code-identity closure (see "Held out of the cut").
- **The owner's attention:** nine rulings below, led by the five qualification routes with no caller (list, defaults, detail, revoke, re-proof): revoke and re-proof are the only way to withdraw or refresh a qualified version and nothing in the repo calls them.
- **Feature coverage:** about 145 feature rows over ten slices. Partial: `close` command (API only, no UI), qualification revoke/re-proof (API only), canonical-point idempotence for `-0.0`, the plan incumbent's validation, default Zoom `passes` against the PRD wording. Unverified locally for want of Postgres on PR CI: every stage runner, the one-transaction approval, the study-level read model and most route tests (they run in the daily tier; I ran them on a throwaway Postgres, see Evidence).
- **For handoff (#3159):** 10 slices (H1–H10), a lens-6 list of about 40 tests with survivors named, nine owner rulings, the held cuts, one cross-stack wire-contract slice that needs serial regeneration.

## Method

Ten read-only readers, split by sub-package and never by lens, each briefed from one file (lenses, Sacred list, the kill lists *with their comments*, the pointers, the method rules from earlier tickets, a fixed report format) plus the five PRD bodies and their comments (#2696, #2811, #2821, #2930, #2932, #2933, #2900) saved as files:

| id | slice |
|---|---|
| P1 | `planning`, `declarations`, `protocol`, `grid_procedure`, `zoom`, `budget`, `exam_rules`, `exposure_rules`, `guidance`, `selection`, `procedure_history`, `models`, `actions`, `activity` and their tests |
| P2 | `evaluator`, `stages`, `worker`, `shared_pool`, `pool`, `final_test`, `final_measures`, `trade_net` and their tests |
| P3 | `service`, `repository`, `views`, `evidence`, `proof`, `approval`, `decision_summary` and their tests and helpers |
| P4 | `qualifications`, `qualification_service`, `run_curves`, `trade_charts`, `search_charts`, `plan_charts`, `fold_charts`, `concentration`, admission and deploy tests |
| P5 | the chance family: `chance`, `chance_compare`, `chance_final`, `chance_rehearsal`, `chance_drawdown`, `chance_approval`, `compare_measures` and tests |
| P6 | the four routers, three wire schemas, `golden_validation` package, route boundary (27 operations), config keys, job phases |
| F1 | the charts: `charts/`, the 12 chart builders, curve set and store, `testing/` fixtures, print stylesheet |
| F2 | the study flow: page, study, plan form and draft, the steps, the stepper and process rail, routes |
| F3 | service, wire types, evidence/approval/chance panels, history, runs, lockbox, exam result |
| F4 | Golden Validation workbench and its service, the manual, the report, the chart guide |

Their reports are appended below, unchanged. I re-verified every cut candidate with a whole-repo `git grep -w` (generated Frontend types included) and read the call sites; I re-ran the repros of P3 C1, F4 C4, P4 C1 and P6 C1 and read the cgroup of the running research container for P2 C7. Subagents cannot write files, so each report came back inline and I saved it as a part file as it landed.

## Evidence

| run | result |
|---|---|
| Readers' slice runs at `cb667942` | P1 262 passed, 4 skipped; P2 100 passed, 13 skipped; P3 100 passed, 79 skipped; P4 160 passed, 19 skipped; P5 150 passed, 16 skipped; P6 32 passed, 46 skipped; every skip is `POSTGRES_URL not configured` |
| Frontend readers' runs | F1 11 spec files, 113 tests; F2 12 files, 225; F3 9 files, 77 (+ 4 files, 103); F4 5 files, 57; `eslint --max-warnings 0` clean on every unit |
| After the cut, no Postgres (lead) | `tests/research/golden_search`, `tests/research/golden_validation`, the three golden router tests, `test_golden_qualification_admission.py`, `test_golden_search_deploy.py`, `tests/research/test_research_runtime.py`: **831 passed, 180 skipped** (Postgres-gated) |
| After the cut, throwaway Postgres 16 on :5441 (lead) | the same set on the final tree: **1,011 passed, 1 failed** in 167 s. The one failure, `test_worker_pool.py::test_admission_verdicts_batch_with_measured_latency_across_widths`, needs the EF-owned `DataLakeArtifacts` catalog table that an empty throwaway database lacks (`relation "DataLakeArtifacts" does not exist`, the recipe's known noise). An earlier run, started while I was reverting two identity-closure edits, failed 6: the worker children refused a sealed identity that changed under them, which is the stranding hazard in miniature |
| After the cut, Frontend (lead) | 10 spec files (compare-step, search-step, chance-final-panel, plan-form, study, compare, decision-step, service, page, chance-render): **224 passed**; `tsc --noEmit -p tsconfig.json` clean; `eslint --max-warnings 0` on the 10 touched `.ts`/`.html` files clean |
| Lint | `ruff check PythonDataService/app/research/golden_search PythonDataService/tests/research/golden_search`: clean |
| Live read (P2 C7) | `learn-ai-research-1`: `memory.current` 250,466,304 of `memory.max` 17,179,869,184; `anon` 235 MB, `file` 6 MB; `pids.max` 512; `RESEARCH_EXECUTION_MODE=research`, `RESEARCH_WORKER_COUNT=4` |
| Lens ruff set (`S110,S112,BLE001,E722,T201,DTZ003-7,F401,F841,F811`) | 26 × BLE001 (`chance_*` ×13, `stages` ×7, `service` ×2, `evaluator`, `qualifications`, `shared_pool`, `worker`); the readers found each one logging or recording a visible failed outcome (P5 D, P2 C5, P3 D, P4 D); no other hit |

## The cut — PR #3215

| file | piece | evidence |
|---|---|---|
| `golden_search/repository.py` | `insert_trial_fenced` | `git grep -nw`: the definition only; the plural `insert_trials_fenced` is the live one (P3 B1) |
| `golden_search/models.py` | `TERMINAL_STATES` | `git grep -nw`: the definition only; first commit `59f96f96`, never imported (P1 B1) |
| `golden_search/qualification_service.py` | `reprove_qualification(blob_store=)` and the `BlobStore` import | the one caller (`routers/golden_qualifications.py:101`) passes three keywords; the endpoint tests point re-proof at their blobs with `settings.GOLDEN_SEARCH_PROOF_ROOT` (P4 B1) |
| `golden_search/shared_pool.py` | `SharedWorkerPool.__init__(spawn=)` | five production and test constructors, none passes it; tests patch the module's `ResearchWorker` (P2 B3) |
| `golden_search/chance_drawdown.py`, `chance_final.py`, `chance_rehearsal.py` | module `logger` and `import logging` | `grep -n logger`: the assignment only (P5 B2) |
| `components/golden-search/golden-search.types.ts` | 31 unused exported type aliases (36 lines) | `git grep -nw` per name over `Frontend/src` less the generated `api/` folder: 0 outside references; `KnobSummary` is read only by `StopReason`, also cut (F3 B1) |
| `golden-search-method-choice.component.{ts,html}` | `readonly` input and `[disabled]` binding | the one host binds `[method]` and `(methodChange)` only; the `[readonly]` near it is the knob table's (F2 B1) |
| `golden-search-metrics.component.{ts,html,scss}` | `compact` input, class binding, `.metrics--compact` | the one host binds `[metrics]` only (F3 B2) |
| `golden-search-chance-final-panel.component.ts` | `rangeNotRecorded` computed | declared and referenced nowhere (F3 B3) |
| `golden-search-search-step.component.scss`, `golden-search-compare-step.component.scss` | `.caption`, `.caption strong` | `caption` there is an attribute, no element has the class, no `<strong>` inside a `.caption` (F2 B2, B3) |
| `golden-search-compare-step.component.ts` | `LINKED_CHARTS` rows `side-by-side`, `concentration-curve` | the server emits chart links to `neighbor-tornado`, `cost-stress`, `without-best` only (`decision_summary.py:104,124,148`), computed on read, never stored (F1 B1) |

## Driver's reconciliation (what changed between the readers' reports and the cut)

- **Held out of the cut: the research code-identity closure.** `app/research/sweep/identity.py:68-102` hashes fourteen Golden Search modules (and `app/engine`, grid search and walk-forward) into `source_digest`; the comment there says a wording fix must never strand a resumable study. A byte edit to `stages.py`, `evidence.py` or `grid_procedure.py` would make every in-flight study fail its code-identity check on the next restart, and move the digest the other research procedures use. So these stay findings, to bundle with a change that already moves identity: the **`on_log` chain** (`StageContext.on_log`, `execute_stage(on_log=)`, `service.run_stage(on_log=)`, `emit.log` at `routers/golden_search.py:410,448`, four test call sites; `ctx.on_log(` has no hit, P2 B1), **`evidence.same_as`** with `test_same_as_collapses_identical_points` (P3 B2) and **`grid_procedure.pair_grid(size=)`** with its `size < 2` guard (P1 B2). I had applied the first two and reverted them; no file in the cut is in the closure (checked by script against the list).
- **`chance.reading_basis` and its test left (P5 B1).** No production caller, but the one test pins the stored-reading constants and the `CONVENTION` literal together; the numeric constants are pinned elsewhere, the literal is not. A test-only helper whose test proves behaviour that stays (#3150 precedent).
- **Wire-contract cuts left for the serial slice** (each changes `contracts/openapi/python-data-service.openapi.json` and `api/broker.types.ts`; #3153's precedent): three qualification routes with no caller (`GET /golden-qualifications`, `/defaults`, `/{id}`; P6 B1–B3), `GoldenSearchRunDetail.daily_equity` with `GoldenSearchEquityPoint` (P3/P6 B4), `run_to_compare`, `updated_at_ms`, four capability-knob fields, `Neighborhood.one_sided` (P3 B3–B6), the unread half of `results.validation` (F3 B6).
- **Other left rows:** `WorkerDispatchContext.job_id`/`.generation` (P2 B2: never read in `app/`, but written by the out-of-scope grid-search, recency and benchmark dispatchers and by about 30 test constructors); `ResearchWorker._pump` (P2 B4: an assigned Thread that is started, not a delete); `GoldenValidationService.list(filters)` (F4 B1: only the service spec passes them; a test-only parameter, #3154 precedent); `StudyListFilters.limit` (F3 B4: pairs with the History cap, H1); the legacy single-candidate exam arm and the v1 fit-to-fit reshape (P2 B5, P5 B3: need a DB probe and the owner's word on deleting old studies, #2933).
- **Lens-6 deletions are not in the cut** (map: lens 2 only), including the duplicate SVG-walk tests in `chance-render.spec.ts` (F1 C4), the `reading_basis` test and every test named in H8.
- **Severity calls.** F4 C4 is P2: `GoldenValidationDossier.state` ignores `review_is_current`, but `assess_deployment_scope` (`service.py:693-707`) and `panel_deploy.py:227` both require `review_is_current is True`, and `accepted_*` is read by no other code. P6 C2 (an incumbent label taken from the request) is P2: the page always echoes the server's own default, it takes a hand-built request, and the approval reads the stored default. P3 C1 is P2: the approval gate is separate and reads the exam rules, not this row. P2 C7 stays unconfirmed under load.
- **Independent review:** one independent reviewer (a fresh subagent running the repo's thermo skill on the working-tree diff, before the first push): **PASS**, no major findings. It re-grepped every removed item over the whole repo (generated types, Backend, scripts, docs, contracts), checked every touched file against the identity closure, found no removed or weakened Sacred item and no deleted test, and confirmed the two `LINKED_CHARTS` rows against `decision_summary.py:104,124,148` and `git log -S`. Three minors: the one-assignment `_spawn` alias the `spawn=` cut left (inlined), a double blank line in `golden-search.types.ts` (fixed), an untracked `.coverage` (not staged). It ran ruff, eslint and `tsc` but not pytest or `ng test`; those runs are mine, above

## Owner rulings left open

1. **Five qualification routes with no caller** (`GET /api/research/golden-qualifications`, `/defaults`, `/{id}`, `POST /{id}/revoke`, `/{id}/reprove`; P4 C3, P6 B1–B5). Only `deploy-offer` is called. Revoke and re-proof are the only way to withdraw or refresh a version (after any strategy edit every version goes stale) and there is no runbook line. Document and keep, build the surface, or cut the three reads and keep the two writes.
2. **`close` has no UI** (F2 C4, P6 C11): the server command, the `closed` state and their tests stay reachable only by hand-built request; PRD #2696 and #2930 name "closes the study". Build the control, or drop the command (persisted closed studies keep the display branches).
3. **Legacy-shape reads** (P2 B5, P5 B3, P5 D1): the pre-#2930 single-candidate exam arm, the v1 fit-to-fit reshape (written for one day, 2026-10-10), and the "not measured" paths for studies locked before #2932. Cut after a DB probe and the owner's "delete the old studies" (#2933).
4. **`objective_percentiles_1x`** (P5 F14) is stored, served and typed, read by no production code: use it as a runtime integrity check of the carry-over reproduction, or version the schema to drop it.
5. **An outage-class error becomes evidence** (P2 C5): a child's `CatalogUnavailableError` or 60 s `LakeAdmissionError` is stored as that candidate's failed evaluation and cached for the study's life. Designed ("a failed run is evidence"); should an outage be a retryable loss instead?
6. **The worker child inherits the whole environment** (P2 C9): `POSTGRES_URL`, `REDIS_URL`, the control secret and broker keys reach a process documented as holding "no database, Redis, provider or broker handle". Allow-list it, or accept.
7. **`axe` assertions in Golden Search specs** (F1–F4 C-rows, about a dozen here, 65 repo-wide): the 2026-10-12 ruling puts accessibility out of scope; `.claude/rules/angular.md` still says "must pass AXE". Delete or keep.
8. **Retry after `DEFAULT_CHANGED`** (P3 C7): the Frontend always sends the plan's frozen incumbent as `expected_default_qualification_id`, so once any other approval moves the default, "Retry qualification" fails the same way every time and only Revise helps. By design per the PRD ("require a new review"), or fix the copy.
9. **Which modules belong in the identity closure** (P1 C1): `activity.py` (the trade-floor policy handed to Zoom and Grid as the selection policy), `chance*.py`, `concentration`, `trade_net`, `compare_measures`, `final_measures` compute stored numbers and sit outside it. Add them, accepting that each edit then strands resumable studies.

## Handoff list for #3159

### Fix slices (each PR-sized; severity is P2 or robustness unless noted)

| # | what | where | lens | evidence |
|---|---|---|---|---|
| H1 | **Golden Validation honesty**: the History badge, catalog button and case chip read "Accepted" over a stale review (C4); a failed designate/review shows an invented cause and drops the server's `detail.message` (C2); the "open saved configuration to rerun" block shows for a `current` case (C1); "Trade deviations 0" beside "No paired verdict" (C5); a failed catalog read reads as "no goldens" and offers "Mark golden" (C6); the list cap of 100 is silent here and in History (F4 C3, F3 C2, `StudyListFilters.limit`) | `golden-validation.service.ts`, `golden-validation-case.component.html`, `engine-lab-run-history.component.ts`, `golden_validation/service.py:101-108`, `routers/golden_search.py:167` | 1/3/5 | F4 C1–C6; F3 C2; repro `scratchpad/F4/stale_state_repro.py` (re-run: `accepted_engine_agreement`, `review_is_current False`) |
| H2 | **Chance and decision wording**: the development chance panel composes its own money line and prints 0.0 / 0.0 / 0.0 % for `no_defined_paths` (F3 C1); a final-side refusal says "The development run failed" (P5 F1); an adverse share is called "inside the model's noise" (F2); rank 0 with 0 finite paths prints "0 of 0" (F3); the rehearsal ignores `undefined` (F4); `fit_to_fit_reading` writes `0.95` and `v1` as literals and omits `chunk_replications` (F6); the ceiling prints `.0%` (F7); the drawdown chart hard-codes `of 10,000` (F1 C8); the decision summary reads "meets" with no neighbor tested (P3 C1); the report and manual hide the Sharpe carry-over rank on a claim of "different return conventions" that the code and the PRD prototype contradict (P5 F8, F4 C12) | `chance_compare.py`, `chance_final.py`, `chance_rehearsal.py`, `chance_approval.py`, `decision_summary.py:103-120`, `golden-search-chance-charts.ts:221`, `golden-search-drawdown-charts.ts:43`, `report/*`, `manual/*` | 1/3/4 | P5 F1–F8; F3 C1; P3 C1 (repro re-run: `status: meets`) |
| H3 | **Plan intake boundary**: `/preflight` and lock answer 500 for `training_months=10**12` (P6 C1, repro re-run: `OverflowError` at `walk_forward_study/folds.py:61`); a non-numeric incumbent value 500s and an unknown key or off-domain value locks (P1 C2); an incumbent label is never re-resolved on lock (P6 C2); `canonical_point` moves a raw value up to half a quantum outside the domain and can emit `-0.0` with a second hash (P1 C3); `COMMANDS`/`CommandName` and `_MEASURE_NAMES`/`RANKING_MEASURES` are hand-kept pairs (P1 C5); ids and run ids have no upper bound (P6 C9); `*_ms` fields in `golden_validation` lack the ceiling (P6 C4); every read answers 500 with the exception text when Postgres is down (P6 C3) | `schemas/golden_search.py:133-134`, `protocol.py:621-634`, `declarations.py:386-412`, `schemas/golden_validation.py:67,79` | 4/5 | P6 C1–C4, C9; P1 C2, C3, C5 |
| H4 | **Worker pool and stage runner robustness**: `drain()` self-deadlocks when closing (C1; repro prints `returned within 5 s: False`); a cancel holds the pool-wide lock for 1 s per lane, 3.8 s measured with 4 lanes (C2); the child cannot observe a cancel, so `cancel()` is a 1 s delay before the kill (C3); failure text differs by execution mode (C4); `_send` swallows `ValueError` so an unencodable task waits 30 min (C6); the memory guard counts page cache (C7, confirm under a real study); one `except Exception` lacks the `EvidenceIntegrityError` clause (C8); `__main__` guard mid-module (C10); `release_dispatch_binding`/`unbind_dispatch` have no test (P3 C3); `build_proof`'s own `TRACE_MISMATCH` never fires in a test (P3 C4); the real stage and the real approval are never run together (P3 C2) | `shared_pool.py:686-707,664-684,766-823`, `worker.py:795-800,682-691,420-434,473`, `stages.py:816-842` | 3/5/6 | P2 C1–C12 (repros `scratchpad/P2/repro_drain.py`, `repro_cancel_lock.py`, `measure_cancel.py`); P3 C2–C4 |
| H5 | **Qualified versions on the plan and Deploy**: the All-runs page lists a failed run as 0 trades and sorts it first (P4 C1, repro re-run); the plan's incumbent label reads "ready" from the raw on-disk digest where approval and Deploy use the drift-checked one (P4 C2); Deploy shows the registry preset for a stale or revoked default with only the generic explanation (P4 C10); `QualifiedDeployConfiguration.golden_qualification_id` has no reader (P4 C8) | `run_curves.py:137-166`, `golden_search/service.py:194-240`, `paper_deploy_service.py:466-480` | 1/3 | P4 C1, C2, C8, C10 |
| H6 | **Study page flow**: Hide lands on "New study" instead of History because the two routes rebuild the page (F2 C1, by router semantics, not executed); a revise whose "Run research" half is refused leaves a locked study and a second Lock mints another linked study (F2 C2, F3 C4); a closed-before-run study shows five done steps (F2 C3); percent to fraction by float division freezes `0.011000000000000001` (F2 C5); `var(--text)` is defined nowhere, so the drawdown legend's Median tick is invisible and four panels inherit colour (F1 C1, F3 C5); a ticked run read as `not_recorded` is silently dropped from "N drawn" (F3 C12) | `golden-search-page.component.ts:62-65`, `plan-form.component.ts:221-267`, `steps.ts:37,50-57`, `plan-draft.ts:288`, four panel `.scss` | 1/3/7 | F2 C1–C3, C5; F1 C1; F3 C4, C5, C12 |
| H7 | **Identity closure**: add `activity.py` and decide the rest (P1 C1, owner ruling 9); bundle the held cuts (`on_log` chain, `evidence.same_as`, `pair_grid(size=)`) into the same change; add `activity` to the second list in `tests/research/sweep/test_identity.py:196-228` | `app/research/sweep/identity.py:68-102` | 3/5 | P1 C1 (listing the closure shows `out app/research/golden_search/activity.py`) |
| H8 | **Test-bar list (lens 6)**, each naming its survivor in the slice report: F1 C2, C3, C4 (≈110 lines of SVG-walk helpers), C5; F3 C6, C7, C8; F4 C7; F2 C6, C9; P2 C16 a–i (`test_shared_pool.py:186-215` cannot fail on the behaviour it names; two timing asserts to loosen); P3 C9, C10, C11, C16, C17; P4 C5, C9, C12; P5 F9 (`test_chance_evidence_service.py:290` reduces to `worse == 0`, passes only on a degenerate fixture), F10 (no admit-level assert for `too_few_sessions` or `derived_return_overflow`), the two weak drawdown tests; P1 C10 | per slice | 6 | slice reports |
| H9 | **Wire fields served and never read** (cross-stack, needs serial OpenAPI + TS regen and fixture edits): the held contract cuts above plus `GoldenSearchExposure.{ledger_overlaps,outside_activity_overlaps}`, `GoldenSearchZoomRound.{first_evaluated,quantization_limit}`, `GoldenSearchKnobSummary.stop_reason`, `GoldenSearchVerdict.{successful_folds,defined_folds}`, `GoldenSearchChanceRehearsalMeasured.objective_percentiles_1x` (ruling 4), qualification provenance fields (F3 C13, P6 C13, P3 B3–B6); `SummaryLink.target`/`SummaryRow.key` as `Literal` and the description that omits `without-best` (P3 C15, F2 pointer) | `schemas/golden_search.py`, `broker.types.ts` | 5 | P3 B3–B6; P6 B1–B4, C13; F3 B5, B6, C13 |
| H10 | **Docs and comments, mechanical**: `engine-authority-map.md:51,79` ("research schema v11" is 20; the shared pool has no row; the module list omits planning, activity, guidance, actions, models); the manual's "Retain current settings" (button says "Keep"), "Read Plan check" (an `sr-only` heading), two labels for one docs page (F4 C8–C10); stale comments in `evaluator.py:566-570`, `worker.py:458-460,56-58` (P2 C11); the #3025 caveat in the report, manual and `incompatibleRank` once H2 lands (F4 C12) | as listed | 7 | F4 C8–C10, C12; P2 C11, C17; P4 C7; P1 C11 |

### Known duplicate, pointer only

- [#2900](https://github.com/tim1016/learn-ai/issues/2900): `golden-search-plan-backfill.ts` against `shared/symbol-catalog/ensure-coverage.service.ts`. Not re-filed.

### Cross-module pointers

- **Research-runtime wave (out of scope):** `app/jobs/checkpoints.py` (`TaskSilence`, `PHASE_SILENCE_LIMIT_S`, `CheckpointGate.last_accepted` have only test callers; production silence is `worker.py:64-80`; `GatedLease._seen` is an unbounded list); `research/benchmark.py:1332` (`WorkerDispatchContext` without `decoded_cache_bytes`, so each lane keeps 2 GiB); `grid_search/dispatch.py:248-296`, `recency/dispatch.py:275-325`, `benchmark.py:1206-1278` (import private `golden_search.worker` helpers and copy the terminal-sink block three times); `benchmark.py::protocol_width_histogram` and `round_widths` (test-only; P3 C13).
- **Backend:** `Backend/Jobs/JobsApi.cs` lists the job `params` hash, `stage_token` included, through `GET /api/jobs/` (P6 D).
- **Fixtures plumbing:** `tests/fixtures/conftest.py:36-37` writes `latest.json` under `except Exception: pass`; `golden_fixtures.py:71` serves `generated_at: str` (ISO) and the page slices it (P6 C6, an ADR 0022 (f) allow-list question).
- **Frontend shared:** `components/grid-search/grid-search-curves.component.ts`, `walk-forward-study/walk-forward-fold-curves*.ts` and `entry/entry.component.ts` import Golden Search chart infrastructure (`GoldenSearchChartComponent`, `curveStore`, `sessionCurvesOption`, `reducedMotion`); any move or cut must count them (F1 E). `--fs-2xl` and `--p-primary-color` are undefined tokens in the manual and knob-table styles.
- **Docs link:** `Frontend/ci-spec-durations.json:187` names the manual spec; follows any spec deletion.

## Slice reports

Appended unchanged (P1–P6 then F1–F4). "Cut candidates" rows there are the readers' proposals; the PR and the reconciliation above are what was done.


---

### Slice P1 — planning, protocol, search procedures and rules

## P1 reader report — #3156 Golden Search, planning / protocol / procedures / rules (read-only)

Pinned SHA `cb667942`. I changed nothing in the repo. Scratch probes and fuzzers are in `scratchpad/P1/` (`probe*.py`, `fuzz*.py`, `report.txt`, `cov_full.txt`).

**Headline**
- One confirmed lens-2 cut: `models.py::TERMINAL_STATES`.
- No P0/P1. Five P2 findings, listed in section C.
- **Cuts in identity-closure files strand resumable studies.**
  - Ten of my 14 production files are in `IDENTITY_SOURCE_PATHS` (`app/research/sweep/identity.py:68-102`).
  - Those ten are `budget, declarations, exam_rules, exposure_rules, grid_procedure, planning, procedure_history, protocol, selection, zoom`.
  - Any byte edit to them, a docstring included, moves `source_digest` and makes every in-flight study fail the code-identity check.
  - `models.py`, `actions.py`, `guidance.py` and `activity.py` are outside the closure.
- Tests: my 13 files give 262 passed, 4 skipped (all four are `test_qualification_defaults.py`, `POSTGRES_URL not configured`). Details in G.

---

### A. Feature table

"Reachable" means reachable in checked-in config via the `/api/research/golden-search/*` routes and the Golden Search page. Items I did not exercise end-to-end through HTTP are marked unverified.

| feature | requirement | entry point | reachable | normal / empty / failure verdicts | classification | evidence |
|---|---|---|---|---|---|---|
| Frozen protocol and validation | #2696 "Plan before seeing results"; ADR 0074 d1 | `protocol.py::GoldenSearchProtocol`, `validate_protocol`; via `planning.review_plan` / `prepare_lock` | yes (`POST /preflight`, lock) | lockable plan; every refusal returned as data with a stable code; empty plan returns `NO_SEARCHABLE_KNOB` | implemented, with a gap on the incumbent (C2) | `test_protocol.py::test_validate_protocol_refuses_with_its_code` (57 params), `test_planning.py::test_a_lock_refused_for_several_reasons_carries_every_refusal`; ran, pass |
| Protocol hash and legacy compatibility | ADR 0074 d9, d10 ("legacy keeps its hash") | `GoldenSearchProtocol.protocol_hash`, `from_dict`, `_knob_dict` | yes | key-order-independent hash; optional fields omitted when absent | implemented | `test_protocol.py::test_protocol_hash_*`, `test_activity.py::test_a_legacy_plan_keeps_its_hash_and_its_two_fixed_floors` |
| Declarations (EMA, EMA-previous) | #2696; #2902 | `declarations.py::_EMA_DECLARATION`, `_EMA_PREVIOUS_DECLARATION`, `declaration_for`, `unavailable_reason` | yes (`capabilities()`) | other strategies return a plain reason | implemented | `test_declarations.py` (all knobs vs the registered param schema; domain/default parity probe in `probe4.py` OK for both strategies) |
| Canonical point and quantization | #2696 "no float-drift duplicate points" | `declarations.py::canonical_point`, `snap`, `quantize`, `scalar`; `planning._canonical_or_raw` | yes | idempotent over a 4,000-case fuzz except negative zero | partial: stated invariant violated for −0 and sub-quantum overshoot (C3) | `test_declarations.py::test_canonical_point_*`; `fuzz2.py` |
| Run-up planning and data refusals | ADR 0074 d2 "refused, not carved" | `planning.plan_study_run_up`, `review_plan`, `_unread_sessions_refusal` | yes | `RUN_UP_HISTORY_MISSING`, `DATA_MISSING`, `DATA_UNREADABLE` with backfill and nearest-valid dates | implemented | `test_planning.py` (about 12 tests: hole, early stop, unreadable, backfill window pinned by `_pin_today`) |
| Default plan, picked range, interval fit | #2811 §2; #2886 | `planning.default_protocol`, `default_final_interval`, `fit_range`, `range_fit_sentence`, `study_range_at` | yes (`service.defaults`) | whole-fold plan, or a refusal naming the needed start | implemented | `test_planning.py` default/range tests; `fuzz1.py`: 2,500 random ranges and 1,500 default layouts, zero fold or interval mismatches |
| Expected trade frequency | #2811 §1; ADR 0074 d9 | `activity.py::window_activity`, `activity_plan`, `TradeFloors`; `protocol._validate_trade_floors`; `planning.build_receipt` | yes (default 50) | floor per window frozen in receipt; zero-session window is `NO_TRADING_DAYS`; missing receipt raises, never falls back | implemented; `activity.py` is outside the code-identity closure (C1) | `test_activity.py` (13 tests: PRD table, cross-year, early close, freeze vs calendar change, forward floor, legacy hash) |
| Importance order | ADR 0074 d10 | `protocol.by_importance`, `KnobPlan.importance`, `_validate_importance` | yes | 1–10 on every knob or none | implemented | `test_planning.py::test_the_search_order_matches_the_order_the_plan_page_shows` (shared fixture `contracts/fixtures/golden-search-importance-order-v1.json`, read by `golden-search-plan-draft.spec.ts` too) |
| Zoom procedure | #2696 "two search tools"; ADR 0074 d2 | `zoom.py::run_zoom`, `_ZoomRun`, `PointEvaluator` | yes (`method="zoom"`, default) | stop reasons `no_improvement`, `quantization_limit`, `pass_limit`, `budget`, `no_eligible` | implemented | `test_zoom.py` (22 tests incl. interacting-landscape miss, bound property over 25 random settings) |
| Grid procedure, pair audits, neighbor probes | same | `grid_procedure.py::run_grid`, `pair_grid`, `neighbor_probes`, `neighbor_at` | yes (UI method choice, `golden-search-method-choice.component.ts`) | invalid and outside-domain cells are not evaluated; no eligible winner returns the seed | implemented | `test_grid_procedure.py`; `test_zoom.py` pairwise-trap tests |
| Selection and ranking | #2696 "Weighted places" | `selection.py::ineligibility`, `best`, `objective_value` | yes | single measure; rank-reversal counterexample pinned | implemented | `test_selection.py::test_best_resists_the_rank_reversal_that_breaks_blended_places` |
| Workload estimate and refusal | ADR 0074 d2 (≤5,000, reserve exam and proof) | `budget.py::estimate`, `review_protocol`, `workload_refusal` | yes | `WORKLOAD_LIMIT`, `BUDGET_CAP_INVALID` | implemented | `test_budget.py` (7 tests) |
| Final-test rules | #2696 "Open one final test"; #2930 | `exam_rules.py::judge_exam`, `retention` | yes (stages) | null is never a pass; failed run is `could_not_evaluate` | implemented | `test_exam_rules.py` (9 tests) |
| Exposure state and claim | ADR 0074 d4 | `exposure_rules.py::exposure_state`, `claim_for` | yes (service) | ledger outranks unknown outside activity | implemented | `test_exposure_rules.py` |
| Fold windows, linked returns, fold evidence | ADR 0074 d3 | `procedure_history.py` | yes | a missing fold breaks the line, never interpolated | implemented | `test_procedure_history.py` |
| Operator copy | #2696 "Angular renders, never composes" | `guidance.py` | yes (views) | closed maps keyed by state, code or situation | implemented; the Run-research copy branches (`study_guidance` lines 97-108) have no non-Postgres test | covered only via `test_study_service.py:364,376` (Postgres-gated) |
| Command permission table | #2696 commands | `actions.py::action_refusals`, `permitted`, `authorize` | yes | one table serves both the view and the handler | implemented | `test_views.py` (imports `action_refusals`, `permitted`, `presented_status`); `authorize` is exercised only by Postgres tests |
| Study records and refusal type | #2696, ADR 0074 | `models.py` | yes | — | implemented; `TERMINAL_STATES` dead (B1); `COMMANDS` duplicates `CommandName` with no parity test (C5) | — |

Notes on classification and PRD drift:
- **Default Zoom passes.** PRD #2696 says default Zoom "up to three passes". Code defaults are `points=5, refinements=2` (three rounds), `passes=2` (`protocol.py:99-102`). I found no ADR amendment naming 2. Classified unverified; not filed.
- **"Activity feed".** There is no activity-feed module in the unit. `activity.py` is the expected-trade-frequency policy. The closest history record is the study trial ledger (repository, not mine).

---

### B. Cut candidates (lens 2)

| id | path::symbol, lines | what | callers searched | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| B1 | `PythonDataService/app/research/golden_search/models.py::TERMINAL_STATES` line 53 (`frozenset({"approved","retained","closed"})`) | module constant; never read. Added in the first commit `59f96f96` and never imported. | `git grep -n -w TERMINAL_STATES` finds only the definition. `git grep -n -i terminal_states` also finds only `UNFILLED_TERMINAL_STATES` in `app/broker/alpaca/clerk/sqlite/*` — a different, money-path symbol. No `.json`, `.md`, `.ts`, `.yml`. No `getattr`/string dispatch. `git log -S` shows no commit ever referenced it. | nothing (no test, doc, fixture or config). `models.py` is outside `IDENTITY_SOURCE_PATHS`, so no study is stranded. | no | confirmed |
| B2 | `grid_procedure.py::pair_grid` line 234 (`size: int = PAIR_GRID_SIZE`) and lines 238-239 (`if size < 2: raise ValueError(...)`) | a keyword nobody passes plus a guard no test executes (coverage-uncovered line 239). | both callers (`stages.py:346`, tests at `test_grid_procedure.py:122,140,152,209`, `test_evidence.py:103`) call without `size=`. `git grep -n "pair_grid("` lists all call sites. | `_landscape_axis`'s `size` stays, fed by the constant. No tests or docs. `grid_procedure.py` is in the identity closure, so editing it moves identity. | no | probable (parameter only; lead may prefer to treat it as a finding) |

Test-only helpers are findings, not cuts: see C4.

---

### C. Finding rows (not cuts)

| id | location | lens | sev | evidence | disposition | what a fix orphans |
|---|---|---|---|---|---|---|
| C1 | `app/research/sweep/identity.py:68-102` omits `golden_search/activity.py` | 3 / 5 | P2 | `activity.py` was created 2026-10-02 (#2811, `d2d9340f`) and never added to the list that was extended on 2026-10-01. `TradeFloors.policy(window)` is what `stages.run_procedure` hands to Zoom and Grid as the selection policy (`stages.py:289`), so a code change to it changes eligibility and winners. Probe: listing the closure's files shows `out app/research/golden_search/activity.py`. Frequency floors are frozen in each receipt, so today only the plumbing is exposed, not the numbers. | fix: add `"activity"` to the module tuple (one-word change in another unit's file). Pointer only: `chance_*.py`, `concentration.py`, `trade_net.py`, `compare_measures.py`, `final_measures.py` are also outside the closure yet `stages.py` calls some of them. The owning unit should decide whether each computes a stored number. | nothing |
| C2 | `protocol.py:621-632 _validate_incumbent`; `planning.py:216-234 _warmup_values` (line 218) and `:237-255` | 3 / 5 | P2 | The incumbent's knob values are never validated. `_validate_incumbent` checks only source, qualification id and `params.symbol`. Probe (`probe8.py`): `GoldenSearchProtocolRequest` accepts `incumbent.params={"gap":"wide",...}` (`params: dict[str, Any]`, `schemas/golden_search.py:114`); `service.preflight` then raises raw `ValueError: 'wide' is not a number` out of `knob_values`. The router maps only `GoldenSearchRefusal` (`routers/golden_search.py:125-310`) and `main.py` has no `ValueError` handler, so the answer is a 500 instead of a refusal. Same for `None`, NaN, `True`. An unknown key (`{"bogus":1}`) or an off-domain value (`gap=5.0`) passes preflight as lockable. The engine then fails the incumbent at the evidence stage, so `BEATS_INCUMBENT` is `not_available` — it fails closed, visibly, after budget is spent. `_validate_seed` already does this check (`SEED_INVALID`), so the asymmetry is the defect. The UI echoes the server's own default, so only a direct API caller reaches it. | fix: validate incumbent values like the seed (finite number, known knob names; domain is deliberate for stale qualifications, see D). | nothing |
| C3 | `declarations.py:386-412 canonical_point`, `:343-354 quantize/scalar`; `planning.py:147-155` | 4 | P2 | Docstring lines 389-391 says a value outside the domain "is refused, not moved". Code snaps first, then checks the domain, so a raw value up to half a quantum outside is moved onto the boundary and accepted, and a tiny negative becomes `-0.0`. Repro (`probe7.py`): `canonical_point("ema_crossover_signal","SPY",{"gap":-0.004})` gives `gap=-0.0`; `{"gap_bps":-0.2}` gives `gap_bps=-0.0`; `point_hash` of `-0.0` differs from `0.0`, so one setting has two hashes (violates the module's "one setting, one hash" statement). `fuzz2.py`: 503 of 4,000 draws hit it; nothing else broke. Reach: seed and incumbent values typed by a client. The seed is accepted instead of `SEED_OUTSIDE_DOMAIN`, and the Zoom start can re-evaluate "the current value" under a second hash. | fix: refuse by the unsnapped value, and normalise −0 → 0 in `snap`. | nothing |
| C4 | `budget.py:179-181 ProtocolReview.lockable`, `:183-188 ProtocolReview.as_dict` | 2 / 7 | hygiene | Only tests call them: `test_budget.py:81,83,92`; `test_planning.py:490,644,655,668` use `review(...).lockable` as "no refusals". Production reads `.refusals/.folds/.estimate` (`planning.py:446-448,555-556`). `plan.lockable` at `planning.py:572` is `PlanReview.lockable`, a different property. | leave-with-reason (brief: test-only helper). If cut: remove one assert at `test_budget.py:83` and rewrite five `.lockable` uses as `not review.refusals`. `budget.py` is identity-closure. | edits to 2 test files |
| C5 | `models.py:40-43 COMMANDS`, `RETAIN_KINDS` vs `CommandName`, `RetainKind` Literals (lines 32-36); `exam_rules.py:40 _MEASURE_NAMES` vs `sweep/ranking.py:25-26 RANKING_MEASURES` | 7 | P2 robustness | Two hand-kept lists of one closed set, no parity test. A command added only to the Literal makes `action_refusals`'s `dict.fromkeys(COMMANDS)` lack it: `KeyError` at `service.py:793`, a 500. A fourth ranking measure raises `KeyError` in `_beats_incumbent`. Both fail closed. | fix: derive with `typing.get_args`, or add a parity assertion | nothing |
| C6 | `planning.py:630-646 _qualified_seed_refusals` (`SEED_IS_QUALIFIED`), also `activity.py:118,125` | 3 / 6 | P2 | The leakage gate that ADR 0074 "Anti-patterns rejected" names ("seeding … from today's newly approved tuple") has its only test inside the Postgres-gated `test_study_service.py:1263`. Non-DB coverage run leaves `planning.py:635-637` unexecuted. I confirmed the gate works without a DB (`probe5.py`: qualified seed gives `['SEED_IS_QUALIFIED']`, registry-equal seed gives no seed refusal). The same file holds all other leakage tests (`:704-760`: later-fold perturbation, fold seed, final-interval isolation). All skip on PR CI by design (daily tier). | leave-with-reason (by design per AGENTS.md "Gates"); a DB-free twin for this pure rule would be cheap | — |
| C7 | `planning.py:147-155 _canonical_or_raw` → `protocol_from_request` | 4 | P2 latent | An off-lattice qualified tuple is silently snapped when the incumbent is canonicalized. Probe (`probe3.py`): qualification incumbent with `rsi_min=29.6` freezes as `rsi_min=30.0`, while the label still names the qualification. Reachable only when a declaration's quantum changes after a qualification was minted; none does today. | owner-ruling (design: canonical point is identity). Worth a one-line note in ADR 0074 if the quantum ever changes. | — |
| C8 | Duplicate utilities (identity-closure files) | 7 | hygiene | `_finite(float\|None)` in `exam_rules.py:60`, `selection.py:126`, plus `evaluator.py:319`, `repository.py:953`, `sweep/ranking.py:47`, `walk_forward_study/verdict.py:90`. `Window = tuple[int,int]` in `activity.py:30`, `evaluator.py:84`, `decision_summary.py:24`, `run_curves.py:234`, `search_charts.py:247`. | leave-with-reason: editing `exam_rules`/`selection` strands studies; bundle with a change that already moves identity | — |
| C9 | non-DB-uncovered branches | 6 | hygiene | Never executed by any non-Postgres test (cov: `cov_full.txt`): `declarations.py:102,312,327,330`; `protocol.py:283,470,501-502,514-515,554,624`; `grid_procedure.py:66,75,170,239`; `zoom.py:239,244,404`; `planning.py:250-253,261,328-329,364,422,589-595,653,756,862-863`; `activity.py:118,125`; `guidance.py:97-108,281,309,333,350,379,390-398`. I probed the protocol non-finite branches (`probe6.py`): all behave. | leave | — |
| C10 | `test_declarations.py::test_ema_knob_agrees_with_the_registered_parameter_model` | 6 | hygiene | parametrized over EMA's 7 knobs only; `ema_previous_crossover` parity (incl. `cross_max_age_bars` default) is a literal `1`. I checked all 15 knobs against the model schema (`probe4.py`): all match. | leave; parametrize over both declarations | — |
| C11 | `docs/architecture/engine-authority-map.md:79` | 7 | hygiene | Row says "research schema v11"; `app/research/persistence/schema.py:57` has `SCHEMA_VERSION = 20`. The module list omits `planning`, `activity`, `guidance`, `actions`, `models`. | pointer: docs owner | — |

---

### D. Considered and kept

- **`StudyRunUp.probed_points` and `bar_span_ms`** (`planning.py:206-213`): attribute-read only in `as_dict`. They flow into `execution_context(... run_up=run_up)` → `context_digest`, so removing them changes every locked study's digest.
- **`ProtocolRefusal.message` using `date.isoformat()`** (`planning.py:261,336,350,779-823`): operator prose, not wire or storage. The wire fields are `*_ms`.
- **`date` values from `default_final_interval`, `fit_range`, `RangeFit`**: trading dates used module-to-module and passed to the canonical calendar; `service.defaults` converts to `*_ms` at the boundary. Within the rule's "local arithmetic" carve-out.
- **`canonicalize=` keyword on `run_zoom`, `run_grid`, `pair_grid`, `neighbor_probes`, `neighbor_at`**: only tests pass it, to inject the synthetic point builder (the synthetic program is unregistered). A test seam, not dead.
- **`declarations.py:312` (non-numeric-parameter reason) and `_WEAKNESS_COPY["EXPOSURE_NOT_OPENED"/"EXPOSURE_UNKNOWN"/"EXAM_MISSING"]`**: defensive keys with no checked-in trigger; deleting saves little and risks a `KeyError`.
- **`test_declarations.py::test_ema_declaration_follows_the_owner_order_and_table`**: reads as a copy pin, but `quantum` and `domain` feed point identity, so changing them re-keys every stored point.
- **`test_selection.py::test_metrics_round_trip_through_their_dict`**: weak, but only it round-trips `Metrics.error`; keep.
- **`test_activity.py::LEGACY_PROTOCOL_HASH`** (`2a155fec…`): a frozen external fact (legacy hash stability), a fixture, not a copy pin.
- **`test_budget.py::test_estimate_serial_seconds_use_each_stage_window_length`**: pins arithmetic on Grid Search's 1.4 s / 0.3 s constants; derived, not copy.
- **Incumbent off the declared domain passing validation**: deliberate. `test_planning.py::test_a_widened_range_stays_inside_the_domain_on_the_quantum` and `default_protocol` handle a stale qualification outside today's domain. C2 is only about non-numeric values and unknown keys.
- **Incumbent with a ≥20-character symbol under a qualification source**: `review_plan` raises `ValidationError` from `registry_incumbent`, but HTTP caps `symbol` at 16 characters (`schemas/golden_search.py:122`). Unreachable from the router. Not filed.
- **`test_qualification_defaults.py` skipping locally**: Postgres-gated by design; `daily-tests.yml` sets `POSTGRES_URL` and `POSTGRES_URL_IS_EPHEMERAL=1`, so it runs daily.
- **Time-zone/hardcoded session times, `np.allclose` without tolerance, `# type: ignore`/`noqa`/`print`/`TODO`**: none in the 14 production files. Test `np.isclose`/`allclose` calls all carry explicit `atol`/`rtol`.

---

### E. Pointers and waits-on

**Symbols whose only production callers are in other units** (they die only if the owning unit drops its call):

| symbols | used by |
|---|---|
| `planning`: `protocol_from_request`, `review_plan`, `preflight_view`, `prepare_lock`, `study_id_for`, `request_sha256`, `registry_incumbent`, `default_protocol`, `default_final_interval`, `fit_range`, `range_fit_sentence`, `study_range_at`, `DEFAULT_{FINAL,TRAINING,TEST}_MONTHS` | service (and router for the months constants) |
| `guidance`: `study_guidance`, `weakness_items`, `knob_stop_explanation`, `candidate_guidance`, `candidate_flags`, `fixed_sentence`, `costs_sentence`, `data_source_sentence`, `validation_explanation` | views |
| `guidance`: `research_weakness` | service, stages |
| `guidance`: `selection_note` | views, `schemas/golden_qualifications.py` |
| `guidance`: `params_sentence` | run_curves, service, views |
| `guidance`: `usd` | decision_summary, trade_net |
| `actions`: `authorize` | service, stages |
| `actions`: `unclaimed`, `presented_status`, `action_refusals`, `STAGE_START_STATES`, `LIVE_PRESENTATIONS` | service |
| `actions`: `permitted` | views |
| `exposure_rules`: `exposure_state`, `claim_for`, `EXPOSURE_EXPLANATIONS` | service |
| `exam_rules`: `judge_exam` | stages |
| `grid_procedure`: `run_grid`, `pair_grid`, `neighbor_probes` | stages |
| `grid_procedure`: `neighbor_at` | run_curves |
| `grid_procedure`: `AuditCell`, `PairGrid` | evidence |
| `zoom`: `run_zoom` | stages |
| `zoom`: `BudgetExhausted` | evaluator, stages |
| `zoom`: `RetryAllowanceExhausted` | evaluator |
| `zoom`: `ProcedureResult` | search_charts, stages, views |
| `budget`: `PROOF_EVALUATIONS` | stages |
| `procedure_history`: `fold_return`, `link_fold_returns`, `fold_evidence` | stages, fold_charts |
| `procedure_history`: `FoldWindow` | run_curves, service, stages |
| `activity`: `TradeFloors` | chance_rehearsal, decision_summary, fold_charts, plan_charts, search_charts, service, stages, views |
| `activity`: `trading_years` | plan_charts |
| `activity`: `DEFAULT_EXPECTED_TRADES_PER_YEAR` | service |
| `models`: `COMMANDS`, `RETAIN_KINDS`, `STAGE_STEPS`, `STAGE_ESTIMATES`, `ESTIMATE_ROW_OF_STEP`, `CommandRecord`, `EvaluationRecord`, `require_mapping` | service, stages, repository, plan_charts, run_curves, search_charts, views |
| `models`: `StudyRow` | actions (unit), plus 8 non-unit modules |

Router and schema imports from the unit (`schemas/golden_search.py`): `KnobKind`, `Method`, `KnobMode`, `IncumbentSource`, `ZoomSettings`, `SelectionPolicy`, `ExecutionAssumptions`, `DEFAULT_STRESS`, `MAX_BUDGET_CAP`, `StopReason`, `ExamOutcome`, `CheckStatus`, `ExposureState`, `Claim`, `CandidateKey`, `CommandName`, `StageName`, `StudyState`, `RefusalKind`.

**Other pointers**
- `tests/research/sweep/test_identity.py:196-228` pins two lists: guidance/views/approval are *not* in the closure, and zoom/evaluator *are*. If C1 is fixed, add `activity` to the second list.
- `PythonDataService/scripts/pr_shard_durations.json` carries node ids of my tests (e.g. `test_activity.py::...`). If tests are deleted, those entries go stale.
- #2900 (one runner for trade-bar backfill) is not touched by my files.
- `planning.py:64` (`trading_date_to_calendar_anchor_ms`) and `golden_search/service.py::lake_sessions` (`except Exception` at `service.py:285`, logs a warning, returns `None`) are the service unit's; `test_lake_sessions.py` exercises them.
- Pending-owner call (not a cut): `activity.py` is outside identity and `chance_*`, `concentration` etc. are too; "is that intended" needs the owner.

---

### F. P0/P1 candidates

None. I looked for false state the operator would act on: the leakage gates, the trade-floor receipt, selection and exam rules, exposure state, workload bound. I fuzzed the planner against the fold validator (4,000 cases, 0 mismatches) and canonicalization (4,000 draws; only the −0 case from C3 fell out). C2 and C3 are the closest, but both fail closed or are visible and are reachable only by a hand-built API payload. Both are P2.

---

### G. Coverage

**Read in full**
- All 14 production files (`__init__, planning, declarations, protocol, grid_procedure, zoom, budget, exam_rules, exposure_rules, guidance, selection, procedure_history, models, actions, activity`).
- All 13 listed test files plus `tests/_helpers/golden_search.py`.
- `tests/research/conftest.py` (Postgres gate).

**Read in part, for context only**
- `service.py` (capabilities, preflight, lock, commands), `stages.py` (procedure runner), `evaluator.py` (run failure handling), `schemas/golden_search.py` (requests).
- `sweep/ranking.py`, `sweep/eligibility.py`, `sweep/identity.py`.
- ADR 0074 decisions 1–10 and 12; PRD #2696 and #2811 in full.

**Tests run**
- `pytest` over my 13 files: **262 passed, 4 skipped**, 22.8 s. The 4 skips are `test_qualification_defaults.py:28,63,102,122` (POSTGRES_URL not configured).
- `tests/research/golden_search` plus `tests/routers/test_golden_search_endpoints.py` with branch coverage: **755 passed, 146 skipped**, 70 s. All skips are "POSTGRES_URL not configured" / "live-DB endpoint tests need an ephemeral POSTGRES_URL".
- `ruff check` on `app/research/golden_search/`, `tests/research/golden_search/`, `tests/_helpers/golden_search.py`: clean. Scratch `ruff --isolated` with F, B, ARG, SIM, C4: clean.

**Not covered**
- Frontend components (other readers).
- Postgres-gated paths. I did not run them; I did not point `POSTGRES_URL` anywhere.
- `repository.py`, `evaluator.py` internals, `approval.py`, `qualifications.py` (other units).
- A live HTTP round-trip of the 500 in C2. I reproduced it at `service.preflight`, one frame below the router, and confirmed from the router source that only `GoldenSearchRefusal` is mapped.

---

### Slice P2 — evaluation engine, stages and the worker pool

# P2 reader report: Golden Search evaluation engine, stages and worker pool (read-only, pinned `cb667942`)

No P0/P1 confirmed. All eight production files and all seven test files were read in full. The Postgres-gated tier did not run locally: no Postgres is available and the brief forbids pointing at a container.

## A. Feature table

All files are under `PythonDataService/app/research/golden_search/`. "Default" means checked-in config selects it with no operator action.

| feature | requirement | entry point | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| Stage runners: search, validation + evidence, exam (both fits), qualification; fenced writes; replay-free Finish | #2696, #2811, #2930, ADR 0074 | `stages.py::execute_stage` ← `service.run_stage` ← `routers/golden_search.py::_stage_work` / `_gated_stage_work` | default (`RESEARCH_EXECUTION_MODE=local`, `config.py:356`) and runtime profile | Normal: state advances and `ctx.write` persists under the fence. Budget out: `incomplete` / fold `BUDGET`. Stale attempt: left alone. Cancel: `_end_run cancelled`. Other failure: `_end_run failed` + reason. Code moved: `StageRefusedError`. | **unverified locally.** Every stage-level test is DB-gated. | `test_study_service.py`: 1 passed, 60 skipped (POSTGRES_URL). Only `_final_chance_for` integrity propagation is run locally (`test_evaluator.py::test_final_chance_propagates_a_durable_evidence_integrity_failure`). |
| Evaluator: cache identity, capability fence, atomic reservation, retry allowance, uncertain-write reconcile, durable detail readback | #2696, #3039, #3040, #3045 | `evaluator.py::StudyEvaluator.evaluate` | default (`serial_dispatch`) | Normal: results in input order. `BudgetExhausted` after the affordable prefix. Cancel: release the admitted prefix. Loss: re-admit under a new generation. | **implemented** | `test_evaluator.py` 38 pass; `test_worker_loss_recovery.py` fast tier |
| Engine projection (`engine_request`, `project_response`, `detail_payload`, daily curve) | #2696, #2930 | `evaluator.py` | default | Failed run: metrics failed, no detail, no curve. No day-ends: no curve, result kept. | **implemented** | `test_evaluator.py::test_the_projection_*`, `test_a_failed_engine_run_is_a_failed_evaluation_with_its_reason` |
| Spawned worker, one lane (child + `ResearchWorker` + `WorkerDispatch`) | ADR 0077 §7, #3044, #3045 | `worker.py` | `RESEARCH_EXECUTION_MODE=research` + compose profile `research`; `RESEARCH_WORKER_COUNT` defaults 1 (`compose.fleet.dev.yaml:229-234`) | Normal: result equals the serial answer. Identity mismatch: `IdentityRefusalError`, terminal. Silence 45 s / no task progress 30 min: kill, `WorkerLost`. | **implemented** | `test_worker.py::test_a_spawned_worker_returns_the_serial_answer_exactly[False/True]`, `::test_a_refused_sealed_identity_*`, `::test_close_kills_and_reaps_*` (real children ran) |
| Pooled dispatch + shared pool (fair ring, warm affinity, global bounds, window permits) | #3046, #3048 | `pool.py::worker_pool_factory`, `shared_pool.py::SharedWorkerPool` | opt-in `RESEARCH_WORKER_COUNT>=2` (`routers/golden_search.py:483-522`; runbook `research-runtime-capacity.md`) | Loss is one item's outcome. Closing pool cancels home. Oversize wire: terminal refusal. | **implemented (fast tier).** Real-children tier is DB-gated, so unverified. | `test_worker_pool.py`, `test_shared_pool.py` pass; my soaks: 150 + 300 + 200 randomized cases, no invariant violation |
| Admission verdict relay, batched | #3047 | `ResearchWorker._answer_admission` | runtime modes | admitted / refused / `unavailable` (typed outage) | **implemented** | `test_worker.py::test_the_verdict_relays_through_the_batcher_*` |
| Operator drain `POST /api/jobs-internal/pool/drain` | #3048 AC10 | `routers/golden_search.py:525-551`, `shared_pool.py::drain_shared_pool` | runtime, secret-gated | no pool: `drained:false`. Otherwise stop, wait ≤60 s, reap, latch. | **implemented**, but see C1 | `test_shared_pool.py::test_drain_*`, `test_the_drain_route_*` |
| Memory / PID guards | #3048 AC6/7 | `shared_pool.py:766-823` | runtime pool | pause assignments, or refuse startup | **partial.** Unit-tested via monkeypatch; live cgroup read unverified. See C7. | `test_memory_pressure_pauses_*`, `test_startup_refuses_*` |
| Capacity-handoff renewal + waiting facts | #3048, #3052 | `pool.py:158-196`, `routers/golden_search.py:458-522` | runtime pool | renew once per 0→1 in-flight; waiting event on transition only | **implemented** | `test_the_handoff_renewal_*`, `test_the_runtime_factorys_handoff_*` |
| Final-test fit records | #2930 | `final_test.py` | default | new shape (`fits`) read; legacy single-candidate shape read | new shape **implemented**; legacy arm **unverified** (no test) | `test_views.py:145-152`, `test_study_service.py:151,895`. `before_test` appears in no test. |
| Development vs final measures | #2821 | `final_measures.py::final_comparison` ← `views.py:602` | default | failed/missing run: none; y=0: none | **implemented** | `test_final_measures.py` 5 pass |
| Per-trade net + reconcile | #2821 | `trade_net.py` ← `concentration.py`, `trade_charts.py` | default | does not reconcile: reason text, no numbers | **implemented** | `test_trade_net.py` 3 pass |

## B. Cut candidates (lens 2, delete-only)

| id | path::symbol and lines | what | callers searched, and what each hit is | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| B1 | `stages.py:141` (`StageContext.on_log`), `:1016`, `:1064`, `:1110` (`execute_stage(on_log=)` and recursion); `service.py:1071,1088` (`run_stage(on_log=)`); `routers/golden_search.py:410,448` (`on_log=emit.log`) | Stage log callback that nothing ever calls. `ctx.on_log(` has zero hits; the engine's own `on_log` args are `lambda message: None` (`evaluator.py:406`, `worker.py:392`). | `git grep -n -w on_log -- PythonDataService/app/research/golden_search PythonDataService/app/routers/golden_search.py`: only definitions and pass-throughs. `\.on_log\b` hits elsewhere belong to walk_forward/options_backtest, not this chain. | test args `test_study_service.py:1610`, `test_worker_loss_recovery.py:471`; positional `StageContext(...)` in `test_activity.py:162,199` (10th arg). Spans units: service.py and the router. | no | confirmed |
| B2 | `worker.py:488-489` (`WorkerDispatchContext.job_id`, `.generation`); writers `stages.py:203-204`, `grid_search/dispatch.py:474-475`, `recency/dispatch.py:491-492`, `benchmark.py:1337-1338` | Two required fields never read in `app/`. | `grep -rn "context\.\(job_id\|generation\)\|_context\.\(job_id\|generation\)" app` is empty; `asdict/vars/replace(context)` is empty. Only test `test_worker_pool.py:656` reads `context.job_id`, as a label. | about 30 constructors in tests (`test_worker*.py`, `test_shared_pool.py`, `grid_search/test_dispatch.py`, `recency/test_dispatch.py`). Low value, wide blast radius; lead's call. | no | confirmed (no reader) |
| B3 | `shared_pool.py:249,253` (`SharedWorkerPool.__init__(spawn=)`; `self._spawn = spawn or ResearchWorker`) | Injection param nobody passes. Tests monkeypatch `shared_pool_module.ResearchWorker` before construction. | `grep -rn "spawn=" app tests` returns zero hits for this class. | none | no | confirmed |
| B4 | `worker.py:534,580-583` (`ResearchWorker._pump`) | Attribute assigned, never read (test stub sets it at `test_worker.py:320`). | `grep -rn "\._pump\b"`: other hits are different classes. | the stub line | no | confirmed, trivial |
| B5 (WAITS) | `final_test.py:64-78` (`recorded_fits` legacy arm), `:60-61` (`pick_of` `before_test`); `app/schemas/golden_search.py:1488` literal; `golden_qualifications.py:72` | Read shape for exam records written before #2930 (2026-10-06). No producer exists in `app/`. | `git grep -n "candidate_point\"\|candidate_metrics\"" -- app`: reader only. Owner comment on #2933 (2026-10-12): "delete the old studies". | schema literal and Frontend `pick` handling | yes if the literal narrows | **waits on a data check** (do any stored studies lack `results.exam.fits`?) and the owner's deletion. Do not cut before then. |

## C. Findings (not cuts)

| id | location | lens | sev | evidence | disposition | what the fix orphans |
|---|---|---|---|---|---|---|
| C1 | `shared_pool.py:686-707` `drain()` | 3/5 | P2 (robustness) | `with self._state_changed:` then `if self._closing: return self.facts()`, and `facts()` re-takes the same non-reentrant `Lock` (`:259`, `:740-742`). **Repro:** `SharedWorkerPool(...,spawn=lambda c:None)`, `.close()`, then `.drain(wait_s=1)` in a thread. It prints `returned within 5 s: False`. Unreachable via the route because `drain_shared_pool` nulls the singleton first (`:1152`). | fix (snapshot facts outside the lock, or `RLock`) | nothing |
| C2 | `shared_pool.py:664-684` `_cancel_owner_children`, with `worker.py:795-800` | 5 | P2 | The pool-wide lock is held while cancelling each of an owner's lanes sequentially (`stopper.join(5.0)`), and each `ResearchWorker.cancel()` sleeps 1 s. **Repro:** 4 lanes, fake child whose `cancel` sleeps 1 s: a second owner's `facts()` blocked **3.81 s**. All owners' `offer`/`consume`/`_deliver`/`facts` stall; 8 lanes is 8+ s. | fix: drop the sleep (C3), then lock hold is about ms per lane | none |
| C3 | `worker.py:795-800` `cancel()`; child `:196,262-263,383-397,421-426`; `:848-855` | 3 | P2/hygiene | The child cannot observe a cancel. `raise_if_cancelled` is wired only to `while_waiting`, which fires only when the process-wide run gate is contended (`run_gate.py:151-158`), and a one-lane child never contends. **Repro:** an uncontended `one_backtest_in_flight(while_waiting=...)` calls it 0 times. Measured with a real child: `cancel()` 1.01 s vs `close()` 0.00 s. The 1 s grace and the `cancel` message only delay the SIGKILL. | fix: `cancel()` becomes `close()`. If the owner agrees cancellation is kill-only, the wire `cancel`, `cancel_flag`, and the `failed:"cancelled"` branches orphan. Tests `test_worker_pool.py:1005-1065` use that message from fake children, so they must be re-based on `JobCancelled` from the poll. | the items listed |
| C4 | `worker.py:420-434` vs `evaluator.py:415-418, 1075-1082` | 5/7 | P2 | Failure text differs by execution mode. The child writes `f"{type(exc).__name__}: {exc}"`, so an `HTTPException` reads `HTTPException: 400: ...`; local mode uses `_failure_message` ("engine refused the run: ..."). It reaches the user-facing sentence `stages.py:426` ("The winner's test run failed: ..."). No test pins either wording. | fix: child calls `_failure_message` | nothing |
| C5 | `worker.py:420-434`, `delivered_outcome` `:856-862`, `repository.py:756-766` | 3 | P2 | Any child exception, including `CatalogUnavailableError`, a 60 s admission-wait `LakeAdmissionError`, and `BaseException`s, is persisted as a `failed` evaluation, then served as cached for the study's life (Finish included). Same as local mode (`_run`), and pinned as designed by `test_a_failed_engine_run_is_recorded_as_evidence_and_the_batch_continues`. It is visible as an invalid point with its error text. | owner-ruling: should outage-class errors be a retryable loss instead of evidence? | n/a |
| C6 | `worker.py:682-691` `_send` | 3 | robustness | `except (BrokenPipeError, ValueError, OSError): pass`. The `ValueError` also covers `json.dumps(..., allow_nan=False)`, so an unencodable task is never sent and waits the 30 min task-silence bound (×3). Unreachable today: inputs are validated finite (`schemas/golden_search.py:63`, `declarations.py:326`). | fix: narrow, or log | nothing |
| C7 | `shared_pool.py:766-823` | 3/5 | P2, unverified live | Guards read cgroup-v2 files. Absent, or `memory.max == "max"`, silently disables them, with no startup log. The pressure test is `memory.current > 0.9*memory.max`, and `memory.current` includes reclaimable page cache. If decoded caches or cache fill the cgroup, assignments pause, and the only recovery is memory dropping while new work (the only thing that evicts) is blocked. `_pump` waits forever (cancellable), shows "capacity", and logs once. | needs one live read of `/sys/fs/cgroup/memory.current` vs `memory.stat anon` in the research container. If confirmed, P1-adjacent for a stalled study. | nothing |
| C8 | `stages.py:816-842` | 3 | robustness | The fit-to-fit `except Exception` lacks the `except EvidenceIntegrityError: raise` that the other three diagnostic sites carry (`:561,585,610,759,779`). Unreachable now: `detail_of` for just-evaluated final fits is served from `_details`. | fix (one clause) | nothing |
| C9 | `worker.py:560-570` | 5 | robustness | Child env is `{**os.environ, ...}`, so POSTGRES_URL, REDIS_URL, DATA_PLANE_CONTROL_SECRET and Alpaca keys reach a process documented as holding "no database, Redis, provider or broker handle". Isolation is by convention only. | fix (allow-list) or owner-ruling | nothing |
| C10 | `worker.py:473-474` | 7 | hygiene | `if __name__ == "__main__": raise SystemExit(_child_main())` sits mid-module, before `WorkerDispatchContext` and `ResearchWorker`. Any child-needed definition added below it fails only at runtime. | fix (move to end) | nothing |
| C11 | `evaluator.py:566-570`; `worker.py:458-460`; `worker.py:56-58` | 7 | hygiene | "Production dispatch stays serial until the worker runtime exists" (stale since #3044). "Parity with ... `DECODED_CACHE_DEFAULT_BYTES`" (2 GiB here vs 1 GiB at `benchmark.py:200`). "re-exported for the tests" while the module uses it. | fix comments | nothing |
| C12 | `pool.py:185-194` | 7 | hygiene | Builds the dispatch with `on_facts=on_facts`, then overwrites the private `dispatch._on_facts_callback`; redundant in-function import of `waiting_state_of` (module already imports from `shared_pool`). | fix | nothing |
| C13 | `shared_pool.py:872-880,967-968`; `evaluator.py:521-542`; `stages.py:1008-1017` | 2 | leave | Test-only modes/seams, tests prove behaviour that stays: private pool (every production caller passes `pool=`: `routers/golden_search.py:513`, `grid_search/dispatch.py:498`, `recency/dispatch.py:513`); `reordered_dispatch` (one test); `execute/approval/blob_store/roots/identity` injection (production passes none). | leave-with-reason | n/a |
| C14 | `shared_pool.py:104-115`, `research_runtime.py:208-221` | 7 | hygiene | `worker_profile` is called only with `count>=2`, so `WORKER_PROFILES[1]` and `default=1` are unreachable. Widths 3, 5-7 log "two lanes"/"four lanes" for a different lane count. | fix or leave | the copy-pin test (C16b) |
| C15 | `final_measures.py:39-40` | 7 | hygiene | `raise ValueError` for `years<=0` is unreachable from the sole caller (`_yearly` guards) and has no test. | leave | n/a |
| C16 | tests (lens 6, **findings, not cuts**) | 6 | | a. `test_worker.py:100-103` asserts `CHILD_ENV_FIXED == {literal}` (constant pin; env behaviour is proven at `:524-592`). b. `test_shared_pool.py:337-341` copy pin of log labels. c. `test_shared_pool.py:186-215` "duplicate residency is reported" asserts only `resident_paths >= 1`: the second owner lands on warm lane 0, so duplicates are never produced and the test cannot fail on the behaviour it names. d. `test_worker_pool.py:608,623` `assert point_hash is not None` plus an import used only for it. e. `test_worker.py:92-97` is a weak twin of `test_selection.py:112`. f. `test_shared_pool.py:242-249` is subsumed by `:573-583`. g. `test_worker_pool.py:1128-1316` (`slow`) is mostly a measurement harness feeding `logger.info`; the two real assertions are `open_connections<=24` and `requests>sql_lookups>0`. h. Flake risk: `test_worker_pool.py:243` `elapsed<0.30` (15 unloaded runs: median 0.168, max 0.224); `:847` `widths[4]<widths[1]*1.5` (slow). i. Gap: nothing outside Postgres exercises the stage runners or the legacy exam shape. | leave / owner | |
| C17 | `docs/architecture/engine-authority-map.md:51` | 7 | hygiene | Row says the runtime "runs the existing serial stage"; the shared pool has no row. | docs | |

## D. Considered and kept

- **Serial `WorkerDispatch`, pooled dispatch, shared pool, drain route:** all selected by `RESEARCH_EXECUTION_MODE` and `RESEARCH_WORKER_COUNT` (`config.py:356-367`, `compose.fleet.dev.yaml:104,229-234`), opt-in per runbook. Not dead.
- **`serial_dispatch` / `engine_executor`:** the default local path.
- **`_answer_admission` `root is None → admitted`:** reference trees outside the catalog (`path_policy.lake_root_for` docstring).
- **A failed run is evidence:** designed, and pinned by tests (C5 is only the outage subtype).
- **Randomized soaks** over the real `SharedWorkerPool` + evaluator + in-memory bridge, covering loss, jitter, cancel, a second owner and re-run. No hang, no duplicate completion, no retry past allowance, budgets back to zero, free re-run after cancel. This covers `_dispatch_and_record` replacement counting, straggler and generation fencing, and pool settlement.
- **Evidence stage runs single-point batches (pair audits, neighbors, stress):** pool width is unused there. An enhancement, not a bug.
- **`trade_net` re-derives the fee as `2c`:** guarded at runtime by `reconciles`, and a mismatch fails loud with text.
- **Engine request ISO dates** (`evaluator.py:305-307`): the engine's own request schema.
- **`_task_checkpoint(out, state)` unused `state`:** shared signature for three callers.
- **Broad `except Exception` in `stages._chance_records`:** recorded as visible failed components.
- **`warmup_session`'s doubling loop:** ends in `OverflowError`; inputs are validated.
- **No hardcoded session times, no second calendar, no tolerance-less `allclose`, no ISO/`DateTime` wire** in the eight files.

## E. Pointers

- `app/jobs/checkpoints.py`: `TaskSilence`, `PHASE_SILENCE_LIMIT_S`, `SIMULATION_CHECKPOINT_TARGET_S`, `CheckpointGate.last_accepted` have only test callers. Production silence is `worker.py:64-80` (45 s / 30 min), which disagrees with the module's 30 s simulation class. Owner: research-runtime wave / PRD #3034 (#3041). `GatedLease._seen` (`:265`) is an unbounded list.
- `app/research/benchmark.py:1332`: `WorkerDispatchContext` built without `decoded_cache_bytes`, so each lane keeps 2 GiB (8 lanes = the whole 16 GiB). The division lives only in `PooledOwnerDispatch.__init__`. Research wave.
- `grid_search/dispatch.py:248-296`, `recency/dispatch.py:275-325`, `benchmark.py:1206-1278`: import private `_expected_identity/_residency_fields/_task_checkpoint` from `golden_search.worker` and copy the child terminal-sink block three times. Research wave.
- `routers/golden_search.py` and `service.py` lines in B1: router/service unit.
- ADR 0077 §7 body ("interrupts the stage") reads through #3045; fine with the 2026-10-12 amendment.
- #2900: nothing in this unit.

## F. P0/P1 candidates

None confirmed. C7 would become P1-adjacent (a study that never advances, shown as "waiting for capacity") only if a live read of `/sys/fs/cgroup/memory.current` in the research container shows the page cache pinning it above 90 %. Repro for C1/C2/C3 (scratch only; the repro files are not committed):
```
cd PythonDataService && POLYGON_API_KEY=x DATA_PLANE_CONTROL_SECRET="" <venv>/python -I -c "import sys;sys.path.insert(0,'.');exec(open('<scratchpad>/p2/repro_drain.py').read())"
# same with repro_cancel_lock.py and measure_cancel.py
```
(`<scratchpad>` = `/private/tmp/claude-501/-Users-inkant-learn-ai--claude-worktrees-cleanup-3156-golden-search-4af8ab/25c84c08-edcf-45fc-bcf7-1fd6bbb35b70/scratchpad`)

## G. Coverage

**Read in full:** `pool.py`, `worker.py`, `shared_pool.py`, `evaluator.py`, `stages.py`, `final_test.py`, `final_measures.py`, `trade_net.py`, and all seven listed test files.

**Read for context:** `app/data_lake/admission.py`, `app/jobs/checkpoints.py` and `progress.py` (parts), `routers/golden_search.py:380-552`, `research_runtime.py:150-330`, `repository.reserve_evaluation`, `engine_backtest_service.py:736-880`, `run_gate.py`, `decoded_cache.py` head, `engine.py` synthetic exit, `config.py`, `compose.fleet.dev.yaml`, ADR 0077, the capacity runbook, and `issue-2930.md` / `issue-2933.md`.

**Test results** (host venv, one process, 7 files): **100 passed, 13 skipped in 18.9 s warm.**
- The cold first run of worker/loss/shared took 40.8 s.
- `slow`-marked tests that ran locally: `test_worker.py` real-child tests, 3.2-7.0 s each.
- `test_memory_pressure_pauses_*` takes 5.0 s because of the 5 s probe cache.
- All 13 skips are POSTGRES_URL-gated: 6 in `test_worker_pool.py` and 7 in `test_worker_loss_recovery.py`. `test_study_service.py` adds 60 more outside this unit's file list.

**Not covered:** the DB tier (stage lifecycles, real-child pool, SIGKILL mid-stage, admission measurement); live cgroup behaviour (C7); the legacy exam shape against real stored rows.

**Tool note:** `ruff check` on the unit is clean. An isolated `ARG` pass surfaced only trivial unused lambda args.

---

### Slice P3 — study service, store, views, proof and approval

# P3 report: study service, durable store, read views, proof and approval (#3156, pinned `cb667942`)

## Summary

- **Cuts (lens 2):** one confirmed dead repository function, one dead evidence helper, and five wire-only fields with no reader. All wire cuts need OpenAPI and TS regen.
- **P0/P1:** none found. One P2 repro ran: a decision-summary row reads "meets" when no neighbor was tested.
- **Tests:** 100 passed, 79 skipped (Postgres gate). There is no Postgres binary on this host and the brief forbids pointing at a container, so none of the DB-gated paths ran here.
- **Largest gap (P2):** the real `stages.run_qualification` and the real `approval.approve_study` are never run against each other. Each side is tested against a fake of the other.

## A. Feature table

All rows are reachable in checked-in config: the router is mounted in `app/main.py`, the Frontend service calls every route, and every service function has a production caller.

`[PG]` means the named test is Postgres-gated, so it skipped locally and runs only in the daily run (`ci.yml:272`, `daily-tests.yml:89-90`).

| feature | requirement | entry point | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|
| Capabilities | #2696 | `service.py::capabilities` | normal. No declaration returns `available:false` plus a reason. | implemented | `routers/test_golden_search_endpoints.py::test_capabilities_offer_the_ema_knobs_and_name_why_other_strategies_are_unavailable` |
| Defaults (plan, incumbent, lake run, exposure) | #2696, #2811, #2886 | `defaults`, `resolve_incumbent`, `lake_sessions`, `exposure_view` | An unreadable lake or default falls back to a registry point and a warning log with a label. No declaration gives `STRATEGY_UNAVAILABLE` (400). | implemented | `test_lake_sessions.py` (4, ran, pass); `test_study_service.py::test_defaults_offer_the_ready_default_qualification_as_the_incumbent` [PG] |
| Preflight | #2696 | `service.preflight` | refusals returned as data in a 200 | implemented | `test_study_service.py::test_preflight_answers_refusals_as_data_with_the_run_up_and_exposure` [PG] |
| Lock, idempotent | #2696 | `lock_study`, `_locked_by`, `repo.insert_study` | same key and plan return the same study. A different plan gives `IDEMPOTENCY_CONFLICT`. | implemented | `test_lock_with_the_same_key_returns_the_same_study_and_a_new_plan_conflicts` [PG] |
| Run research | #2811 | `lock_and_run`, `_run_at_lock_key` | a retry resolves to the same study and run. A lost job is re-authorized and the old token withdrawn. | implemented | `test_a_retried_run_research_resolves_to_the_same_study_and_run`, `test_a_run_whose_job_never_started_is_authorized_again_and_the_lost_token_is_withdrawn` [PG] |
| Commands: continue, finish, cancel, close, retain, revise | #2696, ADR 0074 | `run_command`, `_apply`, `_decide` | revision, idempotency and identity checks are real. An unreachable job store records nothing (503). | implemented | `test_a_repeated_command_returns_its_outcome_and_a_stale_or_reused_one_is_refused`, `test_cancelling_a_bound_stage_asks_its_worker_and_an_unreachable_job_store_records_nothing`, `test_revise_forks_a_linked_study_and_leaves_the_original_untouched`, `test_closing_a_stopped_stage_seals_its_attempt_against_a_worker_still_running` [PG] |
| Open the final test (exposure ledger, one-shot) | ADR 0074 §3 | `_open_exam`, `repo.lock_exposure`, `exposure_overlaps`, `finish_exam` | two concurrent studies on one symbol cannot both claim an unopened interval | implemented | `test_two_concurrent_final_tests_on_one_symbol_cannot_both_claim_an_unopened_interval`, `test_exposure_rows_survive_hiding_the_study` [PG] |
| Approval intent | #2696, #2930, #3015 | `_approve`, `_fit_to_approve` | note, parity ack, weakness ack and fit named are enforced. `FIT_ALREADY_CHOSEN` on retry. | implemented | `test_a_fit_that_failed_the_final_test_needs_the_written_acceptance_and_the_other_does_not`, `test_a_retried_approval_must_name_the_fit_it_started_with` [PG] |
| Jobs boundary: bind, run, reconcile | #3042, #3043 | `bind_dispatch`, `run_stage`, `study_job_binding` | bind and redelivery tested. **Release after a stamp refusal is untested.** `study_job_binding` is only monkeypatched. | bind and run implemented; release unverified | `test_an_unclaimed_stage_reads_queued_cannot_be_finished_and_cancel_then_finish_redispatches` [PG]. No test names `release_dispatch_binding` or `unbind_dispatch`. |
| Reads: detail, summaries, candidate, runs, curves, charts, hide | #2696, #2930 | `service.py` reads, `views.py` | `candidate_detail`, `evidence_view`, `exam_view`, `run_detail`, `knob_summary`, `passes_completed` have PR-time pure tests. `study_detail`, `study_summary`, `results_view`, `procedure_view`, `validation_view`, `qualification_view`, `receipt_summary`, `scope_view` have only DB-gated coverage. | implemented (daily-only for the study-level read model) | `test_views.py` (38, pass); `test_study_service.py::test_lifecycle_runs_from_lock_to_an_approved_golden_configuration`, `test_candidate_detail_and_the_evaluation_ledger_read_back_after_the_exam` [PG] |
| Durable store | ADR 0074 | `repository.py` | fenced writes; `reserve_evaluation` re-admits crashed rows without spending a retry | implemented | `test_a_crash_between_reservation_and_result_reruns_that_evaluation_without_new_budget`, `test_a_superseded_attempt_can_neither_record_nor_spend`, `test_a_proof_draw_lost_before_its_checkpoint_is_not_drawn_again_by_a_later_attempt` [PG] |
| Decision summary | #2811 | `decision_summary.py` | missing evidence reads "missing", never "meets" (one exception, see C1) | implemented | `test_decision_summary.py` (13, pass) |
| Evidence (months, neighborhoods, pair map, recommendation) | #2696 | `evidence.py` | hand-computed values, `atol=1e-9, rtol=0` | implemented | `test_evidence.py` (13, pass) |
| Proof (blobs, staging, two-root replay, re-proof) | #2696, ADR 0074 §5 | `proof.py` | typed `ProofMismatchError` codes | implemented. `build_proof`'s own `TRACE_MISMATCH` refusal (`proof.py:495-500`) has no test; only `reprove`'s does. | `test_proof.py` (34, pass) |
| Approval: publish in one transaction | ADR 0074 §5, ADR 0061 | `approval.py::approve_study`, `_publish` | all-or-nothing; an unknown commit outcome is looked up, not claimed | implemented against a fake caller. Not composed with the real stage (C2). | `test_approval.py` (19 [PG] + 1 pass) |

## B. Cut candidates (lens 2)

| id | path::symbol, lines | what | callers searched and hits | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| B1 | `app/research/golden_search/repository.py::insert_trial_fenced`, 468-481 | Fenced single-trial insert. `insert_trials_fenced` (plural) is the live one. | `git grep -n insert_trial_fenced` over the whole repo returns only the definition. Tests: none. No `getattr` or string dispatch. | nothing; `insert_trial` stays | no | confirmed |
| B2 | `app/research/golden_search/evidence.py::same_as`, 253-258 | Duplicate of the same-as grouping in `final_test.fits_to_test`. | No `same_as(` caller in `app/`, `scripts/`, Frontend, Backend or docs. The only hit is the test. `stages.py` imports `CandidateEvidence, CandidateKey, Neighborhood, StressResult, neighborhood, pair_map, recommendation`. | `tests/research/golden_search/test_evidence.py` line 20 (import) and `test_same_as_collapses_identical_points` 245-248. `final_test.py` keeps its own tested logic. | no | confirmed |
| B3 | wire echo `run_to_compare`: `views.py:125`; `schemas/golden_search.py:479`; `contracts/openapi/python-data-service.openapi.json` (blocks at 27650 and 27979); `Frontend/src/app/api/broker.types.ts:17874,17934` | `StudySummary` and `StudyDetail` field with no reader | `git grep -w run_to_compare` over Frontend: only `testing/fixtures.ts:1296`, two spec inputs, and the generated types. Backend: none. The server uses `row.run_to_compare` directly (`views.py:165`, `guidance.py:96`, `repository.py:305`), so those stay. | `testing/fixtures.ts:1296`; `golden-search-study.component.spec.ts:66,79` inputs | **yes**, plus Frontend types | probable. Waits on schemas/Frontend units. |
| B4 | wire echo `updated_at_ms`: `views.py:115`; `schemas/golden_search.py:469`; openapi (same blocks); `broker.types.ts:17886,17945` | Summary and detail field with no reader | `git grep -w updated_at_ms` in `components/golden-search` and `services`: only `fixtures.ts:1286` and `golden-search-decision-step.component.spec.ts:462` (test data). `StudyRow.updated_at_ms` and the DB column stay (fence). | those two test sites | **yes** | probable. Waits on schemas/Frontend units. |
| B5 | capability knob echo `default_low`, `default_high`, `neighbor_step`, `searchable_by_default`: `service.py:157,158,159,161`; `schemas/golden_search.py:229,230,231,233`; `broker.types.ts:14776,14778,14797,14803` | Four fields of `GoldenSearchCapabilityKnob` | `git grep -w` for each over `Frontend/src` (excluding generated types, specs, fixtures): no hits. `SearchKnob` itself uses them (planner, `test_declarations.py`), so the declaration keeps them. Only the wire echo goes. | `testing/fixtures.ts:68-74` | **yes** | probable. Waits on schemas/Frontend units. |
| B6 | `evidence.Neighborhood.one_sided`: `evidence.py:125,136,164`; `schemas/golden_search.py:927`; `broker.types.ts:16908` | Computed, stored and echoed. No reader anywhere (server or Frontend). | `git grep -w one_sided` over `app/research/golden_search`, Frontend components: definitions only (other hits are an unrelated clerk module). | `test_evidence.py:76,88` assert it; `fixtures.ts:816` | **yes** | probable. Stored JSON of existing studies carries the key and the wire model is `extra="forbid"`, so `compare_measures.neighborhood_view` (`:62-73`, other unit) must drop it too. Hand to lead as a coordinated cut. |

## C. Findings (not cuts)

| id | location | lens | sev | evidence | disposition | orphans |
|---|---|---|---|---|---|---|
| C1 | `decision_summary.py::_neighbors`, 103-120 | 3 | P2 | When every neighbor row is `invalid` or `outside_domain`, nothing is tested and no loss exists, so the row returns status `meets` with "No tested one-step neighbor loses money." Repro below. Needs both sides non-testable on every knob (e.g. fast=2 with slow=3 on `fast_period`), so it is rare. | fix. Return `missing` when zero neighbor rows are `tested`. | none |
| C2 | approval seam: `stages.py::run_qualification` (879-973) and `approval.py::approve_study` | 6 / 3 | P2 (fails closed) | The lifecycle harness replaces the approval with `FakeApproval` (`test_golden_search_endpoints.py`: `monkeypatch.setattr(stages, "default_approval", h.approval.binding)`; `Driver.run` passes `approval=self.approval.binding()`). `test_approval.py` drives `approve_study` with `_Caller` over a hand-inserted study row (`_study`, `'{}'::jsonb`) and a plain `UPDATE` as `on_commit`. No test calls `run_qualification` with the real binding. `git grep -E "approve_study\|run_qualification\|ApprovalRequest\b" tests` outside `test_approval.py`: none. Never composed: real `ApprovalRequest(...)` construction, `repo.save_checkpoint`, `evaluator.consume` into `consume_budget` for `approval:*` keys, `on_commit`'s `lock_current_attempt` + `update_study(_approved_changes)`. `FakeApprovalRequest` is a copy of the dataclass and would not notice a new required field. | fix. One DB test running a real study through the real approval. | none |
| C3 | `service.py::release_dispatch_binding` 1047-1056, `repository.py::unbind_dispatch` 357-375, router branches `routers/golden_search.py:350-372` | 6 | P2 | The #3042 recovery has no test. `git grep` over tests finds `study_job_binding` only as a monkeypatch. `unbind_dispatch` is typed `-> str` while `DispatchOutcome` exists. | fix: add a test | none |
| C4 | `proof.py::build_proof`, 495-500 | 3 | P2 | The central check (lake root must equal restored root) is never made to fire. `TRACE_MISMATCH` appears in tests only at `test_proof.py:461` (reprove). The comparison itself is plain dataclass equality. | fix: monkeypatch `_restored_trace` to return another root | none |
| C5 | `service.py::_approve`, 981-987; `weakness` list 981 | 7 / 3 | P2 | The operator-facing refusal prints raw codes and duplicates: "This evidence is weak (EXAM_DOES_NOT_MEET_RULES, CHANCE_WITHIN_NOISE, CHANCE_WITHIN_NOISE)". The duplicate codes also land in `decision.weakness` and `research.weakness`. `test_study_service.py:944` pins the duplicate. The Frontend gates first, so only a direct API client sees the message. | fix: dedupe codes; name the items in words | the `:944` and `:952` assertions |
| C6 | `service.py::_approve` 970-981 and `stages.py::run_qualification` 884-897 | 7 | hygiene | The same four statements (scope, `stored_final_chance`, `chance_weakness_items`, `research_weakness`) are written twice. Drift between the intent's `weakness` and the qualification's `research.weakness` would be silent. | fix: one helper | none |
| C7 | approval retry after `DEFAULT_CHANGED` | 1 / 3 | P2 | `expected_default_qualification_id` is always the frozen plan's incumbent (`golden-search-decision.ts:96-99`). Once any other approval moves the default, "Retry qualification" fails with the same code every time. The message says "review again", which only Revise does. `test_approval.py:495` passes `expected_default=prior` directly, which the page cannot. | owner-ruling or fix the copy. PRD says "require a new review", so the behavior is by design. | none |
| C8 | PR-time blindness | 3 / 6 | robustness | 79 of this unit's tests are Postgres-gated (60 in `test_study_service.py`, 19 in `test_approval.py`), plus `test_qualifications_repository.py`. The PR gate runs none of the lifecycle, the one-transaction publish, or the study-level read model (C-row 10 above). Policy-compliant. | leave-with-reason | none |
| C9 | `test_study_service.py:340-344` and `service.py:402-406` | 6 / 7 | hygiene | The test checks `derived != key` and a string prefix on three literals. The behavior is proven by `test_a_retried_run_research_resolves_to_the_same_study_and_run`. The docstring says no 1-200 character caller key can equal the derived key. That is false as written: `"run-research:"` + 64 hex is 77 characters, a legal key. Impact is self-conflict only. | delete test; soften comment | none |
| C10 | `tests/_helpers/golden_search.py::Landscape.sent` and `batches`, 143-155 | 6 | hygiene | Nothing reads `.sent` or `.batches` in any test (`git grep` of `\.sent\|\.batches` in `tests/`: only the helper). | delete both, keep `__call__` | none |
| C11 | `test_evidence.py:28` `_MINUTE_MS` | 7 | hygiene | defined, never referenced | delete | none |
| C12 | `repository.py::list_trials` (498), `list_exposures` (648) | 2 / 6 | hygiene | Test-only. `list_trials`: `test_study_service.py:730, 927, 1531`. `list_exposures`: `:848, 904`. Both prove ledger writes that stay. | leave-with-reason (test-only production helper). Could move into the test helper. | none |
| C13 | `test_study_service.py:1518-1548`, `app/research/benchmark.py::protocol_width_histogram` (892) and `round_widths` (~861) | 2 / 6 | hygiene | `protocol_width_histogram` has callers only in tests (`test_benchmark.py` x4 and this test). The script imports `run_baseline`, `summarize_report`, `DECODED_CACHE_DEFAULT_BYTES`, `RUNNABLE_EXECUTION_MODES` only. The test is a #3036 design-time "freeze". | owner-ruling. Waits on the benchmark owner. | `round_widths`; this test; `list_trials` loses three callers (two remain) |
| C14 | written, never read | 7 | robustness | `studies.finished_at_ms` / `StudyRow.finished_at_ms` (`models.py:119`, no reader; four test files construct it); `commands.command` and `created_at_ms` (read into `CommandRecord`, never used); `exposures.payload_json`, `state_at_reservation`, `claim`, `candidate_point_hash` (read only by `list_exposures`); trials rows other than `approval` events (read only by `list_trials`); `results.qualification.failure_code` (`stages.py:962`, `qualification_view` omits it); `results.exam.ledger_overlaps` and `outside_activity_overlaps` (stored, never rendered). Read-never-written: none found. | leave-with-reason: append-only audit ledgers (V11 triggers, ADR 0074 §3). Exception: `StudyRow.finished_at_ms` is a candidate for a later cut. | n/a |
| C15 | `GoldenSearchSummaryLink.target: str` (`schemas/golden_search.py` ~1483) and `GoldenSearchSummaryRow.key: str` | 5 | hygiene | The server emits a closed set (`trades/search/test/neighbor-tornado/cost-stress/without-best/decision`; seven row keys). A rename would not fail type-check. | fix: `Literal` | OpenAPI regen |
| C16 | `golden_search.py:1-…` in tests, `pytest.approx(0.3, abs=1e-12)` without `rel=0` | 4 | hygiene | `test_study_service.py:113,121,128`. Semantically abs-only, but the rule asks both explicit. | fix | none |
| C17 | `test_approval.py:62` | 6 | hygiene | imports `_copied_source_tree` (private) from another test module `tests/services/test_signal_program_admission.py` | fix: move to `_helpers` | none |
| C18 | wire fields kept for API clients | 5 | hygiene | `ledger_overlaps`, `outside_activity_overlaps`, `ValidationFold.failure_code`, `deploy.program_key`, `verdict.defined_folds/successful_folds`: no Frontend reader. `failure_code` is read server-side (`stages.py:472`) and by tests. | leave-with-reason | none |

### Repro for C1

```
cd PythonDataService
PYTHONPATH=. POLYGON_API_KEY=test python -c "
from app.research.golden_search.decision_summary import _neighbors
print(_neighbors({'key':'all_period','neighbors_audited':True,'neighbors':[{'knob':'gap','one_sided':False,'rows':[
 {'value':0.15,'status':'invalid','metrics':None,'reason':'x'},
 {'value':0.2,'status':'center','metrics':{'status':'completed','net_profit':100.0},'reason':None},
 {'value':0.25,'status':'outside_domain','metrics':None,'reason':None}]}]}))"
```

Output (ran): `{'key': 'neighbors', ..., 'status': 'meets', 'text': 'No tested one-step neighbor loses money.', ...}`

## D. Considered and kept

- `repository.StageClaimError` (378): raised at 389 and 391, never caught by type. It propagates to the job runner before the stage is claimed, which is intended (not marked failed).
- `service.study_job_binding` (460): sole caller is `services/research_runtime_client.py:275`, so it waits on the research-runtime unit.
- `service.release_dispatch_binding` and `repo.unbind_dispatch`: called from the router (`golden_search.py:356, 370`).
- `proof.reprove` and `ProofRecord.sha256()`: caller `qualification_service.py` (`:29`, `:281`).
- Approval acknowledgements: `acknowledge_missing_parity` is not a vacuous gate. The Frontend gates it on a checkbox (`golden-search-approval.component.ts:55,61`) and sends the literal only after that.
- `ApprovalFailureCode`: all 15 codes are raised. Every `FindingCode` in `evidence.py` is produced.
- Trial kind `pick` (`service.py:1003`) is read only by tests. ADR 0074 amendment says picks stay in the ledger.
- DB CHECK lists `candidate_locked`: the repository never writes it and a migration at `schema.py:800` handles legacy rows. Persistence is out of scope.
- `test_exposure_reads_the_window_keys_grid_and_walk_forward_requests_store` (`test_views.py:469`): a lint-as-test. It is the only PR-time guard of the exposure SQL's contract with other procedures' stored requests, because the DB-backed tests are daily-only.
- `test_a_stage_under_the_checkpoint_contract_renews_only_on_accepted_checkpoints`: not a twin of `tests/services/test_serial_execution_checkpoints.py` (engine boundaries there, stage boundaries here).
- Three tamper tests in `test_proof.py` (`BlobStore.get`, `materialize`, `reprove`): each guards that its own entry point goes through verified reads.
- Lean-and-mean B14: `golden_validation/service.py::assess` and `as_configuration` are gone (`git grep` finds no `def assess\b`). The `/golden-validations/{id}` and `.../applicability` routes are gone.
- PRD "affected bots" on the review screen: superseded by ADR 0074 §6 (approval starts no bot; a default change revokes nothing). Not a gap.
- `exam_view` / `candidate()` legacy single-candidate shape (`recorded_fits`): ADR 0074 §4 keeps it, tested at `test_views.py:176`.

## E. Pointers and waits-on

- `app/services/research_runtime_client.py:275` is the only production caller of `service.study_job_binding` (research-runtime unit, #3043/#3050).
- Production callers of repository functions sit in other units. If those are cut, these go with them:
  - `stages.py`: `update_study_fenced`, `finish_or_advance`, `save_checkpoint`, `claim_stage`, `finish_exam`, `insert_trials_fenced`, `count_procedure_evaluations`.
  - `evaluator.py`: `reserve_evaluation`, `start_evaluation`, `replace_generation`, `release_evaluation`, `complete_evaluation`, `consume_budget`, `get_evaluation`.
  - `metrics_of`: `evaluator.py`, `stages.py`, `search_charts.py`.
- `evidence.py` helpers have only `stages.py`, `concentration.py`, `final_test.py` and `run_curves.py` as other-unit callers.
- `app/research/benchmark.py::protocol_width_histogram` and `round_widths`: test-only (C13).
- `app/routers/golden_qualifications.py`: the only Frontend caller is `deploy-offer`. List, detail, defaults, revoke and reprove have no Frontend or script caller. They are routes, so not dead, but they are curl-only. No UI shows revocation, so the study page's "ready in Deploy" is a historical fact and Deploy's `deployOffer` is the live check. Qualification unit.
- `compare_measures.neighborhood_view` (`:62-73`) must drop `one_sided` together with B6.
- `PythonDataService/app/research/persistence/schema.py` (`candidate_locked` CHECK entry; trials, exposures and commands DDL): persistence owner. Pointer only.
- #2900 (the backfill runner duplicate): not in this unit.

## F. P0/P1 candidates

None.

I looked for four things and found none that is P0/P1: a qualified version approved on evidence that does not prove it, a stale "ready", a fail-open gate, and a swallowed exception.

- **Approval:** the proof binds the exact tuple and receipt. A reused checkpoint is re-verified for program, version, symbol, parameters, window, build digests and blob integrity. The saved run's tuple is checked in `_require_case`. CAS and the study update share one transaction. A superseded worker's `on_commit` rolls the publish back.
- **Exceptions:** the only broad catches (`service.py:233`, `:285`, `approval.py:210`) log with `exc_info`, and each returns a labelled fallback or a failed outcome. None passes silently.
- **Wire:** the schemas are `extra="forbid"`, so a view/schema mismatch fails loudly and cannot drop a field silently.
- **State:** every `StudyState` has guidance (`_STATE_GUIDANCE` vs `models.StudyState` checked in process, no difference).

The one reproduced defect is the P2 in C1.

## G. Coverage

- **Read in full:**
  - Production: `service.py`, `repository.py`, `views.py`, `evidence.py`, `proof.py`, `approval.py`, `decision_summary.py`.
  - Tests: `test_study_service.py`, `test_views.py`, `test_evidence.py`, `test_proof.py`, `test_approval.py`, `test_decision_summary.py`, `tests/_helpers/golden_search.py`, `tests/_helpers/golden_search_study.py`, `tests/research/conftest.py`.
  - Adjacent: `actions.py`, `models.py`, `routers/golden_search.py`.
- **Read in part:**
  - `qualifications.py` (`insert_qualification`, `set_default_cas`, `lock_default_pointer`).
  - `golden_validation/service.py::review`.
  - `stages.py` (60-140, 820-1191).
  - `schemas/golden_search.py` (summary, detail, exam, capability, neighborhood).
  - `guidance.py` (`study_guidance`, `research_weakness`, `weakness_items`).
  - `concentration.py` (status rule).
  - `compare_measures.py::neighborhood_view`.
  - `walk_forward_study/verdict.py`.
  - `benchmark.py` (histogram).
  - ADR 0074 §5 and amendments; ADR 0061 not re-read beyond what ADR 0074 amends; PRD #2696 "Review and promotion" and "Architecture".
- **Static analysis:**
  - `ruff` (project config): clean on all unit files.
  - `ruff --isolated` with `F,B,ARG,PLW,…`: only intended unused lambda and fake args, three broad `except Exception` with logging, no `F401/F841`.
  - AST caller scan over `app/`, `scripts/` and tests for every top-level name and method in the seven files.
  - Wire-field reader scan over 475 schema field names against Frontend `components/golden-search`, `golden-validation-workbench`, `services`.
- **Tests run** (host venv, `NUMBA_CACHE_DIR` in scratchpad):

| file | result |
|---|---|
| `test_study_service.py` | 1 passed, 60 skipped |
| `test_views.py` | 38 passed |
| `test_evidence.py` | 13 passed |
| `test_proof.py` | 34 passed |
| `test_approval.py` | 1 passed, 19 skipped |
| `test_decision_summary.py` | 13 passed |
| `test_lake_sessions.py` + `test_qualification_defaults.py` | 4 passed; plus 23 skipped in `test_qualifications_repository.py` (out of unit) |
| `tests/research/golden_search` + `test_golden_search_endpoints.py` | ran to completion, no failures (output truncated by `-x -q -q`; skips are Postgres-gated) |

- **Skips:** all 79 in the table skip with "POSTGRES_URL not configured" (the `conn` fixture in `tests/research/conftest.py`). The two that pass in those files are `test_the_run_a_lock_starts_is_keyed_apart_from_every_lock_key` and `test_approval_checkpoint_round_trips_through_its_dict_form`.
- **Not covered:**
  - No DB-backed path ran, so repository SQL, the one-transaction publish and the lifecycle are read-only verified.
  - `qualifications.py` beyond what approval calls, the Frontend components beyond reader scans, and `stages.py` run bodies belong to other units.
  - The router `jobs_router` release branches were read but not exercised.

Nothing in the repo was edited. Scratch files are under `/private/tmp/claude-501/-Users-inkant-learn-ai--claude-worktrees-cleanup-3156-golden-search-4af8ab/25c84c08-edcf-45fc-bcf7-1fd6bbb35b70/scratchpad/P3/`.

---

### Slice P4 — qualified versions, run curves, chart read models, concentration

# P4 reader report — #3156 Golden Search clean-up, pinned `cb667942`

The unit has nothing to cut except one never-passed parameter. Two P2 defects are confirmed, each with a repro I ran. There is no P0 or P1. The most important call for the lead is an owner ruling: five of the six qualification routes have no caller anywhere except tests, and revoke and re-proof are the only way to withdraw or refresh a version.

Worktree root: `/Users/inkant/learn-ai/.claude/worktrees/cleanup-3156-golden-search-4af8ab`. All paths below are relative to it unless absolute.

## Tests run

Command for each run: `cd <worktree>/PythonDataService && NUMBA_CACHE_DIR=<scratch> DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest <files> -q -p no:cacheprovider -rs`

| Run | Result |
|---|---|
| The unit's 10 test files (list below) | 160 passed, 19 skipped, 2.1 s |
| `test_study_service.py`, `routers/test_golden_search_endpoints.py`, `research/test_session_curves.py` (consumers of the unit's wrappers) | 32 passed, 75 skipped |
| `test_views.py`, `test_decision_summary.py`, `test_trade_net.py`, `test_qualification_defaults.py`, `test_approval.py`, `routers/test_golden_qualifications_endpoints.py` | 55 passed, 37 skipped |
| `ruff check` on all unit production and test files | clean |

The 10 unit files are `test_qualifications`, `test_qualifications_repository`, `test_run_curves`, `test_trade_charts`, `test_search_charts`, `test_plan_charts`, `test_fold_charts`, `test_concentration`, `tests/services/test_golden_qualification_admission.py` and `tests/broker/v2panel/test_golden_search_deploy.py`.

**Skips:**
- All 19 skips in the unit are `tests/research/golden_search/test_qualifications_repository.py`, gated by `tests/research/conftest.py:93` (`POSTGRES_URL not configured`).
- Every other skip is the same live-Postgres gate (`tests/research/conftest.py`, and the `_requires_ephemeral_db` fixture in `test_golden_qualifications_endpoints.py`).
- `.github/workflows/daily-tests.yml` `python-full` provides Postgres 16 and `POSTGRES_URL_IS_EPHEMERAL=1`; PR CI has none. So these paths are proven only by the daily run. I did not run them, per the brief.
- **Daily-only paths:** `insert_qualification`, `set_default_cas`, `revoke_qualification`, `append_event`, `read_qualification_evidence`, and the service wrappers `run_curves`, `curve_set`, `plan_step_charts`, `search_step_charts`, `test_over_time_charts`.

## A. Feature table

All rows are reachable in checked-in config. The qualification, charts and runs routers are mounted at `PythonDataService/app/main.py:1328`. Clerk reads of the three qualification tables are granted in `deploy/fleet/sql/provision-fleet-lake-catalog-role.sql:94`.

| # | Feature | Requirement | Entry point | Verdicts (normal / empty / failure) | Class | Evidence |
|---|---|---|---|---|---|---|
| 1 | Immutable qualified version, hashes derived at insert, refused if JSONB rewrites a value | ADR 0074 §5; #2696 | `qualifications.py::insert_qualification` ← `approval.py:672` | normal: row; failure: `ValueError` "does not survive storage" / symbol mismatch → `PUBLISH_REFUSED` | implemented (DB-only proof) | `test_qualifications_repository.py::test_insert_qualification_*` (skipped here, daily) |
| 2 | Status derivation ready / stale / revoked (revoked wins; latest re-proof digest vs running) | ADR 0074 §5 | `qualifications.py::qualification_status` | foreign events → `ValueError` | implemented | `test_qualifications.py::test_qualification_status_*` (ran) |
| 3 | Corpus coverage: registry point, else newest ready qualification of exact (program, version, stock, params hash); fail closed | ADR 0074 §6 | `qualifications.py::resolve_coverage` ← `signal_program_admission.py::resolve_admission_coverage` ← `bot_start_admission.py` | COVERED / UNCOVERED(stale\|revoked\|absent\|unverifiable) | implemented | `test_qualifications.py::test_resolve_coverage_*`, `test_golden_qualification_admission.py` (ran) |
| 4 | Seal pins the qualification id; the proof re-verifies only that id, for these bytes and this tuple | PRD #2696 | `build_start_program_seal` / `prove_running_program_build` / `_corpus_coverage` | other version, other bytes, other tuple, no re-read all → UNCOVERED | implemented | `test_golden_qualification_admission.py::test_pinned_seal_*`, `test_coverage_*` (ran) |
| 5 | Default pointer per (program, stock): compare-and-set, history, advisory lock | ADR 0074 §5 | `qualifications.py::set_default_cas`, `lock_default_pointer` | `DefaultChangedError`; first-pointer race; rolled-back approval leaves none | implemented (DB-only proof) | `test_qualifications_repository.py::test_set_default_cas_*` (skipped here, daily) |
| 6 | Revoke (clears pointer if default, idempotent per command id, serialized with approval) | ADR 0074 §5 | `qualification_service.py::revoke` ← `POST /{id}/revoke` | 404 / 409 already revoked / 409 key reuse | implemented, **no operator surface** (C3) | `test_qualification_defaults.py`, `test_golden_qualifications_endpoints.py::test_revok*` (skipped here, daily) |
| 7 | Re-proof: replay the stored proof under running code, append evidence, never revive | ADR 0074 §5; PRD line 154 | `qualification_service.py::reprove_qualification` ← `POST /{id}/reprove` | 409 revoked / proof mismatch / restart needed; 503 catalog down | implemented, **no operator surface** (C3) | `test_golden_qualifications_endpoints.py::test_reprove_*` (skipped here, daily) |
| 8 | Deploy prefers the stock's READY default; falls back to the registry point; unreadable store degrades and logs | ADR 0074 §6 | `qualification_service.py::ready_defaults` ← `panel_deploy._ready_golden_defaults`; `paper_deploy_service._qualified_configuration` | ready → default; stale\|revoked\|unverifiable → registry preset; read error → `{}` + warning | implemented (wording gap C10) | `test_golden_search_deploy.py` (ran) |
| 9 | "Use in Deploy" handoff | PRD #2696 | `GET /{id}/deploy-offer` ← `golden-search.service.ts:197` ← `alpaca-deploy-workflow.component.ts:1119` | answered whatever the status | implemented | `test_golden_qualifications_endpoints.py::test_ready_default_is_listed_offered_and_shown` (daily) |
| 10 | List / detail / defaults reads | ADR 0074 | `qualification_service.list_judged`, `get_judged`, `judged_defaults` | | implemented, **no consumer** (C3) | daily only |
| 11 | All runs: every run by what asked for it, sort, page, group counts | #2930 | `run_curves.py::runs_page` ← `service.runs` ← `GET /studies/{id}/runs` ← `golden-search-runs.component` | failed/pending sort last; unknown step → `ValueError` | implemented (defect C1) | `test_run_curves.py` (ran) |
| 12 | Named-run curves; "not recorded" never drawn flat | #2930 | `session_curves.curves_read` (shared) ← `service.run_curves` | missing keys listed under `not_recorded` | implemented | `test_session_curves.py` (ran); wrapper `test_study_service.py:1006` (daily) |
| 13 | Step curve sets (search, recent, fold tests, candidate): roles, cap with counts, incumbent fallback with notes | #2930 | `run_curves.py::curve_set_lines`, `curve_set_view` ← `service.curve_set` | `left_out`, `not_recorded`, `failed`, `notes` | implemented | `test_run_curves.py:175-346` (ran) |
| 14 | Plan charts (windows, search space, workload, minimums, coverage) | #2821 V1–V6 | `plan_charts.py::plan_charts` ← `service.plan_step_charts` | catalog unreadable → coverage "missing"+reason; unknown status → `ValueError` | implemented | `test_plan_charts.py` (ran) |
| 15 | Search charts (replay, best-so-far, moves, profiles, eligibility map) | #2821 V7–V10 | `search_charts.py::search_charts` ← `service.search_step_charts` | replay not rebuilding the winner → "not recorded" | implemented | `test_search_charts.py` (ran) |
| 16 | Test-over-time charts (folds, retention, drift, forward minimum) | #2821 V12–V17 | `fold_charts.py::fold_charts` ← `service.test_over_time_charts` | planned / in-progress / verdict | implemented | `test_fold_charts.py` (ran) |
| 17 | Trade charts (net, running net, bars held, Freedman–Diaconis histogram, RSI bands, entry-time cells) | #2821 V28–V32 | `trade_charts.py::trade_charts` ← `views.py:584` | not reconciled → missing+reason; cadence unread → hold not counted | implemented | `test_trade_charts.py` (ran); I fuzzed `histogram` over 20,000 inputs: count, cap, win/loss separation all hold |
| 18 | Concentration (best month, best 5% of trades, curve; informs only) | #2815; #2821 V20/V26/V27 | `concentration.py::concentration` ← `stages.py:47`; `concentration_curve` ← `views.py` | missing never a pass | implemented | `test_concentration.py` (ran) |

Not covered by P4: the final-decision curves (#2930 "Final decision" bullet) come from the candidate detail read in `views.py`, not from this unit's `CurveSet`. I classify that part unverified for P4 and leave it to the views reader.

## B. Cut candidates (lens 2)

| id | path::symbol and lines | what it is | callers searched | goes with it | OpenAPI regen? | confidence |
|---|---|---|---|---|---|---|
| B1 | `PythonDataService/app/research/golden_search/qualification_service.py:254` (parameter `blob_store: BlobStore \| None = None`), `:304` (`blob_store or default_blob_store()` → `default_blob_store()`), `:29` (drop `BlobStore` from the import) | An optional seam of `reprove_qualification` that no caller passes | `git grep -n "reprove_qualification"` → only the definition, `__all__`, and `routers/golden_qualifications.py:101`, which passes `qualification_id`, `idempotency_key`, `actor`. The endpoint tests point re-proof at their blobs with `monkeypatch.setattr(settings, "GOLDEN_SEARCH_PROOF_ROOT", ...)` (`test_golden_qualifications_endpoints.py:240`), never the kwarg. | Nothing else. `default_blob_store` stays used. | no | confirmed |

That is the whole cut for this unit. Everything else I examined has a production caller or is owner-ruling territory (C3).

### Symbol-by-symbol caller map

Established per the brief for `qualifications.py` and `qualification_service.py`. Every symbol has a non-test production caller, so there is **no test-only production symbol**. Test-only return values and fields are in C4.

- **`qualifications.py`**
  - `approval.py`: `DefaultChangedError`, `insert_qualification`, `get_qualification_by_study`, `get_qualification`, `events_for`, `get_default`, `set_default_cas`, `lock_default_pointer`, `qualification_status`.
  - `service.py:220-240`: `get_default`, `get_qualification`, `events_for`, `qualification_status`.
  - `qualification_service.py`: `QualificationEventConflictError`, `QualificationRow`, `QualificationEvent`, `GoldenDefault`, `list_qualifications`, `append_event`, `get_event_by_command`, `default_qualification_ids`, `QualificationNotFoundError`, `QualificationAlreadyRevokedError`, `revoke_qualification`, `read_default_qualifications`.
  - `signal_program_admission.py:28-34`: `Coverage`, `QualificationLookup`, `load_qualification_evidence`, `params_sha256`, `registry_point_matches`, `resolve_coverage`.
  - `bot_start_admission.py:22`: `Coverage`.
  - `schemas/golden_qualifications.py`: `QualificationEvent`; `paper_deploy_service.py`: `JudgedQualification`, `public_parameters`.
  - Used only inside the module (still production-reachable): `find_qualifications` (via `_read_evidence`), `read_qualification_evidence` (via `load_qualification_evidence`), `DefaultQualification`, `Revocation` (produced, see C4), `QualificationSubject`, `QualificationEvidence`, `_coverage_from`.
- **`qualification_service.py`**
  - `routers/golden_qualifications.py`: `get_judged`, `list_judged`, `judged_defaults`, `revoke`, `reprove_qualification`, `QualificationRefusal`.
  - `panel_deploy.py:145`: `ready_defaults`.
  - `schemas/golden_qualifications.py:12`: `JudgedDefault`, `JudgedQualification`, `StatusName`, `public_parameters`.
  - Internal: `judge`, `status_explanation` (via `JudgedQualification.explanation`), `running_artifact_digest_for`.

Consumers outside the package (pointers only):
- `bot_start_admission.py`: only the `Coverage` type and `CoverageResolver` (lines 22, 92-95, 663-686).
- `signal_program_admission.py`: the six symbols above.
- `panel_deploy.py`: `ready_defaults` only.
- `paper_deploy_service.py`: `JudgedQualification`, `public_parameters`.
- `schemas/broker_bots.py`: just the wire field `golden_qualification_id` (E).

## C. Finding rows

| id | location | lens | sev | evidence | disposition | what the fix orphans |
|---|---|---|---|---|---|---|
| C1 | `run_curves.py:137-139` (`_metric`) and `:166` (`run_view`) | 3 | P2 | A failed run is stored with `total_trades=0` (`Metrics.failed`, `selection.py:85-95`, written at `repository.py:979-989`). The All-runs page lists it as `0` trades and, sorted by Trades ascending, ranks it first as a measured result. The docstring says an unmeasured run sorts last. `search_charts._NO_NUMBERS` and `fold_charts` already treat a failed run's zeros as placeholders. `test_run_curves.py` builds its failed run with `total_trades=40`, so the real shape is untested. Repro below. | fix | nothing |
| C2 | `golden_search/service.py:194-197` (`_running_artifact_digest`), `:213` (default arg), `:240` (call) | 3/1 | P2 | `resolve_incumbent` judges the default's readiness with `signal_program_admission.running_artifact_digest`, the raw on-disk hash with no `imported_source_drift` check. Approval, `qualification_service` and admission all use `running_build_digests` (drift and anchor-checked, #2450). In a process whose disk moved after import (the documented "git pull then restart" window), the plan page labels a default "ready" that Deploy reports "unverifiable" and approval refuses with `RESTART_NEEDED`. An unreadable source raises uncaught (500) where `qualification_service` degrades. Fails closed at approval, but a research run's budget is spent first. Repro below. Pointer to the `service.py` reader. | fix: use `qualification_service.running_artifact_digest_for` (None → registry label) | `_running_artifact_digest` and its `running_digest=` injection become a one-liner |
| C3 | `app/routers/golden_qualifications.py:35-107` | 1/2 | owner ruling (P2 visibility) | Five of six routes (`GET /`, `GET /defaults`, `GET /{id}`, `POST /{id}/revoke`, `POST /{id}/reprove`) have no Frontend, Backend, script, runbook or workflow caller. Only `GET /{id}/deploy-offer` is consumed (`golden-search.service.ts:197`). `git grep` of `golden-qualifications`, `revoke_qualification` and `reprove` over Frontend, Backend, scripts and docs/runbooks finds tests only. So an owner can withdraw or refresh a qualified version only by hand-crafted HTTP with the control secret (`contracts/data-plane-control-surfaces.json`). After any strategy code change every version goes stale; nothing in a UI lists them, and no runbook says to re-prove. The only place that says "default is not ready" is the plan incumbent label (`service.py:241`). Revoke is a Start-gate control (money-adjacent), so **do not cut**: choose between a runbook line, a UI, or accepting curl. Routes here are not dead by AGENTS definition because they are routes; the lead may still treat them like the #2706 precedent. | owner-ruling | If ruled dead: the five handlers, `list_judged`, `get_judged`, `judged_defaults`, `revoke`, `reprove_qualification`, `QualificationRefusal`, `JudgedDefault`, response models in `schemas/golden_qualifications.py` except `GoldenQualificationDeployOffer` and `GoldenQualificationResearch`, `qualifications.list_qualifications`, `read_default_qualifications`, `revoke_qualification`, `Revocation`, `default_qualification_ids`, `get_event_by_command`, and the tests `test_qualification_defaults.py` and most of `test_golden_qualifications_endpoints.py`. This removes the only revocation path and needs OpenAPI regen. |
| C4 | `qualifications.py:608-613`, `:688`; `:476-597`; `:195-267` | 7 | hygiene | `Revocation.event` and `.cleared_default` are read only by tests (`test_qualification_defaults.py:94,95,140`); `qualification_service.revoke` (lines 209-228) discards the return. `set_default_cas`'s returned revision (approval.py:691, `qualifications.py:678`) and `insert_qualification`'s returned `QualificationRow` (approval.py:672) are unread in production. Test-only fields, not cuts. | leave | n/a |
| C5 | `test_qualifications_repository.py:59-133` vs `tests/_helpers/golden_qualification.py:37-125`; `QualificationRow` builders at `test_qualifications.py:47-75`, `test_golden_qualification_admission.py:112-140`, `test_golden_search_deploy.py:47-76`; event builders at `test_qualifications.py:78-92`, `test_golden_qualification_admission.py:143-155`; `PROGRAM`/`CONTRACT` repeated five times | 6/7 | hygiene | The repository test re-implements the helper's `accepted_golden_review` + `insert_study` + `seed_qualification`; three modules each hand-build the same record. No defect. | leave (refactor) | n/a |
| C6 | `Window = tuple[int, int]` at `activity.py:30`, `decision_summary.py:24`, `evaluator.py:84`, `run_curves.py:234`, `search_charts.py:247`; `_position` at `plan_charts.py:87` vs `search_charts.py:82`; `MAX_CURVES=200` at `run_curves.py:85`, `routers/grid_search.py:52` | 7 | hygiene | Duplicate aliases and constants; `_position` differs only in Decimal vs float inputs. | leave | n/a |
| C7 | `docs/architecture/engine-authority-map.md:79` | 7 | hygiene | Row says "research schema v11"; `PythonDataService/app/research/persistence/schema.py:57` is `SCHEMA_VERSION = 20`. A version number in prose that rots. | fix (drop the number) | n/a |
| C8 | `PythonDataService/app/schemas/broker_bots.py:387` `QualifiedDeployConfiguration.golden_qualification_id` | 2/7 | hygiene | Produced at `paper_deploy_service.py:459`; no Frontend or Backend reader (`git grep -n golden_qualification_id -- Frontend Backend` → only the generated type `broker.types.ts:25442`). Read only by `test_golden_search_deploy.py`. Test-only wire field. | leave, pointer to deploy ticket (needs OpenAPI regen to drop) | n/a |
| C9 | `test_search_charts.py:213-214`; `test_run_curves.py:168`; `test_golden_search_deploy.py:265` | 6/7 | hygiene | `_protocol_with_floor()` is a no-op wrapper of `protocol_from_request(plan_request("SPY"))` (the name promises a floor it does not set). One `# type: ignore[arg-type]`. A redundant `@pytest.mark.asyncio` under `asyncio_mode=auto` (no other test in the file uses it). | leave | n/a |
| C10 | `paper_deploy_service.py:466-480`; test `test_golden_search_deploy.py:101-108` | 1 | P2, owner ruling | When the default is stale, revoked or unverifiable, Deploy offers the registry preset whose explanation is only the generic "covered by the registered corpus" text. The "Golden Search default … is not offered" sentence is added only for a ready-but-undeployable default (#2841). Not a coverage fail-open (admission fails closed, tests prove it) and the offered preset is valid; the gap is that nothing on Deploy says the approved default went stale. Interacts with C3. | owner-ruling | n/a |
| C11 | `qualifications.py:287-309` `find_qualifications(program_version=None)` | 7 | hygiene | The `$4::text IS NULL` branch is exercised only by `test_find_qualifications_returns_the_exact_tuple_newest_first`; production always passes the version. | leave | n/a |
| C12 | `qualifications.py:920-926` and `qualification_service.py:353` (`__all__`) | 7 | hygiene | Re-exports of `QUALIFICATION_*` / `REGISTRY_POINT_COVERED` from `app.schemas.run_admission` are imported only by tests (`test_qualifications.py:19-26`, `test_qualifications_repository.py:22-26`, `test_golden_qualifications_endpoints.py:29`). Production importers use `app.schemas.run_admission`. | leave (test-import edit) | n/a |

### Repros I ran

**C1.**
- Command: from `PythonDataService`, `POLYGON_API_KEY=test DATA_PLANE_CONTROL_SECRET="" <venv>/python -c "…"` using `tests.research.golden_search.test_run_curves._run/_page`.
- Setup: two completed runs with 10 and 40 trades, plus a failed run built from `Metrics.failed("engine refused the window")` with `status="failed"` and numbers `None`.
- Output: `ascending by trades: [('failed','failed',0), ('ok-10','completed',10), ('ok-40','completed',40)]`, while by return the failed run is correctly last.
- Fix: in `_metric`, return `None` unless `record.status == "completed"`; in `run_view`, gate `total_trades` the same way.

**C2.**
- Setup: patch `signal_program_admission.record_imported_program_sources` and `imported_source_drift` (→ `"artifact"`) to simulate a pull after import.
- Output:
  - `golden_search.service._running_artifact_digest -> 9bbbb40793bf (no drift check)`
  - `running_build_digests -> RestartNeededError`
  - `qualification_service.running_artifact_digest_for -> None`
- Without the patch, in a bare process, `running_build_digests` raises `ValueError` (anchor not run) while the service helper still returns a digest.

## D. Considered and kept

- **`resolve_admission_coverage` calling `registry_point_matches` before `resolve_coverage` (`signal_program_admission.py:572` vs `qualifications.py:895`).** Deliberate: it avoids reading build digests for the registry point.
- **`wiring_digest` on qualifications and re-proofs is recorded but never gates readiness.** ADR 0074 §5 defines ready by artifact digest only, and wiring is warn-only by default (`SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED=False`, #1735).
- **Rollback after a re-proof reads "stale".** The latest re-proof's digest differs from the rolled-back bytes. It fails closed and is pinned by `test_qualification_status_follows_the_latest_reproof`.
- **`resolve_coverage` `except Exception`.** Logs with `exc_info` and returns UNCOVERED "cannot verify" per the PRD; not a silent handler.
- **`trade_charts._params` swallowing `ValidationError`, and `fold_charts._forward_minimum` swallowing `ValueError`.** Each is commented and returns "none to show". The study's code-identity rule means these reads are for old receipts.
- **`trade_charts` `ChartParamRef` multiplier branch.** Neither declared strategy uses it (both fixed 15 min), but `entry_rsi_gates` needs `ChartParamRef` for the gates, and the cadence read is generic.
- **`status_explanation` prints the ET date of an approval instant.** Backend-authored prose, allowed by temporal-rigor "Display".
- **`GoldenQualificationSummary.parameters` etc. and `DefaultQualification`.** Have producers and consumers; only the C3 consumers are missing.
- **`insert_study` / `accepted_golden_review` in `tests/_helpers/golden_qualification.py`.** Not unused: used by `seed_qualification` in the same file. `canonical_point`, `seed_qualification`, `PROGRAM` and `CONTRACT` are imported by `test_approval.py`, `test_qualification_defaults.py` and `test_golden_qualifications_endpoints.py`. No unused helper in the file.
- **Test-bar review of all unit tests.** Nothing fails the bar.
  - `test_a_dropped_research_connection_degrades…` vs `test_unreadable_defaults_degrade…` exercise different exception classes of `_RESEARCH_READ_ERRORS`.
  - Admission-layer stale/revoked tests sit above the resolver tests and assert seal and proof facts, so they are not twins.
- **Percentile tests.** `test_the_best_five_percent_is_one_in_twenty_rounded_up` pins float-noise-exact ceil.
- **Default pointer does not gate coverage.** By ADR §6: "a change of default revokes nothing".
- **`plan_charts.windows` / `search_charts` raise on a receipt lacking a floor while `fold_charts` degrades for the forward window only.** Documented fail-loud vs one commented degrade.
- **`GoldenSearchTestOverTimeCharts.in_progress` stays true for a stage that failed mid-way.** Its schema description says "running or stopped short of its verdict".
- **`QualifiedDeployConfiguration.golden_qualification_id` is a test-only wire field (C8).**

## E. Pointers

- `PythonDataService/app/research/golden_search/service.py:194-197,213,240`: C2; owner is the reader of `service.py`/planning.
- `PythonDataService/app/routers/golden_qualifications.py` and `PythonDataService/app/schemas/golden_qualifications.py`: C3; routers and schemas readers. Only `GoldenQualificationDeployOffer` and `GoldenQualificationResearch` have a consumer.
- `PythonDataService/app/schemas/broker_bots.py:387` (C8) and `paper_deploy_service.py:466-480` (C10): broker deploy ticket.
- `PythonDataService/app/schemas/golden_search.py:1767` `GoldenSearchRunDetail.daily_equity` (produced by `views.run_detail`): no Frontend component reads it (fixtures only). Another wire field with no reader; views/schema reader.
- `PythonDataService/app/routers/grid_search.py:52`: its own `MAX_CURVES = 200` (C6).
- `docs/architecture/engine-authority-map.md:79`: C7; docs reader.
- `PythonDataService/app/research/session_curves.py` is shared with Grid Search and Walk-Forward; reviewed, no finding.
- No overlap with known duplicate #2900. No `golden_validation/service.py::assess` or `as_configuration` in this unit.

## F. P0/P1 candidates

None. The two confirmed P2s (C1, C2) have repros above.

## G. Coverage

- **Read in full:**
  - Production: `qualifications.py`, `qualification_service.py`, `run_curves.py`, `trade_charts.py`, `search_charts.py`, `plan_charts.py`, `fold_charts.py`, `concentration.py`.
  - Tests: all 8 unit test files, `test_golden_qualification_admission.py`, `test_golden_search_deploy.py`, `tests/_helpers/golden_qualification.py`.
  - Consumers: `routers/golden_qualifications.py`, `schemas/golden_qualifications.py`, `session_curves.py` and `schemas/session_curves.py`, `research/persistence/db.py`, the qualification DDL in `research/persistence/schema.py` (lines 640-760).
- **Read in part:**
  - `signal_program_admission.py` (lines 15-60, 120-260, 289-500, 505-600, 596-660).
  - `bot_start_admission.py` (85-110, 640-700), `paper_deploy_service.py` (415-500), `panel_deploy.py` (120-190), `schemas/broker_bots.py` (375-432).
  - `golden_search/service.py` (200-270, 600-740), `approval.py` (168-275, 585-720), `proof.py` (`reprove`), the golden-search router chart handlers, `repository.py` (relevant functions).
  - ADR 0074 §5, §6, consequences; the full #2930 PRD; #2696 and #2821 by grep.
- **Not covered:** `trade_net.py`, `evidence.monthly_results`, `selection.py` beyond `Metrics`, `views.py`, `procedure_history.py` (other readers). I did not open the Frontend components beyond grepping for consumers. I did not run Postgres-gated tests (per brief); their verdicts in A rest on test names and code reading.
- **Scratch only (not in repo):** `…/scratchpad/P4/` (caller classification script, two repros, fuzz script).

---

### Slice P5 — the "could it be chance" family

# P5 report: the "could it be chance" family (#3156, Golden Search, Python, read-only)

**Verdict.** No P0/P1. One confirmed cut (`reading_basis`), a second tiny cut, and one probable cut gated on a DB probe. Five P2 defects with repros that ran. One cross-unit contradiction: the Frontend report and manual say the Sharpe carry-over rank "compares different return conventions", which my repro contradicts.

All scratch scripts are in `.../scratchpad/P5/`. They need a dummy `POLYGON_API_KEY=test` (imports `app.config`) and run from `PythonDataService` with the host venv, `-I`.

---

## A. Feature table

All features are unconditional in checked-in config; none sits behind a flag. "Daily-only" means the end-to-end test is Postgres-gated and skips on PR shards.

| feature | requirement | entry point | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|
| Session series admission and path statistics (net return, Sharpe, worst fall, objective) | #2932 "What a resampled path is scored on"; ADR 0074 d12 | `chance.py::session_series`, `path_*` | `SessionSeries`; six named string refusals; `<20` sessions refused; NaN, never inf | implemented | `test_chance.py` (engine parity `atol=1e-12`, hand paths, overflow); SB-001 `test_stationary_bootstrap_agrees_with_arch_fixture_sb_001` passes (`atol=0.008`, justified in the manifest). My `sharpe_conv.py`: `summarize()` Sharpe == `path_sharpe(session_series(daily_equity(curve)))`, diff 0.0. |
| Seed derivation | #2932 (no user seed) | `chance.py::seed_for` | SHA-256 first 8 bytes of canonical JSON | implemented | `test_seed_for_is_the_first_eight_bytes...`; seed inputs and value are stored in every reading's `basis.seed` |
| Development comparison (paired draw vs incumbent) | #3011 | `chance_compare.py::development_readings`, called from `stages._chance_records` | measured / same_settings / baseline / missing (named) / failed (logged and stored) | implemented | `test_chance_compare.py`; offline wiring harness `stage_wiring.py` (wire-valid for all 3 objectives); e2e daily-only |
| Development profitability and net-return range | #3011, #3026 | `chance_compare.py::_profitability_reading` | counts over all B; undefined kept apart | implemented | `test_chance_compare.py` (#3026 counterexample); e2e daily-only |
| Rehearsal ladder (1x-4x joint rules) | #3012 | `chance_rehearsal.py::rehearsal_reading` | rules mirror `judge_exam`; undefined counted as not passing | implemented | `test_chance_rehearsal.py`; floors rounded once |
| Session-close drawdown spread | #3013 | `chance_drawdown.py::drawdown_reading` | `concern` is `p95 > ceiling`; engine figure `None` is shown as "not recorded" | implemented | `test_chance_drawdown.py` |
| Final-interval comparison and profitability | #3014 | `stages._final_chance_for` -> `development_readings(sessions_label="final")` | same machinery, final sessions | implemented | `stage_wiring.py`; daily-only e2e |
| Carry-over rank | #3014, #3025 | `chance_final.py::carry_over_reading` | exact reproduction from the stored seed; every mismatch is a named missing | implemented in Python; presentation contradictory (F8) | `test_chance_final.py` (incl. `test_the_reproduction_repeats_a_real_ladders_own_1x_draw`) |
| Fit-to-fit, both directions | #3014, #3029 | `fit_to_fit_reading`, `resolved_fit_to_fit` | one shared draw; v1 legacy reshape | implemented, one wording defect (F1) | `test_chance_final.py`; daily-only e2e |
| Stored final-record validation | #3024 | `stored_final_chance`, `FinalChanceScope` | invalid -> named `invalid_record` missing, never repaired | implemented | `test_chance_final.py` (8 tests); offline: real stage output validates under `FinalChanceScope.for_protocol` |
| Approval policy and enrollment marker | #3015; ADR d12 | `chance_approval.py::enrollment_policy` (`planning.py:610`), `chance_weakness_items` (`service.py:976`, `stages.py:890`, `views.py:487`) | weak -> CHANCE_WITHIN_NOISE; missing/corrupt -> CHANCE_UNAVAILABLE for enrolled; legacy never gates on missing | implemented (gate logic sound) | `test_chance_approval.py`; `test_chance_approval_service.py` daily-only |
| Compare chart measures | #2821 | `compare_measures.py` (called from `views.py:404-407`) | failed/missing run -> `None`, never 0 | implemented | `test_compare_measures.py` |
| Backfill of pre-#2932 studies | #2933 | none | closed not planned, owner 2026-10-12 | not planned | `git grep -i backfill` over `app/research/golden_search`, `scripts`, routers: no chance backfill code exists |

On #2933: the code carries no backfill machinery. What remains is the "reads not measured, never backfilled" reader path that ADR 0074 d12 mandates (see D1).

---

## B. Cut candidates (lens 2)

| id | path::symbol and lines | what | callers searched | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| B1 | `chance.py::reading_basis` L89-100, plus the `(\`\`reading_basis\`\`)` mention in the L70 comment | function building a dict of the five constants | `git grep -n -w reading_basis` gives 4 hits: def, the L70 comment, `test_chance.py:27` (import), `test_chance.py:197` (call). No Frontend, Backend, scripts or docs hit. Stored readings build their own `basis` dicts in `_basis`, `_draw_ladder` and `_draw`. | `test_chance.py::test_reading_basis_names_the_method_and_the_thresholds_a_reading_was_judged_by` (L195-203) and the import. Values stay pinned by `test_chance_compare.py:182-183` and `test_chance_rehearsal.py:159`. Only the `CONVENTION` string literal loses its pin, so the lead may keep one 1-line assert. | no | confirmed |
| B2 | Three unused module loggers plus their `import logging`: `chance_drawdown.py:39,58`, `chance_final.py:50,87`, `chance_rehearsal.py:38,75` | `logger = logging.getLogger(__name__)` never called | `grep -n 'logger' chance_*.py`: only `chance_compare.py:497` uses its own; the three have zero uses and no `import ... as` re-export | nothing | no | confirmed |
| B3 | v1 fit-to-fit compat: `chance_final.py::SCHEMA_FIT_TO_FIT_V1` L93-94; v1 branch of `resolved_fit_to_fit` L434-439; v1 branch of `normalized_exam_fit_to_fit` L461-476; `_v1_fit_to_fit_direction` L505-539 | reshapes a #3014 one-direction record | callers: `views.py:488,513` (stored data only). The shape was written only between #3023 (2026-10-10 10:06 CDT) and #3032 (17:45 CDT), same day. | `test_chance_final.py:493-576`; `test_chance_evidence_service.py:298-345` (daily-only). The "unsupported schema" branch stays. | no | probable. Needs a DB probe: no study with `results_json->'exam'->'chance'->'fit_to_fit'->'basis'->>'schema_version' = 'golden-search-chance-fit-to-fit/v1'`. Owner's 2026-10-12 stance on #2933 is "delete old studies, carry no compat". |

---

## C. Findings (not cuts)

| id | location | lens | sev | evidence | disposition |
|---|---|---|---|---|---|
| F1 | `chance_final.py:327` (`admit_run(side["metrics"], side["detail"], window)`) | 1/3 wording | P2 | Missing `sessions_label="final"`. Repro `ftf_label.py`: "One fit's final run could not be read ... Recorded reason: **The development run failed**, so there are no sessions to resample. (engine refused)". `admit_run`'s own docstring says `sessions_label` exists so a final refusal never calls itself development. The test asserts only status and code. | fix (pass `sessions_label="final"`; add a `"final" in reason` assert) |
| F2 | `chance_approval.py:115-119` `_share_item` | 1 wording | P2 | Any share below clear is called "inside the resampling model's noise", including a clearly adverse one. Repro: comparison 100 of 10,000, profitability 50 of 10,000 -> both read "inside the resampling model's noise ... (1.0%)". PRD story 14 distinguishes "clear the other way". Gating is unchanged (acknowledgement needed either way). | fix text: branch on `worse`/`negative` counts reaching clear |
| F3 | `chance_final.py:276,283` `rank = below / finite_count if finite_count else 0.0` | 3 | P2 | Repro `flat_carry.py`: a flat development run on Sharpe gives `status: measured`, `rank: 0.0`, `finite: 0`. The sentence and the approval gate are right (`chance_approval.py:186` -> unavailable). The Frontend table row `golden-search-chance-final-charts.ts:250` prints "0 of 0 defined paths strictly below (0%)". | leave-with-reason, or make `rank` nullable (schema change -> OpenAPI regen) |
| F4 | `chance_rehearsal.py:155-187` `_guidance`; `rungs[*].undefined` | 3 | P2 | Same repro: Sharpe objective, all paths undefined -> "the limit is the trade-count rule (0%) and the objective comparison ... (0%)"; 10,000/10,000 undefined. `rung.undefined` is stored and served but no Frontend file reads it (rehearsal charts use `shares` only). Conservative, and also what the real exam does (`not_available` is not a pass). | fix wording (mention undefined) or leave |
| F5 | `chance_compare.py:649-669`, `chance_rehearsal.py:363-372`, `chance_drawdown.py:184-193` (`stored_chance`, `stored_rehearsal`, `stored_drawdown`) | 5 | P2 | They accept any record with a known status, no shape check. Repro: `{'status':'measured','label':'mixed'}` passes through, then `GoldenSearchChance.model_validate` raises (9 errors), i.e. the Compare response fails validation. The final side has #3024/#3029 validation; the development side does not. `test_chance_compare.py:438-442` pins the pass-through and its name ("says why it is unavailable") is wrong for the profitability half. | fix (validate against the wire model like `_serves_on_the_wire`) or leave: records are written only by this code |
| F6 | `chance_final.py:353,356` | 4/7 | P2/hygiene | `fit_to_fit_reading` writes literals `"stationary-bootstrap/v1"` and `0.95` instead of `CONVENTION` / `CLEAR`, and its basis omits `chunk_replications`, which every other basis stores (ADR d12 requires the draw convention). Not redrawn anywhere, so harmless today; a constant change would leave v2 records stating v1/0.95. | fix (3 lines) |
| F7 | `chance_drawdown.py:93-95` | 1 wording | P2/hygiene | `{ceiling:.0%}`; the ceiling is free in (0,1] (`schemas/golden_search.py:82`; `exam_rules.py` uses `.2%`). A 12.5% ceiling prints "12%". | fix to `.1%`/`.2%` |
| F8 | Cross-unit: `Frontend/.../report/golden-search-report.component.ts:45-49`, `.html:49`; `manual/golden-search-manual.component.html:43` | 1/4 | P2 | `incompatibleRank` hides every measured v1 Sharpe carry-over rank, claiming "recorded Sharpe and the rehearsal Sharpe use different return conventions ... tracked repair #3025". Evidence against: (a) `stats["sharpe_ratio"]` is `_sharpe(_daily_returns(_resample_to_daily(equity)))`, zero rf, the same chain as `path_sharpe` (`engine_backtest_service.py:1363`, `engine_adapter.py:122`); (b) my `sharpe_conv.py`: diff 0.0 on Sharpe, net_profit_pct, net_profit; (c) PRD #2932 prototype: all 25 detail runs matched, max diff 4.4e-16; (d) #3025 shipped (`47981ec8`) as horizon/capital matching, not a Sharpe change. The current schema string is `.../carry-over/v1`, so the guard is always true for Sharpe studies, and the decision step (`showCarryOver` default true) shows the same rank. The report and decision pages contradict each other. | fix in the Frontend unit: drop the guard and banner. Python side: optionally add a `recorded` vs own-series objective check in `carry_over_reading` so the claim becomes a receipt. |
| F9 | `test_chance_evidence_service.py:290` | 6 | P2 | The assertion reduces to `better + tied + undefined == B`, i.e. `worse == 0`. It passes only because `fake_detail`'s linear ramp makes the Sharpe order degenerate: I simulated `fit_to_fit_reading` on `fake_detail` and got 10,000/0/0/0 in one direction. The correct invariant is `test_chance_final.py:486`. Daily-only, so it will not surface until the fixture changes. | fix the assertion or delete as a duplicate |
| F10 | `chance_compare.py:139,203`; `DERIVED_RETURN_OVERFLOW` L95,216 | 6 | P2 | `too_few_sessions` is coupled to the message prefix `"The run covers"`; no test asserts the `admit_run` code for either `too_few_sessions` or `derived_return_overflow` (`git grep` in tests: none). Both work by repro (`admit.py`). A reworded `chance.py` message would silently turn into `unreadable_series`. `chance.py:148` also leaks an uncatalogued numpy overflow `RuntimeWarning`. | add two admit-level asserts |
| F11 | `test_chance_evidence_service.py`, `test_chance_approval_service.py` (16 tests) | 3/6 | known pattern | All stage-wiring tests are Postgres-gated; they skip on PR shards by design (#3123) and run daily (`daily-tests.yml:90`). My offline harness (`stage_wiring.py`, fake ctx/evaluator, `_development_detail_for` patched) exercised `stages._chance_records` and `_final_chance_for`, then `stored_final_chance`, `chance_weakness_items`, `fit_to_fit_reading` and the wire models, all valid at this SHA. | leave; note that PR CI does not cover the wiring |
| F12 | seam: `evaluator.project_response(...).detail` -> `admit_run` | 1/6 | unverified | No test composes a real `project_response` detail (or `summarize` output) with `session_series`/`admit_run`. FakeEngine detail is hand-built. Real-engine equivalence rests on the PRD prototype plus my scratch repro, not on the suite. | add one parity test (`summarize` + `daily_equity` + `path_sharpe`; `sharpe_conv.py` is the shape) |
| F13 | duplicates | 7 | hygiene | `chance_compare._window_sessions` (L178-180) vs `grid_search.service.window_dates` (used by `chance_rehearsal.final_session_count`), equivalent under ET-midnight windows (`protocol.py:676`); inline quantile `chance_final.py:278` vs `chance_rehearsal._percentiles` L110-115; private `_missing` / `_pair_chunk` / `_at_least` imported across modules; `_Cancel` alias x4 | leave or consolidate |
| F14 | `chance_rehearsal.py:284-337` `objective_percentiles_1x` | 2/3 | note | Stored, served, in OpenAPI and TS types, but no production reader. Carry-over reproduces from the seed instead and never cross-checks the stored percentiles. Not a delete-only cut: `_Wire` is `extra="forbid"`, so stored v1 records would break on removal. It is the oracle of `test_the_reproduction_repeats_a_real_ladders_own_1x_draw`. | owner-ruling: use it as a runtime integrity check, or schema-version the removal |

Test-bar notes (lens 6, not cuts):
- `test_chance_drawdown.py::test_quantiles_interpolate_linearly_and_carry_their_finite_count` (L81-85) is a weaker near-duplicate of the hand-computed percentile test.
- `test_a_deterministic_alternating_series_states_its_actual_outcomes_not_a_promise` (L116-123) is misnamed (its series is not alternating) and asserts only `0 < beyond < 10_000`.
- The rest pass the bar. Every float comparison in the unit's tests has explicit `atol`/`abs`, with rtol dropped or 0.

---

## D. Considered and kept

1. **"Not measured" / `policy is None` legacy paths** (`NOT_MEASURED` in compare/rehearsal/drawdown, `_NOT_MEASURED_BY_COMPONENT`, `stored_*` fallthroughs, `chance_weakness_items(policy=None)`): ADR 0074 d12 mandates them, and they are reachable by any study locked before 2026-10-10. They become cuttable only after the owner deletes those studies (#2933 comment 2026-10-12), and the cut is not delete-only.
2. `INFORMATIONAL_COMPONENTS` / `"compare_drawdown"`: text in the immutable enrollment marker, not dead.
3. `_KNOWN_BY_COMPONENT["comparison"]` including `same_settings`/`baseline`: not produced by exam wiring, but harmlessly accepts stored records.
4. `failed_readings`, `failed_component`, `failed_rehearsal`: all called from `stages.py`.
5. Redraw not cross-checked against stored draws: numpy is pinned `==2.2.6` (`requirements-heavy.txt:4`) and a literal seeded-draw test fails on stream change.
6. `rank` uses strict-below and ties are not below: documented, tested (`test_a_tie_at_the_recorded_value_is_not_strictly_below`).
7. `ceiling` "within" vs "beyond": equality is within, tested at `count_falls`.
8. `weakness` lists `CHANCE_WITHIN_NOISE` twice (comparison and profitability): cosmetic.
9. Golden fixtures: SB-001 is `active`, `validated_by` resolves and passes. Not touched.

---

## E. Pointers

- Frontend report/manual carry-over caveat: F8 (report/manual unit, #3031 presentation layer).
- `tests/_helpers/golden_search_study.py::fake_detail`: linear ramp; Sharpe order degenerate (F9); helper unit.
- `stages.py::_drawdown_for` (L522-536): redundant `except Exception`, since `drawdown_reading` already catches (stages unit).
- Symbols whose only callers are in other units (go if stage/view wiring is cut): `development_readings`, `incumbent_readings`, `failed_readings`, `failed_component`, `rehearsal_reading`, `failed_rehearsal`, `drawdown_reading`, `carry_over_reading`, `fit_to_fit_reading` (all `stages.py`); `stored_chance`, `stored_rehearsal`, `stored_drawdown`, `normalized_exam_fit_to_fit`, `resolved_fit_to_fit`, the compare_measures view helpers (`views.py`); `enrollment_policy` (`planning.py:610`); `chance_weakness_items`, `stored_final_chance`, `FinalChanceScope` (`service.py`, `stages.py`, `views.py`).
- #2900 (trade-bar backfill runner duplicate): not touched by this unit.

---

## F. P0/P1 candidates

None. Repros that ran, all P2: F1 (`ftf_label.py`), F2 (inline `chance_weakness_items`), F3/F4 (`flat_carry.py`), F5 (inline `stored_chance` + `model_validate`), F8 (`sharpe_conv.py`), F9 (inline sim), F10 (`admit.py`).

---

## G. Coverage

- **Read in full:** `chance.py`, `chance_compare.py`, `chance_final.py`, `chance_rehearsal.py`, `chance_drawdown.py`, `chance_approval.py`, `compare_measures.py`; all nine test files.
- **Read in part:** `stages.py` 480-960, `views.py` 380-540, `service.py` 930-1010, `schemas/golden_search.py` 977-1500, `exam_rules.py`, `evaluator.detail_payload`, `sweep/daily_equity.py`, `engine statistics.summarize` and its engine call chain, `monte_carlo/stationary.py` head, ADR 0074 d11-12, `issue-2932.md` body plus all five comments, `issue-2933.md`; Frontend final panel, charts, report and manual for the carry-over paths.
- **Tests run** (host venv, `NUMBA_CACHE_DIR` in scratchpad):
  - the nine unit files: **150 passed, 16 skipped**. The skips are all `POSTGRES_URL not configured`: 8 in `test_chance_evidence_service.py`, 8 in `test_chance_approval_service.py`. Not run, per the brief.
  - `test_stationary.py`: 17 passed.
  - `test_views.py` + `test_evaluator.py`: 68 passed.
  - ruff `--isolated` F401/F841/F811/F821/B/SIM/PLW/RUF100 on the seven files: clean.
- **Not covered:** real-engine runs; Postgres-gated tests; Frontend panels beyond the carry-over and rehearsal paths; `golden_validation/`; fixtures under `tests/fixtures/golden` (sacred, only manifest/validated_by checked).

---

### Slice P6 — routes, wire schemas, Golden Validation, route boundary

# P6 report: Golden Search routes, wire schemas, Golden Validation, route boundary (pinned `cb667942`, read-only)

Nothing in the repo was written; probe scripts are under `scratchpad/P6/`. No P0 or P1 found. Two P2 defects have repros (C1, C2), and C2 is a P1 by the letter of the owner's "never key evidence on a subject-supplied value" rule if you choose to promote it. The 3 route cuts and the `daily_equity` wire field in section B are confirmed by caller search, not by running a cut.

## Route inventory (27 operations, contract matches the live app)

`python scripts/export_openapi_contract.py --check` exits 0, so the committed OpenAPI contract matches the live app. The kill-list items are verified gone:
- `GET /golden-validations/{id}` and `/applicability` no longer exist.
- `assess`, `as_configuration` and `GoldenValidationApplicabilityRequest` have no hits anywhere in `app/`, the contract, or the generated TS.

| # | Method path | Callers (non-test) |
|---|---|---|
| 1 | GET `/api/research/golden-search/capabilities` | Frontend `golden-search-page.component.ts:69` |
| 2 | GET `/defaults` | `golden-search-plan-form.component.ts:293,323` |
| 3 | POST `/preflight` | `plan-form.component.ts:379` |
| 4 | POST `/studies` | `plan-form.component.ts:232` |
| 5 | GET `/studies` | `golden-search-history.component.ts:53` |
| 6 | GET `/studies/{id}` | `golden-search-page.component.ts:85`, `golden-search-study.component.ts:210` |
| 7 | POST `/studies/{id}/commands` | `plan-form.component.ts:247,249`, `study.component.ts:318` |
| 8 | DELETE `/studies/{id}` | `study.component.ts:275` |
| 9 | GET `/studies/{id}/runs` | `golden-search-runs.component.ts:91` |
| 10 | GET `/studies/{id}/runs/curves` | `runs.component.ts:106` |
| 11 | GET `/studies/{id}/charts/run-curves` | `curve-set.component.ts:93` |
| 12 | GET `/studies/{id}/charts/plan` | `plan-summary.component.ts:68` |
| 13 | GET `/studies/{id}/charts/search` | `search-step.component.ts:40` |
| 14 | GET `/studies/{id}/charts/test-over-time` | `test-step.component.ts:52` |
| 15 | GET `/studies/{id}/candidates/{key}` | `compare-step.component.ts:107`, `final-charts.component.ts:107` |
| 16 | POST `/api/jobs-internal/golden-search` | `Backend/Jobs/JobsApi.cs:49`, reached through `JobsService.startJob(dispatch.job_type)`; the API process forwards it to the runtime via `research_runtime_client.py:76` |
| 17 | POST `/api/jobs-internal/pool/drain` | operator runbook `docs/runbooks/research-runtime-capacity.md:45` |
| 18 | GET `/api/research/golden-qualifications/{id}/deploy-offer` | `golden-search.service.ts:deployOffer`, then `alpaca-deploy-workflow.component.ts:1119` |
| 19 | GET `/api/research/golden-qualifications` | none |
| 20 | GET `/golden-qualifications/defaults` | none |
| 21 | GET `/golden-qualifications/{id}` | none |
| 22 | POST `/golden-qualifications/{id}/revoke` | none |
| 23 | POST `/golden-qualifications/{id}/reprove` | none |
| 24 | POST `/api/research/golden-validations` | `golden-validation-workbench.component.ts:145` |
| 25 | GET `/golden-validations` | `workbench.component.ts:68`, `engine-lab-run-history.component.ts:80` |
| 26 | POST `/golden-validations/{id}/reviews` | `workbench.component.ts:199` |
| 27 | GET `/api/golden-fixtures` | Frontend `/golden-fixtures` page (menu `app-menu.ts:69`) |

Routes 19–23 are the only ones with no Frontend, Backend, script, CI or runbook caller. Routes 1–15 and 18 are called by the matching `GoldenSearchService` methods, and each of those methods has a production component caller.

`golden_fixtures.py` serves the math golden-fixture catalog: `tests/fixtures/golden/manifest.json` plus `artifacts/fixture-validation/latest.json`. It is not Golden Search; the name only collides. It is alive (page, menu, Dockerfile bake, compose mount) and returned 200 with the manifest in my probe.

## A. Feature table

| feature | requirement | entry point | reachable in checked-in config | verdicts (normal / empty / failure) | class | evidence |
|---|---|---|---|---|---|---|
| Capabilities | #2696, ADR 0074 §7 | `routers/golden_search.py::get_capabilities:94` | yes (data-plane core role) | EMA available; others `available=false` plus reason / no failure path | implemented | `test_capabilities_offer_the_ema_knobs…` PASS (no DB) |
| Plan defaults | #2696, #2811, #2821 | `get_defaults:100` | yes | 200 plan / 400 `{code,field}` refusal / DB unreadable falls back to registry point, labelled | implemented | PASS: `test_refused_defaults_are_a_400…`, `test_defaults_refuse_a_range…`, `…a_month_count_below_one…`; happy path DB-gated `test_defaults_lay_the_plan_out_inside_a_picked_range…` (skipped) |
| Preflight | #2696 "refusals are data" | `preflight_plan:130` | yes | 200 with refusals / 422 malformed / **500 on huge month counts (C1)** | implemented, defect C1 | PASS: `test_preflight_returns_every_problem…`, `…names_the_instant…`, `test_a_body_that_is_not_a_plan_at_all_is_a_422[*]` |
| Lock study / Run research | #2696, #2811 | `lock_study:143` | yes | 201 / 400 with every refusal / 409 key reuse / replay returns same study | implemented (DB-gated) | `test_a_study_runs_from_lock_to_an_approved…`, `test_run_research_locks…`, `test_a_lock_key_reused…`, `test_a_refused_lock_is_a_400…` (skipped) |
| History (list, read, hide) | #2696 | `list_studies:162`, `get_study:174`, `hide_study:204` | yes | 200 / 404 `NOT_FOUND` / 409 `STUDY_RUNNING` | implemented (DB-gated) | `test_a_running_study_cannot_be_hidden…`, `test_reads_of_a_missing_study…` (skipped) |
| Lifecycle commands (9) | #2696, ADR 0074 §1 | `run_command:183` | 8 of 9 have a UI producer; **`close` has none** | 200 / 409 `STALE_REVISION` (carries study) / 409 `COMMAND_NOT_PERMITTED` / 400 / 503 | partial (`close` API-only) | `test_a_stale_revision_is_a_409…`, `test_one_idempotency_key_replays…`, `test_cancel_with_the_job_store_unreachable…` (skipped) |
| Runs ledger, curves, curve sets | #2930 | `list_runs:216`, `get_run_curves:242`, `get_curve_set:255` | yes | rows / `not_recorded` named, never drawn flat / 400 `RUNS_QUERY_INVALID` | implemented (DB-gated) | covered inside `test_a_study_runs_from_lock_to_an_approved…` |
| Step charts (plan, search, test-over-time) | #2821 | `:271,281,291` | yes | measured / planned / missing blocks | implemented (DB-gated) | same test |
| Candidate detail | #2696, #2930 | `get_candidate:303` | yes | 200 / 404 `CANDIDATE_UNAVAILABLE` / 422 unknown key | implemented (DB-gated) | same test, `test_reads_of_a_missing_study_or_candidate…` |
| Jobs boundary | #2696, #3041–#3043 | `start_golden_search_job:318` | local mode default; forward mode opt-in (`RESEARCH_EXECUTION_MODE=research` plus compose `research` profile) | 202 / 409 `STAGE_TOKEN_MISMATCH`, `NOTHING_PENDING` / 404 / 503 stall / 502 on forward failure | implemented (local, DB-gated); forward path unverified here | `test_the_jobs_entry_runs_only_the_stage_a_command_authorized` (skipped); `tests/research/test_research_runtime.py` (not run, other unit) |
| Pool drain | #3048 | `drain_research_worker_pool:525` | yes (route-level control secret) | `{drained:true,…}` / `{drained:false,reason}` | implemented | `tests/research/golden_search/test_shared_pool.py:392,594` (not run) |
| Deploy offer | ADR 0074 §5–6 | `get_golden_qualification_deploy_offer:74` | yes | `status` ready / stale / revoked / unverifiable; Deploy applies only `ready` and refuses an answer naming a different id | implemented (DB-gated) | `test_ready_default_is_listed_offered_and_shown`, `…offered_as_stale`, `…unknown_qualification_is_404`, `…never_reads_ready` (skipped) |
| Qualification list / defaults / detail | ADR 0074 §5 | `golden_qualifications.py:40,56,69` | route reachable, no caller | 200 / 404 | API-only | DB-gated tests only |
| Revoke / re-proof | PRD #2696 ("Explicit revocation is separate"), ADR 0074 §5 | `golden_qualifications.py:80,96` | route reachable, no caller, no runbook | 200 / 404 / 409 `QUALIFICATION_REVOKED`, `IDEMPOTENCY_CONFLICT`, `PROOF_MISMATCH`, `RESTART_NEEDED` / 503 `CATALOG_UNAVAILABLE` | implemented, API-only | `test_revoke_refusals`, `test_reprove_*` (skipped) |
| Golden Validation designate / list / review | ADR 0061 | `routers/golden_validation.py:45,61,76` | yes | 201 / 404 / 409 `STALE_…`, `COMMAND_CONFLICT`, `INELIGIBLE` | implemented | `test_applicability.py` 7 PASS (pure); endpoint and service tests DB-gated (skipped) |
| Fixture catalog | D-010 | `golden_fixtures.py:109` | yes | 200 / 503 manifest missing or unreadable / `validation:null` when `latest.json` absent or unreadable | implemented (no test in repo) | my probe: GET → 200 |
| Config keys | — | `config.py:418`, `:366-386` | — | `GOLDEN_SEARCH_PROOF_ROOT` read at `proof.py:164`; `RESEARCH_*` read at `golden_search.py:483-494`, `grid_search/dispatch.py`, `recency/dispatch.py`, `research_runtime.py` | all have readers | — |
| Job phases | #2696 | `jobs/phases.py:134` | — | all 7 ids emitted at `stages.py:365,369,373,679,681,791,955`; pinned by `…test_a_study_runs_from_lock…:431` | implemented | — |

## B. Cut candidates (lens 2)

No cuts to `app/main.py`, `config.py` or `phases.py`.

| id | path::symbol and lines | what it is | callers searched | goes with | OpenAPI regen? | confidence |
|---|---|---|---|---|---|---|
| **B1** | `PythonDataService/app/routers/golden_qualifications.py::list_golden_qualifications` 40–52 | `GET /api/research/golden-qualifications` | `git grep -n -E "golden-qualifications\|golden_qualifications\|list_golden_qualifications\|list_judged"`. Production hit is `golden-search.service.ts:90` (`qualificationsBase`), used only for `deploy-offer`. The rest are `main.py` mount, docs, `test_golden_qualifications_endpoints.py` (`:96`) and `contracts/data-plane-control-surfaces.json`. No Backend, scripts, CI or runbook hit. | `qualification_service.list_judged` (`qualification_service.py:146-154`, `__all__:361`); `qualifications.list_qualifications` (`qualifications.py:312-333`, `__all__:952`) and its test `test_qualifications_repository.py:300-309`; endpoint test line `:96-102`. `GoldenQualificationSummary` **stays** (base of `GoldenQualificationDetail`) but its component schema drops out of the OpenAPI document. | yes: operation `list_golden_qualifications_api_research_golden_qualifications_get`; TS `operations[...]` and path in `broker.types.ts`; schema `GoldenQualificationSummary` | confirmed no caller; **cut the family together with B2/B3** |
| **B2** | `golden_qualifications.py::list_golden_defaults` 55–66 | `GET …/golden-qualifications/defaults` | same grep. `judged_defaults` has a second caller, `qualification_service.ready_defaults:195` ← `services/broker_v2_panel/panel_deploy.py:145`. | `GoldenDefaultView` (`schemas/golden_qualifications.py:203-224`); route import; endpoint test assertions (`:97,…` and the `defaults` call inside `test_revoking_the_default_clears_the_pointer…`). `judged_defaults`, `JudgedDefault`, `ready_defaults` **stay**. | yes: `GoldenDefaultView` and the path | confirmed no caller |
| **B3** | `golden_qualifications.py::get_golden_qualification` 69–71 | `GET …/golden-qualifications/{id}` | same grep | only the route fn. `GoldenQualificationDetail`, `_judged`, `get_judged` stay (revoke and re-proof return Detail, deploy-offer uses `_judged`). Test assertions that read `GET /{id}` (`test_golden_qualifications_endpoints.py:98,…` and `before = …` in `test_reprove_brings_a_stale_version_back…`) need rewriting to use the revoke/reprove response body. | yes: path and operation | confirmed no caller |
| **B4** | `schemas/golden_search.py:1767` `GoldenSearchRunDetail.daily_equity` plus class `GoldenSearchEquityPoint` 1635–1637; producer `research/golden_search/views.py:549,557` (**views unit's file**) | the per-session equity series on every candidate detail run | `git grep -n -w "daily_equity" -- Frontend/src` hits only `golden-search.types.ts:269` (`DailyEquityPoint` alias, no importer) and `testing/fixtures.ts:1020,1051`. The UI reads `cumulative_return` and `drawdown` instead. Backend: none. The stored `detail_json["daily_equity"]` is the chance/trade_net input and **stays**. | views.py two lines; `test_views.py:461` assertion (`== []`); TS alias `DailyEquityPoint`; fixtures lines; `GoldenSearchEquityPoint` | yes: `GoldenSearchRunDetail` schema and `GoldenSearchEquityPoint` | confirmed unread; touches the views unit's file |
| **B5 (owner ruling, do not apply without one)** | `golden_qualifications.py::revoke_golden_qualification` 80–93, `reprove_golden_qualification` 96–108 | the only writers of `revoked` and `reproved` qualification events | same grep: no Frontend, Backend, script, CI or runbook caller. PRD #2696 and ADR 0074 §5 name both; `QUALIFICATION_REVOKED` and `…_STALE` states have no other writer. | If ruled cut: `RevokeQualificationRequest`, `ReproveQualificationRequest`, `IDEMPOTENCY_KEY_PATTERN`, `GoldenQualificationEventView`, `GoldenQualificationProofView`, `qualification_service.revoke/reprove_qualification/_append_reproof`, `qualifications.revoke_qualification`, `proof.reprove`, ~12 DB-gated tests, and the `/api/research/golden-qualifications` prefix in `contracts/data-plane-control-surfaces.json:9`. `judge()` and admission still read events, so "revoked" becomes unreachable. | yes | **owner ruling**: revoke is a gate control, admission-adjacent |

Any test deletion also touches `PythonDataService/scripts/pr_shard_durations.json`, which carries test node ids.

## C. Findings (not cuts)

| id | path::symbol:line | lens | sev | evidence | disposition | what the fix orphans |
|---|---|---|---|---|---|---|
| **C1** | `schemas/golden_search.py:133-134` `training_months`/`test_months: int` unbounded; `protocol.py:691` catches only `ValueError`; `walk_forward_study/folds.py:61 add_months` | 5 | P2 | **Repro**: `POST /preflight` with `training_months=10**12` → HTTP 500 `"signed integer is greater than maximum"`; `2**70` → 500 `"Python int too large to convert to C long"`. `lock` shares `review_plan`, so it 500s too. Docstring and tests promise "every problem a plan can have is a refusal in a 200". `/defaults` already bounds the same fields (`ge=1, le=120/60`). Scripts: `scratchpad/P6/probe_boundary.py`, `probe_overflow.py`. | fix: bound the two fields at the schema, or add `OverflowError` to the `except` | nothing |
| **C2** | `planning.py:625-646` `_qualified_seed_refusals`; `protocol.py:621-634` `_validate_incumbent`; `service.py:209 resolve_incumbent` only on `/defaults` | 3/5 | P2 (P1 by the letter) | **Repro** (`scratchpad/P6/probe_incumbent.py`, runs no-DB over a seeded temp lake): a plan whose `incumbent` is `{source:"qualification", qualification_id:"gq-does-not-exist", params:{gap:0.35}}` is lockable (`refusals:[]`). So is `{source:"registry", params:{gap:0.35}}`, which is not the registry point. A seed equal to a qualified tuple gets `SEED_IS_QUALIFIED` only while the incumbent honestly says `qualification`; relabelled `registry`, the leak guard passes. The incumbent is taken from the request, never re-resolved or checked against the registry or qualification store. The UI always echoes the server-computed incumbent, and approval's `expected_default_qualification_id` reads the DB. So it takes a hand-built request. | fix or leave: re-resolve on lock; severity is the lead's call (owner's "don't key evidence on a subject-supplied value") | nothing |
| C3 | `routers/golden_search.py` all reads | 5 | P2 | With Postgres unreachable every read answers 500 with the exception text echoed (`utils/error_handlers.py:74-81`); only `reprove` maps `CatalogUnavailableError` to 503. Seen as `GET /studies?symbol=` → 500 `"POSTGRES_URL is empty…"`. Fails closed, wrong status. | leave-with-reason (global catch-all) or map to 503 | nothing |
| C4 | `schemas/golden_validation.py:67,79` `reviewed_at_ms`, `designated_at_ms: int` | 4 | hygiene | The only instant fields in the three schema files without `ge=0, le=MAX_TIMESTAMP_MS` (contract scan; `bar_span_ms` is a duration). temporal-rigor.md requires the ceiling on every `*_ms`. | fix | OpenAPI regen (bounds appear) |
| C5 | `Frontend/src/app/services/golden-validation.types.ts` (hand-written) vs `GoldenValidationResponse` | 5 | hygiene | Golden Search's TS types derive from `broker.types.ts`; this file does not, so drift compiles. Already drifted: `evidence_applicability?` optional in TS, required in Python. | fix: derive from the generated schema | none |
| C6 | `golden_fixtures.py:71` `generated_at: str`; `golden-fixtures-catalog.component.html:46` `\| slice:0:16`; `tests/fixtures/conftest.py:36-37` | 4/3/7 | P2 / hygiene | ISO string on the wire and string-sliced in a template (ban list: `*_at` typed string). The guard script allow-lists only the TS file (`check_temporal_authority.py:57-62`); ADR 0022 (f) does not name it. The writer swallows errors with `except Exception: pass`. | owner-ruling (accept the allow-list) or fix `generated_at_ms` | `golden-fixtures.types.ts:21`, guard allow row |
| C7 | `golden_fixtures.py` | 6 | hygiene | No Python or Frontend test for the route or the page. `canonical_module`, `canonical_callable`, `tolerance.rtol`, `tolerance.note` are served but never rendered. A malformed manifest entry (`f["id"]` KeyError) is a 500. | leave | — |
| C8 | `golden_qualifications.py:24-37`, `golden_validation.py:19-42` | 5 | hygiene | These two routers declare no `responses=`, and use `{code,message}` where Golden Search uses `{code,message,field,refusals,study}`. `current_evidence_revision` (409 body) has no reader (workbench handles the code only). | leave | — |
| C9 | `golden_validation.py:76 golden_run_id: int`; `schemas/golden_validation.py:15 source_run_id: int gt=0`; `golden_qualifications.py:70 qualification_id: str` | 5 | robustness | No upper bound or length; a value above int64 would reach asyncpg as a 500. Unverified here (needs a DB). `StudyId` shows the pattern (`max_length=64`). | fix (one line each) | — |
| C10 | `golden_validation/service.py:486-494 list_dossiers` | 5 | robustness | 1+2N sequential queries per `GET /golden-validations` (N ≤ 100 by default, 500 max); `list_latest_accepted_dossiers` already batches. | leave-with-reason | — |
| C11 | `service.py:766`, `models.py:32,41`; `Frontend/…/golden-search.types.ts:244` | 1/2 | hygiene | `close` command: no UI producer (only tests and `testing/fixtures.ts` list it); the `closed` state is reachable only by hand-built API call. PRD names `awaiting_review → closed`. | owner-ruling | service/actions rows (service unit) |
| C12 | `routers/golden_search.py:534` | 5 | hygiene | `drain` returns a naked `dict`, undeclared in OpenAPI; lives in the Golden Search router but drains the shared research pool. | leave | — |
| C13 | wire fields with no production Frontend reader (`-w` over `Frontend/src/app`, specs and `testing/` excluded) | 2/5 | hygiene | `GoldenSearchStudySummary.updated_at_ms`, `.run_to_compare`; `GoldenSearchExposure.ledger_overlaps`, `.outside_activity_overlaps`; `GoldenSearchCapabilityKnob.default_low/default_high/neighbor_step/searchable_by_default`; `GoldenSearchZoomRound.submitted/first_evaluated` (read only by `research/benchmark.py:881`, from stored payload not the wire); `GoldenSearchKnobSummary/ProcedureView.stop_reason`; `GoldenSearchVerdict.successful_folds/defined_folds`; `GoldenSearchValidationFold.train/test/incumbent_test metrics, failure_code`; `GoldenSearchNeighborhood.one_sided`; `GoldenSearchChance*Basis` (`convention`, `chunk_replications`, `mean_block_sessions`, digests, `horizon`, `final_interval`), `…RehearsalRules.drawdown_basis`, `…RehearsalActivity.*`, `…Measured.objective_percentiles_1x`, `…FitToFitDirection.opponent`; `GoldenSearchDeployHandoff.program_key`; `GoldenQualificationDeployOffer.is_default`; `GoldenValidationResponse.designated_by/designated_at_ms`; fixture catalog fields in C7. The chance `basis` blocks are ADR 0074 §12 audit records. Most are stored-record projections, not read-time computations. | leave-with-reason | — |
| C14 | `results.validation` in every `GoldenSearchStudyDetail` | 5 | robustness | Duplicates what `/charts/test-over-time` serves (UI reads only `folds[].status`, `verdict`, `summary_pills`, `explanation`, `incomplete` from it). Payload weight on each study poll, not dead. | leave | — |
| C15 | `tests/routers/test_golden_{search,qualifications,validation}_endpoints.py`, `tests/research/golden_validation/test_service.py` | 6/3 | known tier | 46 of 78 tests skip without an ephemeral Postgres (list in G). PR CI has no Postgres, so revoke, re-proof, golden-validation review and the lock/command/jobs paths run only in the daily. | leave-with-reason (documented tier) | — |
| C16 | `golden_qualifications` operator surface | 7 | hygiene | Revoke, re-proof and the three reads have no runbook; the only documented operator route in this family is `/pool/drain`. | fix: add a runbook line, or resolve via B1–B5 | — |

Test health for my unit: I found no trivial, copy-pin, mock-theater or retired-feature tests. `test_applicability.py` (7) asserts real behaviour. `# type: ignore[arg-type]` appears once, in `test_service.py:250`; ruff is clean.

## D. Considered and kept

- `POST /api/jobs-internal/pool/drain`: runbook caller, route-level control secret, also mounted on the research runtime. Keep.
- `golden_fixtures` router: a live feature (Frontend page, menu, Dockerfile, compose mount); not Golden Search.
- `qualification_service.judged_defaults/ready_defaults/JudgedDefault`: Deploy reads them in-process (`panel_deploy.py:145`). Only the HTTP route is uncalled.
- `GoldenQualificationSummary`: base class of the Detail; `GoldenQualificationResearch`: used by deploy-offer (TS `QualificationResearch` alias exists but has no importer).
- `GoldenSearchRefusalBody`: used only as the OpenAPI `responses=` model; documents the typed refusal.
- `_CamelTolerantModel`, `_Wire`, `GoldenSearchRevisePayload`, `GoldenSearchScoredNumbers`: bases or validator helper (only classes unreachable from routes).
- Stress `slippage_add/commission_add`, `job_id/study_id/stage_token` fields: request or echo shapes the server needs.
- `golden_validation` repository and service: every public function has a production caller (`strategy_validation_admission.py:237,266,277`, `panel_deploy.py:202`, `golden_search/approval.py:642`, the router).
- `GoldenSearchStudySummary` and others in the unread list that look like audit records (ADR 0074 §12 basis fields).
- `designate` replay by source run returns the first designation under a different `command_id` (idempotent by source); designed, not a silent loss.
- `FORWARD_TIMEOUT_S=10` and reconcile-on-transport-failure in `research_runtime_client.py`: bounded and reconciled before answering.
- `golden_search` job params stored in Redis (`JobsApi.cs:109-119`) include the stage token; the token is already bound by the time the job is listed; no escalation.
- Unbounded per-item `key` length on the curves route: lookups only, no state.

## E. Pointers

- `PythonDataService/app/research/golden_search/views.py:549,557`: `daily_equity` projection (B4); owner: views unit.
- `Frontend/src/app/components/golden-search/golden-search.types.ts`: ~30 exported aliases with no production importer (`KnobKind`, `FixedControl`, `KnobConstraint`, `SelectionPolicy`, `ZoomSettings`, `ExecutionAssumptions`, `StressScenario`, `ExposureClaim`, `StageEstimate`, `StudyEstimate`, `RunUpView`, `StudyReceiptSummary`, `StudyGuidance`, `StageDispatch`, `StudyDecision`, `StopReason`, `ProcedureCounts`, `FoldRunStatus`, `ValidationVerdict`, `ValidationSummaryPills`, `CandidateNeighborhood`, `IntervalMs`, `EvidenceScope`, `DailyEquityPoint`, `TradeCharts`, `ChanceComparisonLabel`, `CandidateRunDetail`, `RunGroup`, `QualificationResearch`, `QualificationStatus`); owner: Frontend types unit. No OpenAPI change.
- `PythonDataService/tests/fixtures/conftest.py:36-37`: swallowed exception that writes `latest.json` (hard rule); owner: fixtures/hygiene.
- `Backend/Jobs/JobsApi.cs`: job `params` Redis hash and `GET /api/jobs/` expose `stage_token`; owner: Backend unit.
- `app/research/golden_search/shared_pool.py::drain_shared_pool`: serves all research pools but is mounted via the Golden Search router; owner: pool unit.
- `contracts/data-plane-control-surfaces.json:9`: golden-qualifications prefix exists only so proxy-attached POSTs work; the Frontend sends none (changes with B5).
- #2900 (one runner for trade-bar backfill): no occurrence in my unit.
- Waits on another unit: B4 (views.py), C11 (service/actions).

## F. P0/P1 candidates

None confirmed. P2 repros are C1 and C2, which I ran; their key output lines are quoted in the table above. C2 is a P1 only if you choose to apply the letter of "gate keyed on a request-supplied label"; the UI path cannot trigger it.

## G. Coverage

- **Read in full:** `routers/golden_search.py`, `golden_qualifications.py`, `golden_fixtures.py`, `golden_validation.py`; `schemas/golden_search.py` (1804 lines), `golden_qualifications.py`, `golden_validation.py`; `golden_validation/service.py` (715 lines); `research_runtime_client.py`; `test_golden_search_endpoints.py`, `test_golden_validation_endpoints.py`, `test_applicability.py`.
- **Skimmed:** `golden_validation/repository.py` (signatures and callers), `test_golden_qualifications_endpoints.py` (full body from line 120; header skimmed), `test_service.py` (head and names), `Backend/Jobs/JobsApi.cs` (dispatch, list, redaction).
- **Not covered:** the service/views/chance internals (other units); the generated `broker.types.ts` beyond schema-name checks.
- **Tests run** (host venv, `NUMBA_CACHE_DIR` in scratchpad): `tests/routers/test_golden_search_endpoints.py`, `…_qualifications_endpoints.py`, `…_validation_endpoints.py`, `tests/research/golden_validation/` → **32 passed, 46 skipped, 0 failed**.
  - 7 passed in `test_applicability.py`; 25 passed in `test_golden_search_endpoints.py`.
  - Skips are all Postgres-gated: 14 in `test_golden_search_endpoints.py`, 17 in `test_golden_qualifications_endpoints.py`, 3 in `test_golden_validation_endpoints.py`, and 12 test functions (13 parametrized cases) in `golden_validation/test_service.py`.
  - The reason is `live-DB endpoint tests need an ephemeral POSTGRES_URL` or `POSTGRES_URL not configured`. I did not start Postgres, per the brief.
- **Other runs:** `ruff check` on all touched files: clean; `python scripts/export_openapi_contract.py --check`: exit 0, repo clean afterward.
- **Probe scripts** (`scratchpad/P6/`): `probe_boundary.py`, `probe_overflow.py`, `probe_incumbent.py`, `fx_probe.py`, plus `fields.py`, `fieldhits.py`, `fieldhits2.py` (field-reader scan; results in `classes.json`, `fieldhits.json`).
- **Method caveat for C13:** field readers are matched by name over the whole of `Frontend/src/app`, specs and `testing/` excluded. A generic name read from a different parent object is therefore counted as read; the unread list is a lower bound.

---

### Slice F1 — the charts

# F1 report: Golden Search charts (Frontend, read-only)

Pinned SHA `cb667942`, worktree `/Users/inkant/learn-ai/.claude/worktrees/cleanup-3156-golden-search-4af8ab`. Nothing in the repo was touched.

**Headline**
- No P0/P1 found.
- No in-unit cut. One 2-line dead-row cut in the compare-step file, outside my unit (B1).
- One real visual defect: C1, the drawdown panel's legend "Median" tick is invisible because `--text` is an undefined CSS token.
- Test-health findings: C2–C6.
- All 11 spec files of the unit pass (113 tests). A scratch probe (outside the repo) ran every builder's table and tooltip formatter on the fixtures with no throws or `undefined`/`NaN` text.

---

## A. Feature table

All entry points are under `Frontend/src/app/components/golden-search/` unless noted. Every feature below is reachable in checked-in config (a routed study page, a report view, or the shell docs route).

| # | feature | requirement | entry point | normal / empty / failure | classification | evidence |
|---|---|---|---|---|---|---|
| 1 | 12-column page grid, 12/8/6/4/3 spans | #2821 US1–3 | `charts/golden-search-grid.component.ts::GoldenSearchGridComponent` + `src/app/styles/_page-grid.scss` | CSS only | **unverified** | No spec evaluates spans or the 1100/720 container breakpoints. Used by compare-step, evidence-tabs, final-charts, plan-summary, procedure-charts and test-step. |
| 2 | Chart host (lazy ECharts, theme tokens, resize, dispose, table, draw failure) | #2821 Shared building blocks | `charts/golden-search-chart.component.ts` + `golden-search-echarts.ts` | Failure: alert, clears the old drawing, opens the table | **implemented** | `charts/golden-search-chart.component.spec.ts` (3 tests) |
| 3 | "Show as table" lists every value | #2821 US8 | each builder's `ChartSpec.table` | Null cells print "—", never 0 | **implemented** | Table assertions in `golden-search-compare-step.component.spec.ts:235-454`, `-decision-step…:51,55`, `-plan-summary…:23`, `-search-step…:21-156`, `-test-step…:26,97`, `-runs…:81`, `-curve-set…:54` |
| 4 | Hover detail (tooltip rule: what / value / rule / sample / in-sample) | #2821 US4–7 | `tooltipHtml` + each builder's `formatter` | Missing reads "not recorded" | **partial** | Only run curves have a committed hover test (`golden-search-runs.component.spec.ts:69-72`). My scratch probe called every builder's formatter on fixtures: no throw, no `undefined`/`NaN`/`null` text, no row/column mismatch, no duplicate row keys. |
| 5 | Missing never looks like zero | #2821 principle, #2930 US18 | `'-'` data in builders, `session-curves.alignToSessions`, `NOT_RECORDED` | Gaps stay gaps | **implemented** | `golden-search-session-curves.spec.ts` (null curve has no fall, never 0), `golden-search-curve-set.component.spec.ts:94-119`, `golden-search-drawdown-panel.component.spec.ts:28-31` |
| 6 | "About this chart": guide drawer + every guide anchor | #2821 US54–56, 61 | `charts/golden-search-panel.component.ts::about` → `MarkdownDrawerService` | n/a | **implemented** | `charts/golden-search-chart-guides.contract.spec.ts` (44 tests: 43 sections + reading path), `golden-search-compare-step.component.spec.ts:266` |
| 7 | "Walk me through it" | #2821 US57–58 | `charts/golden-search-walkthrough.component.ts` + `highlightActions` | Unknown group → clear only | **implemented** for `equity-and-fall`; **unverified** in the repo for the other charts | `charts/golden-search-walkthrough.component.spec.ts` (3). My scratch probe: every non-null step target of the 41 chart guides that draw a chart matches a series id. |
| 8 | "Review this study" tour | #2821 US59 | `charts/golden-search-review-tour.ts` + `golden-search-study.component.ts:11,107,239` | Plan stops always present, because plan is never "upcoming" (`golden-search-steps.ts:50-57`) | **implemented** (`without-best` → 'months' tab stop unverified) | `golden-search-study.component.spec.ts:489-524`; order vs guide reading path: contract spec |
| 9 | Run curves: All runs, per-step curve sets (#2930) | #2930 US10–18 | `golden-search-run-curves-chart.ts`, `-step-curves-chart.ts`, `-session-curves.ts`, `-curve-store.ts`, `-curve-set.component.ts`, `-runs.component.ts` | Failed / not recorded / left-out are each worded | **implemented** | `golden-search-curve-set.component.spec.ts` (12), `golden-search-session-curves.spec.ts` (5), `golden-search-runs.component.spec.ts` |
| 10 | Equity and fall (V19) and final equity (V34) | #2821 US34–35, 51 | `golden-search-equity-chart.ts`, `-equity-chart.component.ts`, `-final-charts.ts` | No run: empty axes. Error: alert + retry | **implemented** | `golden-search-compare-step.component.spec.ts:235`, `golden-search-decision-step.component.spec.ts:51-55` |
| 11 | Chance charts: comparison, final intervals/profit/carry-over, rehearsal, drawdown | #2932 slices (#3011–#3014, #3026–#3029) | `golden-search-chance-charts.ts`, `-chance-final-charts.ts`, `-rehearsal-charts.ts`, `-drawdown-charts.ts` | measured / missing / failed each worded; null ranges draw only the zero line | **implemented** | `golden-search-chance-charts.spec.ts` (7), `-chance-final-charts.spec.ts` (6), `-chance-render.spec.ts` (12, real ECharts SVG), `-rehearsal-charts.spec.ts` (6), `-drawdown-charts.spec.ts` (4), `-drawdown-panel.component.spec.ts` (6) |
| 12 | Plan V2–V6, Search V7–V11, Test-over-time V12–V17, Compare V18/V21–V23, months/concentration/trades V24–V32, final V33/V36 | #2821 | `golden-search-{plan,search,test,compare,month,concentration,trade,final}-charts.ts` | Missing-reason notes at call sites | **implemented** through step component specs (tables only; no builder-level spec); hover unverified | See rows 3–4 |
| 13 | Report print layout | #3031 (report) | `src/app/styles/_golden-search-print.scss`, imported at `styles.scss:4` | n/a | **unverified** | No test. I confirmed every selector names a real element (`app-top-bar`, `app-markdown-drawer-host`, `app-page-header`, `app-page-body`, `.study > .back`, `.help`, `.gs-page-grid`). |

PRD deviations where the code and guide agree and the PRD wording is stale (not defects):
- V14 is grouped bars with retention labels, not a train = test diagonal.
- V3 draws the band, current and start marks, but no per-value ticks.

---

## B. Cut candidates (lens 2)

**In-unit:** none. Every exported builder, component, spec factory and fixture function has a production caller; evidence is in D and C10.

| id | path::symbol, lines | what | callers searched | goes with it | OpenAPI regen? | confidence |
|---|---|---|---|---|---|---|
| **B1** (outside F1, **waits on the compare-step unit**) | `golden-search-compare-step.component.ts:209` (`side-by-side`) and `:212` (`concentration-curve`) in `LINKED_CHARTS` | Two dead rows of the summary-link → chart table. The server emits chart links only for `neighbor-tornado`, `cost-stress`, `without-best`. | `git grep -n "kind: 'chart'\|LINKED_CHARTS\|'side-by-side'\|'concentration-curve'" -- Frontend/src`: only the table's own find (`:180`) and fixture rows with the 3 live targets (`testing/fixtures.ts:1263-1265`). Server: `decision_summary.py:104,124,148` is the whole producer list, computed on read (`views.py:46`), never stored. | Nothing: both panels and guide sections stay (they are real panels, just not link targets). | no | confirmed |

---

## C. Finding rows (everything not a cut)

| id | path:line | lens | sev | evidence | disposition | orphans |
|---|---|---|---|---|---|---|
| **C1** | `golden-search-drawdown-panel.component.scss:49` `.tick { background: var(--text) }` | 3 / 7 | **P2** | `--text` is defined nowhere (`git grep -nE -- '--text[[:space:]]*:' Frontend` → none; `src/app/styles/_tokens.scss:107-111` defines `--text-primary/-secondary/-subtle/-muted`). The legend's "Median" marker (`.html:9`, a 2px × 0.7rem box) resolves to a transparent background and shows nothing. The same undefined token sits at `golden-search-chance-final-panel.component.scss:10,17`, `-chance-panel…scss:3`, `-drawdown-panel…scss:3`, `-rehearsal-panel…scss:3`, all as `color:` that silently inherits. | fix (`--text-primary`) | none |
| **C2** | `golden-search-rehearsal-charts.spec.ts:30-36` | 6 / 3 | P2 | "draws a falling rung at its actual value" asserts `rehearsal.rungs.map(r => r.shares['joint'])` equals the fixture literal `[0.612, 0.748, 0.815, 0.839]` (rising) and the series keys. No test reads `option.series[].data` (`rehearsalOption` rounds `share*1000/10`). The falsifiability comment at `:10` ("draw a falling rung as rising") is not caught by anything. | fix test (assert plotted data, add a falling fixture) or delete under the bar | none |
| **C3** | `golden-search-rehearsal-charts.spec.ts:52-56` | 6 | P2 | "never lets the joint share exceed a single rule's share" asserts `rehearsalRung(2)` fixture output only. No production code runs, so it tests the fixture generator (fails the bar, kind 4). Python owns the invariant. | delete candidate (lens-6, not in cut) | none |
| **C4** | `golden-search-chance-render.spec.ts` 177-210, 232-252, 254-287 vs 309-326, 328-348, 350-359 | 6 | P2 | Three properties are tested twice, once by walking SVG path strings and once through `convertToPixel`/`containPixel`/the zrender display list. Intervals inside the plot: 177-210 vs `it.each` 309-326. Two finite band rows on ordered heights: 232-252 vs 328-348. Extreme recorded objective stays in the plot: 254-287 vs `it.each([-8,8])` 350-359. The describe at `:308` is titled "(#3031 approach, consolidated)", and PRD #2821 says "Not too much tests for visualization". The pixel/display-list variants are the stronger ones. | owner call: keep the strongest, delete the SVG-walk copies | Helpers `paintedXs`, `clusterRows`, `bandRowCenters`, `textPosition`, `plotBounds`, `finalPlot`/`GRID` (≈110 lines). `renderSvg`, `parse`, `paintedPoints`, `paintedTexts` stay for 212-222 and 289-305. |
| **C5** | `golden-search-drawdown-charts.spec.ts:20-24, 36-39` | 6 | P-none | `:36` title promises "never promises one path in twenty" but asserts `JSON.stringify(option)` has no `'confidence'` (the same absence check as `chance-charts.spec.ts:24`, `chance-final-charts.spec.ts:20`). `:20` ("for every candidate") asserts `xAxis.min === 0` for one candidate (a literal pin). | leave / retitle | none |
| **C6** | `golden-search-chance-charts.spec.ts:52-57` | 6 | P-none | Pins literal segment order and fixture formatting (`'better 93.5%'`). Borderline copy pin. Statistical wording is lens-1 evidence, so kept. | leave-with-reason | none |
| **C7** | duplicates, builders | 7 | P-none | `windowBar` renderItem is near-identical at `golden-search-test-charts.ts:107-120` and `-plan-charts.ts:70-78` (height factor 0.5 vs 0.55). `dateEt` is at `test-charts.ts:37` and `plan-charts.ts:21`. `windowText` (`test-charts.ts:42`) is the same logic as `windowDates` (`plan-charts.ts:26`). `tradesText` is at `trade-charts.ts:21` and `concentration-charts.ts:24`. `ONE_DECIMAL` is at `compare-charts.ts:28`, `final-charts.ts:29`, `trade-charts.ts:19`. `WHOLE` is at `chance-charts.ts:16` and `compare-charts.ts:29`. `WHOLE_PERCENT` is at `concentration-charts.ts:21` and `golden-search-display.ts:144`. `countText` vs raw `toLocaleString('en-US')` is at `drawdown-charts.ts:43,47,48` and `rehearsal-charts.ts:43`. Status words are duplicated between `compare-charts.ts:19-26` and `golden-search-compare.ts:176-179`. | fix when touched | none |
| **C8** | `golden-search-drawdown-charts.ts:43` | 5 / 7 | P2 | Hardcodes `of 10,000` while `drawdown.basis.replications` is on the record and every other chance chart reads `basis.replications`. Python `chance.py:72 REPLICATIONS = 10_000`. The drawdown table would lie if the constant moved. The guide prose (`chart-guides.ts:130`, `golden-search-guide.md:852,860,889,935`) also quotes 10,000. | fix (use `basis.replications`) | none |
| **C9** | `golden-search-chance-final-charts.ts:26-27,55-56`, `golden-search-drawdown-charts.ts:95-96` | 7 | P-none | Three `no-explicit-any` disables (+1 in `chance-render.spec.ts:331`). Sibling `golden-search-chance-charts.ts:185-198` types the same custom-series `renderItem` with no `any`, and `test-charts.ts:107` / `plan-charts.ts:70` use `NonNullable<CustomSeriesOption['renderItem']>`. Probably removable; not compiled by me. | fix with `tsc` (optional) | none |
| **C10** | export-keyword residue | 7 | P-none | Exported but never imported outside the defining file: `chartTheme` (`chart-theme.ts:38`; only the default factory), `TooltipContent` (`:118`), `WalkthroughStep`, `ChartGuide` (`chart-guides.ts:49,56`), `NEIGHBOR_STATUS` (`compare-charts.ts:19`), `windowDates` (`plan-charts.ts:26`), `FINAL_EQUITY` (`final-charts.ts:156`), `DEVELOPMENT_EQUITY` (`equity-chart.ts:29`), `ShareSegment` (`chance-charts.ts:37`), `CurvesRead`/`HeldCurve` (`curve-store.ts:4,11`), `ReadCurve` (`session-curves.ts:57`), `RunNoun` (`run-curves-chart.ts:19`). Test-only exports: `rehearsalSeries` (`rehearsal-charts.ts:32`, also used internally at `:40`; imported by two specs). `charts/echarts-install.d.ts:42-44` declares `installSVGRenderer` for one spec (`golden-search-chance-render.spec.ts:11`). | leave (test-only helpers stay findings) | none |
| **C11** | hover formatters, all builders but run curves | 1 / 6 | P-none | No committed test invokes a tooltip formatter (see A4). PRD US4–5 is only proven by my scratch probe. Owner direction "Not too much tests for visualization" supports leaving. | leave-with-reason | none |
| **C12** | walkthrough targets | 6 / 3 | P2 (robustness) | `HighlightTarget.group` is a string prefix into series ids authored in 15 builder files; a rename makes `highlightActions` return clear-only, silently. Only `equity-and-fall` is tested. I verified all 41 chart guides that draw a chart resolve today (probe), so there is no defect now. | leave-with-reason | none |

---

## D. Considered and kept

- **`ChartTheme.neutral/tooFew/stepBelow/stepAbove/stressed/withoutBest/development`:** all have drawing consumers (`golden-search-search-charts.ts`, `-test-charts.ts`, `-plan-charts.ts`, `-trade-charts.ts`, `-drawdown-charts.ts`, `-step-curves-chart.ts`). Several resolve to the same token, which is a design choice.
- **`echarts-setup.ts` registrations (bar, custom, heatmap, line, scatter, grid, legend, tooltip, axisPointer, markArea, markLine, visualMap, canvas):** each is used by at least one builder. No builder uses an unregistered component (grepped `dataZoom|markPoint|graphic|toolbox|title:|piecewise`).
- **`golden-search-grid.component.ts` doc says "12, 8, 6, 4 or 3":** the mixin also supports 9 and `manual/golden-search-manual.component.html:26` uses it.
- **`sparklineSpec` builds a full table that is never shown (`[offersTable]="false"`, `candidate-cards.component.html:36`):** `ChartSpec.table` is required and the build is cheap. Not a defect.
- **`golden-search-final-charts.component.ts:49-55` "final-comparison" panel appears only after runs load:** the review tour names it earlier. Self-correcting, no false claim.
- **`sameChart` ignores styling inputs (`accent`, `ceiling`):** `ceiling` is frozen at lock. A candidate switch goes through the resource's null state, so I could not demonstrate a stale colour. Unverified, not filed.
- **Rank/share rounding (`chance-final-charts.ts:250`, `shareText`):** rank 0.9999 shows "100%", but the raw counts sit beside it and the guide states the rule (guide `:889`).
- **`drawdown-panel.component.spec.ts:49-53` axe test:** keeps, since accessibility findings are out of scope per the 2026-10-12 owner ruling. `.claude/rules/angular.md` still says "must pass AXE"; the two sources disagree, so surface it, no action.
- **`withoutBest` colouring by `value > 0` (`concentration-charts.ts:177`):** the browser re-derives a classification the server also records. It colours only; the decision-summary status stays the server's.
- **`golden-search-curve-store.ts` unbounded `kept` Map:** page-lifetime, keyed by scope; recorded curves never change.
- **Fixtures used only inside `testing/fixtures.ts` (export keyword only):** `DEVELOPMENT_START_MS`, `FINAL_START_MS`, `FINAL_END_MS`, `procedureView`, `pairMap`, `chanceFitToFit`, `FINAL_SELECTION_NOTE`, `concentration`, `qualificationView`. Each is called by other fixture builders, so none is dead. Every other exported fixture has at least one importing spec. `deployOffer` is imported by `broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts:43`.
- **`NEIGHBOR_STATUS` / `golden-search-compare.ts:176` word maps:** different domain types (`EvidenceCellStatus` vs `PairCellKind`).
- **Guide md:** 43 chart anchors + `glossary` + `reading-path`, no orphan anchors, no internal `(#…)` link to a missing anchor; manual links (`manual/golden-search-manual.ts:7-35`) all resolve.

---

## E. Pointers

- **Compare-step unit:** B1 (`LINKED_CHARTS` dead rows, `golden-search-compare-step.component.ts:209,212`).
- **Shared chart infrastructure has callers outside Golden Search**, so any move or cut must count them:
  - `components/grid-search/grid-search-curves.component.ts` uses `GoldenSearchChartComponent`, `sameChart`, `curveStore`, `runCurvesSpec`.
  - `components/grid-search/grid-search-result.component.spec.ts` uses `fakeCharts`.
  - `components/walk-forward-study/walk-forward-fold-curves-chart.ts` and `walk-forward-fold-curves.component.ts` use `sessionCurvesOption`, `onOneAxis`, `recordedSpan`, `lastValue`, `NOT_RECORDED`, `tooltipHtml`, `reloadOnProgress`.
  - `components/entry/entry.component.ts` imports `reducedMotion` from `charts/golden-search-chart.component.ts`.
- **`--fs-2xl` (`manual/golden-search-manual.component.scss`)** and **`--p-primary-color` (`golden-search-knob-table.component.scss`)** are also not defined in repo tokens. They are outside F1 (manual / knob-table units); `--p-*` is probably a PrimeNG runtime variable.
- **The chart-timestamp guard** (`Frontend/scripts/verify-chart-timestamp-guard.cjs:6-9`) lists only two broker/options chart files, so the Golden Search builders are not covered. They do use `shared/timestamp` correctly (`mode: 'date-et'` for date-anchored values, `'local'` for trade instants).
- **#2900:** not touched by this unit.

---

## F. P0/P1 candidates

**None.**
- No money path.
- The final-equity ceiling is the same `policy.max_drawdown_ceiling` the exam rules judge (`exam_rules.py:140`).
- Missing values were never drawn as zero in any builder.
- Chance wording reads from stored counts and sentences.
- Probe result: 48+ builder invocations with formatters run on fixtures, including null/failed folds, null-RSI/no-bars trades, a single-bin histogram and an unrecorded engine drawdown. The only strings the probe flagged were false positives: the literal label "Better / tied / worse / undefined", a server sentence containing "0 undefined", and an empty-axis tooltip index that ECharts would never request.

---

## G. Coverage

- **Read in full:**
  - Every file in `charts/` (all 13 plus 3 specs).
  - All 12 builders: chance, chance-final, compare, concentration, drawdown, final, month, plan, rehearsal, search, test, trade.
  - `golden-search-equity-chart.ts` and its component (ts/html/scss).
  - `golden-search-run-curves-chart.ts`, `-step-curves-chart.ts`, `-curve-store.ts`, `-session-curves.ts` (+ spec).
  - `golden-search-curve-set.component.ts` (+ spec), `-pair-landscape.component.ts`, `-procedure-charts.component.*`.
  - `golden-search-drawdown-panel.component.*` (+ spec), `-final-charts.component.ts`, `testing/fake-charts.ts`.
  - All 9 unit spec files; `_golden-search-print.scss`; `_page-grid.scss`.
- **Skimmed or call-site only:**
  - `testing/fixtures.ts`: export list, importer map and selected builders.
  - Step components (compare/test/search/plan-summary/decision/runs), the chance panels, evidence-tabs and candidate-cards, at the call sites.
  - `golden-search-guide.md`: anchors and links checked programmatically; two sections read.
  - PRDs #2821 (to line 260) and #2930 (to ~120).
- **Tests run:**
  - `npx ng test --no-watch` over the 11 spec files of the unit: **11 files, 113 tests, all passed** (exit 0). The same run is the AOT type-check.
  - `eslint --max-warnings 0` on the unit's files: clean.
  - Scratch probe `scratchpad/F1/fe/src/app/components/golden-search/probe-f1.spec.ts`, run in a copy of `Frontend/` (not the repo); logs are `scratchpad/F1/ngtest.log` and `probe.log`.
- **Not covered:**
  - No live browser run, so C1 rests on CSS custom-property semantics, not a screenshot.
  - `golden-search.types.ts`, the generated `broker.types.ts` and the guide prose were not read in full.
  - The step components' own logic belongs to other units.

---

### Slice F2 — study flow: page, plan form and steps

# F2 report: study flow (page, plan form, steps), Frontend, read-only

SHA `cb667942`. Nothing in the repo was touched. Scratch files are in `.../scratchpad/F2/`.

Result: **no P0/P1**. One cut of three trivial lines, four P2/robustness findings with code-read evidence, and one owner-ruling question (the `close` command has no UI).

## A. Feature table

| feature | requirement | entry point | reachable (checked-in config) | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| Page and routes: `/golden-search`, `/golden-search/:studyId`, `?revise=`, `?step=`, `?view=report`, menu entry | #2696 | `golden-search-page.component.ts::GoldenSearchPageComponent`; `app.routes.ts:224-237`; `app-menu.ts:96` | yes. `?step` is linked from the manual and report; `?revise` from plan-summary | loading notices shown; capabilities error shown; revise load failure falls back to defaults with a message | implemented, one gap (C1: Hide lands on the wrong tab) | `golden-search-page.component.spec.ts` (3 tests); manual spec hrefs |
| Plan form: defaults, edit, debounced preflight, lock | #2696 | `golden-search-plan-form.component.ts` | yes | stale preflight answers dropped by generation; unreadable value blocks the check; defaults/layout failures shown and fail closed | implemented | `golden-search-plan-form.component.spec.ts` (58 `it`); `golden-search-plan-draft.spec.ts` (24) |
| Run research from the form (lock then Search+Test, stop at Compare) | #2811 §3 | `plan-form::lock(true)`; `createStudy({run_research})`; revise path sends `revise` then `run_research` | yes | one request for a new plan; two commands for a revision | implemented, one gap (C2) | spec "Run research locks the plan…", "…on a revision runs the new study under a key derived from the revision" |
| Expected trade frequency control, legacy plan adoption, window floors | #2811 §1, ADR 0074 | `protocol-controls`, `golden-search-activity` (other unit), `plan-draft::applyPlanEdit` `'number'` | yes | non-frequency branch reachable only when revising pre-#2811 studies | implemented | plan-form spec "shows editable expected trade frequency…", "clears obsolete floor errors…"; study spec "a frequency plan shows the floors…" |
| Knob table, importance order (ADR 0074 §10), pair audits, held knobs | #2696, #2813 | `knob-table`, `plan-draft::byImportance/withImportance`, `refinement-controls` | yes | server count of settings shows "…" until checked, "—" when range invalid, never 0 | implemented; TS/Python parity pinned by `contracts/fixtures/golden-search-importance-order-v1.json` | `plan-draft.spec.ts` "shows the search order the server freezes for every pinned case"; Python `test_planning.py:121` |
| Time windows, study range, server date layout | #2886 | `time-windows`, `plan-form::scheduleDates/layOutDates` | yes | range the server cannot lay out blocks lock; older data plane detected (`RANGE_NOT_LAID_OUT`) | implemented | plan-form spec lines 617-948 (about 14 tests) |
| Backfill-before-search gate in the footer | #2898; #2900 is the known duplicate (pointer only) | `plan-backfill.ts::GoldenSearchPlanBackfill`, `plan-footer` | yes | running, finished-still-short, failed, cancelled, cancel-failed, left-the-page all have copy | implemented | plan-form spec lines 950-1128 (7 tests) |
| Plan summary (frozen plan, charts, Revise) | #2696, #2821 | `plan-summary.component.ts` | yes (Plan step) | chart read failure shows alert and Try again; lake-unreadable coverage says so | implemented | `plan-summary.component.spec.ts` (4); study spec "Plan step: …" |
| Stepper plus research-process rail | #2696, #2811 | `steps.ts`, `process.component.ts`, `study.component.html` | yes | all states mapped | implemented; the two rails disagree for a study closed early (C3) | `steps.spec.ts` (4) |
| Study shell: guidance, Cancel/Finish/Hide, polling, stale-revision and view-generation guards, announcement | #2696 | `study.component.ts` | yes | failed poll keeps the study shown and retries; stale answers dropped; dispatch failure offers "Start again" under the same key | implemented | `study.component.spec.ts` (36 `it`) |
| Search step | #2696, #2821 | `search-step.component.ts` + `procedure*` | yes | pending copy by state; chart failure retry | implemented | `search-step.component.spec.ts` (13) |
| Test over time step | #2696 | `test-step.component.ts` | yes | planned folds shown before the stage runs; failed folds listed | implemented | `test-step.component.spec.ts` (7) |
| Compare step and decision summary | #2811 §4 | `compare-step.component.ts`, `compare.ts`, `decision-summary` | yes | missing evidence reads Missing, never Meets; every link target the server emits is handled | implemented | `compare-step.component.spec.ts` (24); `compare.spec.ts` (8) |
| Final decision step (lock, running, review, finished) | #2696, #2930 | `decision-step.component.ts`, `decision.ts`, `tuple` | yes | a missing outcome is never read as a pass | implemented | `decision-step.component.spec.ts` (31); `decision.spec.ts` (7) |
| Close a study (the `close` command) | #2696 lifecycle `awaiting_review → retained / closed`; #2930 "or closes the study" | no UI entry point | no Frontend sender anywhere (C4) | n/a | partial: server and types only | Python `test_study_service.py:213,585,1340` |
| Measured sensitivity beside controls | #2811 §2 | none | n/a | n/a | designed out: ADR 0074 §10 (owner, 2026-10-02) replaces it with owner-set importance | `docs/architecture/adrs/0074-…md:159-169` |

## B. Cut candidates (lens 2, delete-only)

| id | path::symbol, lines | what | callers searched | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| F2-B1 | `Frontend/src/app/components/golden-search/golden-search-method-choice.component.ts:30` `readonly readonly = input(false);` plus `golden-search-method-choice.component.html:1` `[disabled]="readonly()"` | an input no parent binds. `<fieldset>` is never disabled | `git grep -n -w app-golden-search-method-choice` → only `golden-search-plan-form.component.html:54`, binding `[method]` and `(methodChange)`. `git grep -n -E "\[readonly\]"` over the dir → only knob-table (`plan-summary.component.html:78`). No spec for this component | nothing; no SCSS rule keys on `:disabled` | no | confirmed |
| F2-B2 | `golden-search-search-step.component.scss:17` `.caption { color…; font-size… }` | dead rule. `search-step.html` has a `caption="…"` attribute on `app-golden-search-curve-set` but no element with class `caption` | script over class tokens in `.html`/inline templates, plus a manual read of the template | nothing | no | confirmed |
| F2-B3 | `golden-search-compare-step.component.scss:7` `.caption strong { color… }` | dead descendant rule. The `.caption` elements (`html:36,50,59,86`) hold plain text, no `<strong>` | manual read of `compare-step.html` | nothing | no | confirmed |

Not waiting on any other unit. Two further rows from the kill-list check:
- Nothing from #2701–#2716 is in this unit.
- Over-exports (C8) are not cuts.

## C. Findings (not cuts)

| id | location | lens | sev | evidence | disposition | what the fix orphans |
|---|---|---|---|---|---|---|
| F2-C1 | `golden-search-page.component.ts:62-65` `onHidden()` | 3 | P2 | `onHidden` sets `activeTab('history')` then navigates `/golden-search/:id` → `/golden-search`. Those are two separate route entries (`app.routes.ts:224-237`). The app has no custom `RouteReuseStrategy`, and the default reuse test is `future.routeConfig === curr.routeConfig` (`node_modules/@angular/router/fesm2022/_router-chunk.mjs:4127-4129`). So the page is rebuilt with `activeTab = 'new'`, the `set('history')` is lost, and after Hide the user sees "New study", not History. **Not executed**; derived from router semantics. Repro spec: `provideRouter` with both routes + `withComponentInputBinding`, navigate to `/golden-search/x`, call `onHidden()`, expect the History tab selected (it will not be) | fix (carry intent in a query param, or one route with an optional param) | nothing |
| F2-C2 | `golden-search-plan-form.component.ts:221-267` (`lock`, `revise`, `onLockFailure`) | 3 | P2 | Revise + Run research is two commands. If `revise` succeeds and `run_research` is refused (400) or conflicted (409), `onLockFailure` settles the key and shows a refusal; it never emits `locked` for the already-created study. A revise moves no revision on the source (`service.py:859-861` comment), so pressing Lock again mints a new key and creates a **second** linked study. No test covers the second-half refusal. Repro: `service.command.mockResolvedValueOnce({study: revised}).mockRejectedValueOnce(new GoldenSearchRefusedError(...))`; click Run research; `locked` is not emitted for `study-0002-bbbb`; click again → a third `command('revise')` call with a new key. Compare `StageDispatchError`, which does emit `locked` | fix: on any failure after the revise succeeded, emit `locked(revised.id)` and let the study page explain | nothing |
| F2-C3 | `steps.ts:37,50-57` (`STEP_OF_STATE.closed='decision'`, `stepProgress`), `test-step.component.ts:77-81` | 3 | P2 | Study closed before it ran (the server permits `close` from `locked`; API-only per C4): steps nav shows Plan/Search/Test/Compare/Decision all **done**. Ran `stepProgress` for `closed` under node → `plan:done search:done test:done compare:done decision:done`. The process rail above it uses `researchStage` and says Research is current. The Test step says "has not been tested over time yet. It runs after the search." for a study that will never run | leave-with-reason unless C4 adds a UI to close; then fix via `researchStage` | the two rails could share one source |
| F2-C4 | `golden-search.types.ts:244` (`CommandEnvelope<'close'>`), `testing/fixtures.ts:1348-1352`; server `service.py:846,1018-1021`, `actions.py:94` | 1, 2 | P2 | No Frontend code sends `command: 'close'`. `git log -S"command: 'close'"` is empty, and the type came in the first workbench commit `9474dc87`. The UI only renders a `closed` study's recorded decision. Server and its tests still implement the command and the `closed` state. #2930 ("or closes the study") and the #2696 lifecycle list it as an owner action | owner-ruling: add the control, or cut the server command and `closed` state (persisted studies may carry it, so the display branches stay either way) | server command, `TERMINAL_STATES`, guidance row, tests (Python unit) |
| F2-C5 | `plan-draft.ts:288-289` (`value / 100`), `golden-search-display.ts:119-121` | 4 | robustness | The percent→fraction conversion is client-side float division. Typing 1.1 freezes `max_drawdown_ceiling = 0.011000000000000001` (node: 1.1→0.011000000000000001, 12.3→0.12300000000000001, 33.3→0.33299999999999996), so a non-canonical float lands in the frozen protocol and hash. The display round-trips via `toPrecision(12)`. Harmless to results; the ceiling is a policy, not a searched parameter | leave-with-reason | – |
| F2-C6 | `plan-form.component.spec.ts:462-479` | 3, 6 | hygiene | Test title says "every field the server can refuse links to an input". It enumerates the client's own `REFUSAL_INPUTS` plus knob fields, not the server's list. The server also emits `symbol`, `method`, `knobs`, `seed`, `seed.symbol`, `incumbent.source`, `incumbent.params`, `stress`, `execution` (`protocol.py:431,433,461,470,583,586,624,632,758,763,765`). Those refusals still render, unlinked (target null). The test cannot fire for completeness | leave; rename or add a server-derived field list | – |
| F2-C7 | `golden-search-draft`: `plan-draft.ts:114-121` (`draftFromDefaults`) | 5 | robustness | The six defaults-only keys are stripped by name (`incumbent_label`, `incumbent_sentence`, `exposure`, `final_months`, `final_sessions_cut`, `range_note`). Diffing the generated `GoldenSearchDefaults` against `GoldenSearchProtocol` shows exactly those six today. A future defaults-only field would flow into the plan, then fail preflight on the server's `extra="forbid"` (loud, fails closed). Not compile-checked | leave-with-reason | – |
| F2-C8 | over-exports used only inside their own file | 7 | hygiene | `GoldenSearchTab`, `StudyStepDefinition`, `KnobOrigin`, `ProblemField`, `readNumber`, `NUMBER_FIELD_LABELS` (spec-only: `REFUSAL_INPUTS`, `RANGE_PROBLEM_KEY`), `KnobRefusalPart`, `CANDIDATE_ORIGINS`, `PairCellKind`, `PairAudit`, `OUTCOME_LABELS`, `EvidenceRow`, `RESEARCH_PROCESS`, `ResearchStage` | leave (not dead; reachable internally) | – |
| F2-C9 | specs: `plan-form.spec.ts:1149`, `plan-summary.spec.ts:60`, `search-step.spec.ts:153`, `test-step.spec.ts:94`, `compare-step.spec.ts:433`, `study.spec.ts:527,539`, `decision-step.spec.ts:41` (helper) | 6 | – | axe tests assert a rule the owner put out of scope on 2026-10-12. `study.spec.ts:527/539` re-run axe over steps the step specs already cover | owner-ruling (not a cut per the lead's call) | – |
| F2-C10 | `compare.ts::pairAudit` (`valid = x*y − invalid`) | 4 | hygiene | A tally derived in the browser although the file header says "No metric is derived here". Counting only; the server owns the cell statuses | leave | – |

## D. Considered and kept

- `study.component.ts` input `pollMs` and `plan-form.component.ts` input `preflightDebounceMs`: unbound in production but live defaults (3000, 300); specs set them to 0. Test seam, not dead.
- Non-frequency (fixed-floor) and unranked-knob branches in protocol-controls, knob-table and plan-form: reachable when revising pre-#2811 studies.
- Test step "Legacy verdict" block: the server still emits `verdict` (`views.py:337-353`) and the decision summary consumes it.
- `closed` display branches (`steps.ts`, `process`, decision step `finished` view): persisted closed studies exist; the server command and tests exist.
- `compare.ts` `selectedCaption`, `cellDetail`, `pairMapView`, `PairCellView`, `PairMapView`: callers are `candidate-cards`, `search-charts`, `pair-landscape`, all alive.
- `golden-search-process` plus the steps nav: two rails of different grain (6-stage process vs 5-step nav), both rendered.
- `LINKED_CHARTS` ids `side-by-side` and `concentration-curve`: not emitted by the server's decision summary, but they are real Compare charts the map must know.
- `app-symbol-picker` without `adjustmentMode` in the plan form: intended; the server refusal plus the footer backfill is the gate.
- `plan-backfill.ts` duplicating `EnsureCoverageService.composeAndRun`: #2900, pointer only.
- `.sr-only` duplicated in 8 component SCSS files (no global rule in `styles.scss`): accessibility utility, out of scope per the 2026-10-12 ruling.
- `window.hidden` output payload ignored by the page: trivial.
- Spec test `researchStage` living in `steps.spec.ts`: placement only.
- Measured sensitivity beside controls: see the A table (ADR 0074 §10).

## E. Pointers

- #2900 (`golden-search-plan-backfill.ts` vs `shared/symbol-catalog/ensure-coverage.service.ts`): known duplicate, do not re-file.
- C4 `close` command: server and types units (`models.py:33,41`, `service.py:846,1018-1021`, `actions.py:94`; `golden-search.types.ts:244`; `testing/fixtures.ts:1348-1352`).
- `PythonDataService/app/schemas/golden_search.py:1522` plus `contracts/openapi/python-data-service.openapi.json:28188` (and `broker.types.ts`): the `SummaryLink.target` description lists "(neighbor-tornado, cost-stress)", but the server also emits `without-best` (`decision_summary.py:148`). Doc drift; needs OpenAPI regen if edited. Owner: schemas unit.
- `golden-search-plan-charts.ts` is not in F2's list. Every export has a caller (`windowDates` is used internally). Owner: charts unit.
- `chance`, `activity`, `approval`, `lockbox`, `exam-result` and the other `golden-search-*` components were not read.

## F. P0/P1 candidates

None. Repros run: `stepProgress` for `closed`/`retained` under `node --experimental-strip-types` (C3), and the `p/100` values (C5). C1 and C2 are code-read only.

## G. Coverage

- **Read in full:** every `.ts`, `.html`, `.scss` of the unit listed in the brief (page, study, strip, steps, plan-form/draft/problems/summary/footer/backfill/card.scss, knob-table, time-windows, protocol/refinement/cost controls, method-choice, search/compare/test/decision steps, compare.ts, decision.ts, decision-summary, tuple, scope-line, process, procedure, procedure-path), plus `app.routes.ts:215-245`, `app-menu.ts:85-118`, `idempotency-keys.ts`, `golden-search.service.ts` and `golden-search.types.ts` (for contract checks).
- **Specs read in full:** `page`, `steps`, `plan-summary`, `search-step`, `test-step`, `compare`, `decision`, `compare-step`.
- **Specs skimmed by name plus samples:** `study` (36), `plan-form` (58), `plan-draft` (24), `decision-step` (31).
- **Run:** `npx ng test --watch=false` with 12 `--include` exact spec files: **12 files, 225 tests, all passed**. `npx eslint` on all unit `.ts` files (specs included): exit 0, no warnings. No NG8113 warnings from golden-search files in the AOT build. The first background launch printed only "Building…"; the foreground rerun was fine.
- **Static analysis:** exported-symbol caller counts (`git grep -n -w`), unbound inputs and unlistened outputs scan, class-token SCSS dead-rule scan, template-member references.
- **Not covered:** `charts/*`, `report/*`, `manual/*`, `history`, `runs`, `curve-set`, `evidence-tabs`, `candidate-*`, `final-charts`, `exam-result`, `lockbox`, `approval`, `retain` internals, and the Python server beyond the contract and refusal checks above. Nothing was rendered in a browser. Components without their own spec (strip, footer, knob-table, time-windows, the three controls, method-choice, decision-summary, tuple, scope-line, process, procedure, procedure-path, problems, backfill) are covered only through the plan-form, study and step specs.

---

### Slice F3 — service, wire types, evidence/approval/chance panels, history, runs

## F3 report — service, wire types, evidence/approval/chance panels, history and runs (Frontend, read-only)

No P0 or P1 confirmed. The nearest miss is F3-C1 (P2): the development profitability line is composed client-side and drops the server's "no defined path" wording. Specs for this unit pass (77 tests, 9 files), and ESLint with `--max-warnings 0` on the unit's files is clean.

### A. Feature table

| feature | requirement | entry point | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| HTTP client for every golden route | #2696, ADR 0074 | `golden-search.service.ts::GoldenSearchService` | yes, every method has a caller (`deployOffer`: broker `alpaca-deploy-workflow.component.ts:1119`) | 409 → `StudyConflictError`, 400 → refusal, dispatch failure → `StageDispatchError` | implemented | `golden-search.service.spec.ts` (11); the five chart/candidate reads (`planCharts`, `searchCharts`, `testOverTimeCharts`, `curveSet`, `candidate`) are tested only through component doubles |
| Wire types | #2696 | `golden-search.types.ts` | yes | all `Schemas[...]` aliases of the generated contract | implemented | I diffed the property sets of all 183 `GoldenSearch*`/`GoldenQualification*` schemas in `contracts/openapi/python-data-service.openapi.json` against `api/broker.types.ts`: zero drift |
| Idempotency keys | #2696 | `idempotency-keys.ts`, used by plan-form `:141,:226` and study `:170,:301` | yes | key reused on no answer; settled on any server answer | implemented, one gap (C4) | `study.component.spec.ts:318`, `plan-form.component.spec.ts:562` |
| History | #2696 | `golden-search-history.component` | yes (page tab) | rows / "No studies yet" / error alert | implemented, gap C2 | `history.component.spec.ts` (2) |
| All runs | #2930 | `golden-search-runs.component` | yes (`study.component.html:78`) | list / empty / list-error / curves-error / not-recorded note | implemented | `runs.component.spec.ts` (6 tests) |
| Candidate cards, aside, evidence tabs | #2821 | three components, mounted in `compare-step.component.html:18,25,70` | yes | no-detail, loading, error, "missing" reasons | implemented | `compare-step.component.spec.ts` (passes) |
| Development chance panel | #3011 | `chance-panel` | yes (compare, report) | measured / same_settings / baseline / missing / failed all worded | implemented, gap C1 | `chance-panel.component.spec.ts` |
| Rehearsal and drawdown panels | #3012, #3013 | two components | yes | measured / missing / failed | implemented | their specs, `rehearsal-charts.spec.ts:30` |
| Final chance panel | #3014, #3026, #3029 | `chance-final-panel` | yes (decision, report) | every component status, `positiveNetRequired` gate, legacy "no range" | implemented | spec (17 tests), `chance-render.spec.ts` |
| Lockbox | #2696, #2930 | `lockbox` | yes (`decision-step` case "lock") | acknowledgement gate, server refusal text, exposure preview | implemented | `decision-step.component.spec.ts:86,124,130,139` |
| Exam result | #2930 | `exam-result` | yes | choose between fits, "No result recorded", proof-failure alert | implemented | `decision-step.component.spec.ts:177,303,326,350,428` |
| Approval and retain | #2696 | `approval`, `retain` | yes | gated approve, retry, building, pending/stopped, approved, finished | implemented, gap C3 | `decision-step.component.spec.ts:197-259,289,365,378,390,400,421,428,442` |
| Metrics (procedure winner) | #2696 | `metrics` (only caller `procedure.component.html:44`) | yes | completed / failed / null | implemented | failure branch only: `search-step.component.spec.ts:87`; completed branch rendered, figures unasserted |
| Activity (trade-frequency floors) | ADR 0074 | `activity` | yes (plan-summary, protocol-controls) | n/a | implemented | `plan-form.component.spec.ts:163-170` |

### B. Cut candidates (lens 2, delete-only)

| id | path::symbol and lines | what | callers searched | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| B1 | `golden-search.types.ts` lines 32, 33, 34, 49, 50, 51, 52, 79, 107, 108, 109, 124, 125, 127, 128, 135, 136, 137, 144-151 (`FoldRunStatus` 146, `ValidationVerdict` 148, `ValidationSummaryPills` 151), 158, 165, 166, 269, 276, 298, 315, 344, 375, 383 | 31 exported type aliases: `KnobKind`, `FixedControl`, `KnobConstraint`, `SelectionPolicy`, `ZoomSettings`, `ExecutionAssumptions`, `StressScenario`, `ExposureClaim`, `StageEstimate`, `StudyEstimate`, `RunUpView`, `StudyReceiptSummary`, `StudyGuidance`, `StageDispatch`, `StudyDecision`, `KnobSummary`, `StopReason`, `ProcedureCounts`, `FoldRunStatus`, `ValidationVerdict`, `ValidationSummaryPills`, `CandidateNeighborhood`, `IntervalMs`, `EvidenceScope`, `DailyEquityPoint`, `TradeCharts`, `ChanceComparisonLabel`, `CandidateRunDetail`, `RunGroup`, `QualificationResearch`, `QualificationStatus` | `git grep -n -w <name> -- . ':!…/golden-search.types.ts' ':!Frontend/src/app/api' ':!contracts' ':!PythonDataService'` returned 0 hits for each of the 31. `KnobSummary` is used only by `StopReason`, which is also cut | their doc comments | no | confirmed |
| B2 | `golden-search-metrics.component.ts:20` (`compact = input(false)`), `.html:5` (`[class.metrics--compact]`), `.scss:3` | input never bound: the only caller is `procedure.component.html:44`, `<app-golden-search-metrics [metrics]=…/>`; no spec sets it | `git grep -n compact` over `golden-search/` | the scss rule | no | confirmed |
| B3 | `golden-search-chance-final-panel.component.ts:69-70` | `rangeNotRecorded` computed, referenced nowhere (template uses `profitSpec()`'s `@else`) | `git grep -n rangeNotRecorded` → declaration only | its doc comment | no | confirmed |
| B4 | `golden-search.types.ts:265` (`StudyListFilters.limit`), `service.spec.ts:135-137` | no production caller sets it. Alternative: C2's fix may use it, so decide with C2 | `git grep -n limit` | spec edit | no | probable |
| B5 | WAITS on the Python schema owner: `GoldenSearchRunDetail.daily_equity` + `GoldenSearchEquityPoint` | served at `views.py:549,557`, schema `golden_search.py:1635,1767`; no TS read (chart code reads `cumulative_return`, `drawdown`, `monthly`); only `testing/fixtures.ts:1020,1051` set it | `git grep -n daily_equity -- Frontend/src` | B1's `DailyEquityPoint`, fixtures, views.py | yes + `broker.types.ts` | probable |
| B6 | WAITS on the Python schema owner: `results.validation` payload beyond what the Test step reads | the Test step reads only `folds[].status`, `verdict.*`, `summary_pills.*`, `incomplete`, `explanation` (`test-step.component.html:5-25`, `.ts:49`, `curve-set.component.ts:19`). Never read: `folds[].{train_metrics,test_metrics,incumbent_test_metrics,failure_code,winner_hash,*_ms}`, `ValidationView.linked`, `incumbent_linked`. The charts come from `/charts/test-over-time` (`FoldChart`), which carries its own copy | name-grep within `golden-search/` | fixtures | yes | probable |

### C. Finding rows

| id | location | lens | sev | evidence | disposition | orphans |
|---|---|---|---|---|---|---|
| C1 | `chance-panel.component.ts:62-66`, `.html:41-42`, `golden-search-chance-charts.ts:221-226` | 3 | P2 (candidate P1 if the lead weighs advisory lines as acted on) | The money line is composed in the browser: "positive in X% … negative in Y%, exactly flat in Z%". It ignores the stored `profitability.sentence` and `label`. When the server says `no_defined_paths` (sentence: "No resample produced a defined compounded return, so no profitability reading is available", `chance_compare.py:421-422`) the page prints 0.0% / 0.0% / 0.0%. With partial undefined paths it omits the server's "N paths left the return undefined" (`:428`). The component doc says "every … sentence is the server's". The final panel does print `p.sentence`. The spec pins the client string (`chance-panel.component.spec.ts:29`) | fix: render `p.sentence` as the final panel does | `moneyText`, `profitabilityText`, spec line 29 |
| C2 | `golden-search-history.component.ts:53`, `service.ts:104-109`, router `golden_search.py:167` | 3/5 | P2 | The client never sends `limit`; the server default is 100. A 101st study silently vanishes from History, with no truncation notice | fix (show the cap, or send/page `limit`) | B4 |
| C3 | `approval.component.html:6` | 3 | P2 | "Research override recorded; the original warning remains" is keyed on `weak()` = `fit().weakness`, which the server recomputes on every read (`views.py:486-487`). The wire `decision` carries only kind/note/at_ms (`golden_search.py:525`). The recorded `research_override` lives on `GoldenQualificationResearch` and no Frontend code reads it. Same value for any approval made under current rules; could differ for older records | owner-ruling or leave-with-reason (agrees by construction since approve refuses weakness without acknowledgement, `service.py:982`) | none |
| C4 | `plan-form.component.ts:247-261` (pointer, plan-form reader) | 1 | P2, unverified, no test | revise path: `revise` commits, then `run_research` refused (400/409) → key settled → Lock again → new key → second `revise` under the same parent. A revise "moves no revision" (`service.py:812`), so a second child study appears; the first stays locked and never run | fix (reuse the revise key until its run half is answered) | none |
| C5 | `chance-panel.scss:3`, `chance-final-panel.scss:10,17`, `rehearsal-panel.scss:3`, `drawdown-panel.scss:3,49` | 7 | hygiene (cosmetic P2) | `var(--text)` is defined nowhere (`_tokens.scss` has `--text-primary` etc.; repo-wide `--text\s*:` → 0 hits). `color` falls back to inherited; `.tick { background: var(--text) }` computes `transparent`, so the "Median" legend swatch (`drawdown-panel.html:9`) is invisible. Also used in `golden-validation-workbench` (not mine) | fix: `--text-primary` | none |
| C6 | `chance-render.spec.ts:176-211` vs `:308-357` | 6 | hygiene | The third block is titled "consolidated" yet the older SVG path-walk tests stayed. Duplicates: `:177,189,202` (interval inside plot) ⊂ `:309` (it.each, `containPixel`, stronger, covers zero); `:232` ⊂ `:328` (two band rects, ordered rows). `:189`'s "zero visibly between" assertion (`xs.find(...)` over all painted x) is near-vacuous. Unique and worth keeping: `:212` (legend), `:254,273` (marker label placement), `:289,299` (no NaN). The ~130 lines of SVG helpers serve only the duplicates | lens-6 finding; delete the superseded tests (not a cut) | `paintedPoints`/`clusterRows`/`textPosition` helpers |
| C7 | `chance-final-panel.component.spec.ts:75` vs `:82`, `:88` vs `:108` | 6 | hygiene | `:75` and `:82` both assert `getAllByText(/chosen after the final test/i)`. `:108` subsumes `:88`, except `:88`'s "not adjusted for that choice" | merge | none |
| C8 | `rehearsal-panel.component.spec.ts:27-42` | 6 | hygiene | "draws a nonmonotone ladder at its actual falling values" supplies a guidance string and asserts that string renders; the `toBeLessThan` line checks the fixture. The real assertion is `rehearsal-charts.spec.ts:30` | delete or fold | none |
| C9 | axe checks: `chance-panel.spec.ts:90`, `final-panel.spec.ts:195`, `rehearsal-panel.spec.ts:66`, `drawdown-panel.spec.ts:49`, `runs.spec.ts:32` (plus `compare-step.spec.ts:433`) | 6 | hygiene | a11y findings are non-defects by owner ruling 2026-10-12; these are a11y lints written as tests | owner-ruling (all pass; harmless) | none |
| C10 | `golden-search-metrics.component.html:6-10` vs `display.ts::metricTexts` | 7 | hygiene | The same run's figures read `+8.70%` here (`percent:'1.2-2'`) and `+8.7%` elsewhere; the failure copy is "Run failed — …" vs "The run failed." Second formatter beside the shared one | leave-with-reason or route through `metricTexts` | none |
| C11 | four chance-panel `.scss` | 7 | hygiene | `.sentence/.muted/.diagnostic/.caption` copied four times | leave | none |
| C12 | `runs.component.ts:131-137` + `curve-store.ts:43` | 3 | robustness | A ticked run whose read returns it under `not_recorded` stores `null`; `lines` drops it silently, the checkbox stays ticked and "N drawn" counts it. Narrow race: `has_curve` is true in the listing | fix (note) or leave | none |
| C13 | served and never read by any Frontend non-spec code (name-grep over `Frontend/src`, `Backend/` has no readers) | 2/5 | robustness | `GoldenSearchCapabilityKnob.{default_low,default_high,neighbor_step,searchable_by_default}`; `GoldenSearchExposure.{ledger_overlaps,outside_activity_overlaps}`; `StudySummary/Detail.{run_to_compare,updated_at_ms}`; `GoldenSearchNeighborhood.one_sided`; `GoldenSearchKnobSummary.stop_reason` (copy comes as `stop_explanation`); `GoldenSearchZoomRound.{first_evaluated,quantization_limit}`; `GoldenSearchChanceRehearsalMeasured.objective_percentiles_1x` (written `chance_rehearsal.py:337`, read nowhere); `GoldenQualificationDeployOffer.is_default`; `GoldenQualificationDetail/Summary/EventView/ProofView/Research` provenance fields (`approved_by`, `golden_run_id`, `artifact_digest`, `wiring_digest`, `research_override`, …). Chance `basis.*` digests/chunk counts are stored-evidence audit: keep | Python owner decides; wire cut needs regen plus fixtures (`run_to_compare` at `study.component.spec.ts:66,79`) | schemas, fixtures |
| C14 | `golden-search.service.ts:96-97` | 5 | robustness | `String(value)` over `Object.entries(layout)` would send `"undefined"` for an explicit undefined key. No current caller builds one (`draftLayout` omits them) | leave | none |
| C15 | `display.ts::EXPOSURE_PREVIEW_LABELS`, `RULE_WORDS`, `PointEntry`; `service.ts::toConflict`; `types.ts::LIVE_STATUSES` | 7 | hygiene | `export` with no importer (used in-file) | leave | none |

### D. Considered and kept

- **Service methods:** `deployOffer` is called by the broker deploy workflow (`alpaca-deploy-workflow.component.ts:1119`). The `GoldenSearchRefusedError` alias, `StudyConflictError.current` and `StageDispatchError` are all used.
- **Idempotency fingerprint omits `run`:** harmless. The server derives the run key (`service.py::_run_at_lock_key`), and a retried lock replays.
- **`ExamFit.weakness` and `meetsRules`:** correct. The server merges chance items into `fit.weakness` (`views.py:486-487`), so `meetsRules` cannot read "meets" over a chance weakness.
- **Candidate aside "Luck adjustment — Not estimable":** consistent with the server's "unadjusted for selection" wording. Pinned at `compare-step.spec.ts:221`.
- **Chance panels:** every status variant is handled against `golden_search.py:1040-1273`.
- **`RULE_WORDS` vs `guidance.py::_RULE_FLAGS`:** different surfaces (chart tooltip vs candidate flag).
- **`.actions--end`** in the approval scss is used.
- **Request camelCase aliases** (`GoldenSearchProtocolRequest`): see E.
- **Lens 4:** no number is derived in the browser beyond counts and `end − 1` display.

### E. Pointers

- **Python routes with no checked-in caller (Python router reader):** `golden_qualifications.py` has five routes, `GET ""`, `GET /defaults`, `GET /{id}`, `POST /{id}/revoke`, `POST /{id}/reprove`. `git grep golden-qualifications` shows only `deploy-offer` is called, from `golden-search.service.ts:197`; the other five are exercised by `PythonDataService/tests/routers/` only.
- **Contract advertises camelCase request bodies, the only client sends snake_case:** `GoldenSearchProtocolRequest`, `…JobRequest`, `…SelectionPolicyRequest`, `…StressScenarioRequest`. The tolerant model accepts both; there is no compile check on the request side (TS `ProtocolRequest` derives from the response echo).
- **`golden-search-report.component.html:49`** hardcodes `https://github.com/tim1016/learn-ai/issues/3025` in UI copy (report reader).
- **`golden-search-manual.component.scss`:** `--fs-2xl` is undefined (manual reader).
- **`golden-validation-workbench`:** two scss files also use undefined `--text` (C5).
- **Cross-feature coupling:** `service.ts:4` imports `toRefusal` and `GridSearchRefusedError` from `grid-search.service`. Not a defect.
- **#2900 (trade-bar backfill runner):** not touched by this unit.

### F. P0/P1 candidates

None confirmed, so there is no repro. C1 is the closest and is a P2 by code reading (`chance_compare.py:419-432` vs `chance-charts.ts:221-226`).

### G. Coverage

- **Read in full:** `service.ts`, `service.spec.ts`, `types.ts`, `display.ts`, `display.spec.ts`, `idempotency-keys.ts`, `decision.ts`, and the ts/html/scss of history, runs, approval, retain, lockbox, exam-result, metrics, candidate-cards, candidate-aside, activity, evidence-tabs, chance-panel, chance-final-panel, rehearsal-panel and drawdown-panel.
- **Specs read:** history, runs (first 150 lines), chance-panel, chance-final-panel, rehearsal-panel, drawdown-panel, chance-render.
- **Skimmed:** the Python wire schemas and the router, `service.py` lock/revise/approve, `chance_compare.py` profitability, `guidance.py`, `plan-form` lock path, `study.component` command path, `decision-step`, and `broker` deploy handoff (pointer only).
- **Tests run, host:**
  - Unit specs `service`, `display`, `history`, `runs`, `chance-panel`, `chance-final-panel`, `chance-render`, `rehearsal-panel`, `drawdown-panel`: 9 files, 77 tests, all pass.
  - Related `decision-step`, `compare-step`, `study`, `decision` specs: 4 files, 103 tests, all pass.
  - ESLint on the unit's files: exit 0, no output.
- **Not covered:** browser rendering (C5 is by CSS semantics, not screenshot), the Python side beyond wire shape, and `testing/fixtures.ts`.

---

### Slice F4 — Golden Validation workbench, manual, report, guide

**F4 report: Golden Validation workbench, manual, report and guide (read-only, pinned `cb667942`)**

Nothing in this unit is dead except one test-only parameter. There are no P0 or P1 findings. The guide and manual are accurate against code apart from three small wording drifts. The rows below that need a decision from you are C1, C3, C4 and C7.

## A. Feature table

| feature | requirement | entry point | reachable in checked-in config | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| Designate a run as Validation Golden Run | ADR 0061 §1, §3 | `golden-validation-workbench.component.ts::designate` (:134), service `designate` | Yes: Strategy Lab History tab, `strategy-lab.component.html:101` (`[sourceRunId]`, `(changed)`) | Normal: card appears. Empty: catalog notice. Failure: generic message that names a cause it does not know (C2) | implemented | `golden-validation-workbench.component.spec.ts` "designates a Python history run only after the baseline note is supplied" (pass) |
| Review: accept/reject, authorized program version, Manual override | ADR 0061 §2 + #2544 amendment | `submitReview` (:175), `golden-validation-review.component.*` | Yes: both hosts | Normal: dossier updates. Stale 409: reload. Other failure: generic text (C2) | implemented | workbench spec tests 2, 3, 4, 6, 7 (pass) |
| Catalog list and select | ADR 0061 | `catalog` resource (:67), `selectCase` (:114) | Yes: `strategy-validation.component.html:240` (no inputs) | List capped silently at 100 (C3). `selectCase` has no named test | implemented, selection branch unverified | none for `selectCase` |
| Parity evidence panel | ADR 0061 §5 | `golden-parity-evidence.component.ts` | Yes (child of workbench) | "Trade deviations 0" shown with no verdict (C5) | implemented | workbench spec tests 1, 2, 4 assert only the heading. The checks list, divergence list and qualification warning have no named test |
| Evidence-applicability / rerun link | ADR 0061 #2544 amendment | `golden-validation-case.component.html:19-26` | Yes | Shown for every status, including `current` (C1) | partial | spec test 7 covers `affected` only. The fixture `CASE` omits the field |
| History "Golden" badge / "Mark golden" | ADR 0061 | `engine-lab-run-history.component.ts:78-91`, `shared/run-history/run-history.component.html:41-56` | Yes | Badge reads `state` only (C4). Catalog error silently reads as "no goldens" (C6) | implemented (consumer) | `engine-lab-run-history.component.spec.ts:185`, not run by me |
| Printable report | #2930 / #2932 PRDs | `report/golden-search-report.component.ts`, `golden-search-study.component.html:9` (`?view=report`) | Yes | Rank hidden for v1 Sharpe (#3025). Empty states present | implemented | `golden-search-report.component.spec.ts` (2 tests, pass). Print stylesheet and `window.print()` are not testable here, so unverified |
| In-app operating manual | #2821 | `manual/golden-search-manual.component.*` plus `MANUAL_STEPS` | Yes: menu `app-menu.ts:112`, page header, report header, route `app.routes.ts:312` | `?study=` binds via `withComponentInputBinding` (`app.config.ts:53`) | implemented | `golden-search-manual.component.spec.ts` (pass) |
| Chart guide (markdown) | #2821 | `assets/docs/golden-search-guide.md`; `markdownDocRoute("docs/golden-search-guide")` `app.routes.ts:316`; drawer registry `markdown-drawer.model.ts:39-46`; menu `app-menu.ts:113` | Yes | Anchors resolve. Every one of the 43 CHART_GUIDES ids has a section and a rendered panel | implemented | `charts/golden-search-chart-guides.contract.spec.ts` (44 tests, pass) |

**Cut routes (#2706).** `GET /api/research/golden-validations/{id}` and `/applicability` are gone. The router has only `POST ""`, `GET ""` and `POST /{id}/reviews` (`routers/golden_validation.py:45,61,76`). The service has exactly `list`, `designate` and `review`. `GoldenValidationService.assess` and `GoldenValidationApplicabilityRequest.as_configuration` no longer exist. `assess_deployment_scope` and `ApplicabilityReceipt` are alive (used at `strategy_validation_admission.py:237,330`).

## B. Cut candidates (lens 2)

| id | path::lines | what | callers searched | goes with it | OpenAPI regen | confidence |
|---|---|---|---|---|---|---|
| B1 | `Frontend/src/app/services/golden-validation.service.ts:17-22`, the `filters` parameter plus `HttpParams` branches | `list({strategyName, symbol})` filter params | `git grep -n -w "GoldenValidationService"` shows only two production callers, both zero-argument: `golden-validation-workbench.component.ts:68` (`this.golden.list()`) and `engine-lab-run-history.component.ts:80` (`this.goldenValidation.list()`). Only the service spec passes filters | `HttpParams` import. `golden-validation.service.spec.ts:23-33` loses its params assertions and keeps a plain GET check | No. Backend query params stay (tested at `test_golden_validation_endpoints.py:64`) | probable, parameter-level only. Your call under "test-only helpers stay findings". The Strategy Validation host shows all strategies' goldens and never wired the filter |

I found no other dead component, input, output, branch or SCSS rule. Specifically checked:
- Every selector is hosted (workbench: Strategy Lab and Strategy Validation; sub-components via `imports:`).
- Every SCSS class in all five workbench stylesheets, plus the manual and report stylesheets, matches a template element.
- `_golden-search-print.scss` selectors all exist: `app-top-bar`, `app-markdown-drawer-host`, `p-toast`, `app-golden-search-page > app-page-header`, `.shell`/`.main`/`.main-content`/`app-page-body`/`.study`, `.study > .back`, `.help`, `app-golden-search-chart > button`, `.gs-page-grid`.
- `styles.scss:4` imports the print partial.

## C. Findings (everything not a cut)

| id | path:line | lens | sev | evidence | disposition | orphans |
|---|---|---|---|---|---|---|
| C1 | `golden-validation-case.component.html:19-26` | 1/7 | P2 | The block is gated only on the field existing. Backend always sends it (`schemas/golden_validation.py:84`, no default). For `status: "current"` it still prints "Open saved configuration to rerun… designate and explicitly review the new Golden case." The ADR amendment scopes rerun to affected/unknown. Type is optional (`golden-validation.types.ts:55`), so the spec fixture omits it and the `current` path is untested | fix: gate on `status !== 'current'`, add a `current` fixture | none |
| C2 | `golden-validation-workbench.component.ts:159-161`, `:226-228` | 3 | P2 | `catch {}` and the non-409 branch drop the server's `detail.message`. Any failure (network, 5xx, command conflict, the paired-evidence-lock "retry after history update" refusal at `service.py:451`) shows "Only completed Python baselines are eligible" or "Your note and command are preserved for a safe retry". The cause shown is invented | fix: show `detail.message` when present | none |
| C3 | `golden-validation.service.ts:17-22` with `routers/golden_validation.py:65`, `repository.py:127-140` | 5 | P2 | Router defaults `limit=100` (`le=500`). The Frontend never passes `limit`. Both the workbench catalog and the History badges see only the 100 newest by `designated_at_ms`, silently. An older golden shows no badge and, on its Strategy Lab run, offers "Designate" (idempotent, so it self-corrects). On the Strategy Validation page older cases cannot be opened | fix or leave-with-reason (needs more than 100 cases) | none |
| C4 | `golden-validation-case.component.html:7`, `golden-validation-workbench.component.html:35`, `shared/run-history/run-history.component.html:41-48` | 1 | P2 | Backend `GoldenValidationDossier.state` ignores `review_is_current` (`service.py:101-108`). A stale review still reads `accepted_*`, and the badge, catalog button and case chip print "Accepted …". Only the review panel shows the red "no longer applies" alert. The deploy gate stays fail-closed (`assess_deployment_scope` needs `review_is_current is True`). Repro in F | fix: show a stale marker next to `state`; borderline P1 only for the History badge, your call | none |
| C5 | `golden-parity-evidence.component.html:12` | 3 | P2 | "Trade deviations 0" renders beside "No paired verdict" and states pending/missing/unavailable. Missing shown as zero. Header and verdict row do say the state | fix: hide the row unless the verdict is `agree`/`diverged` | none |
| C6 | `engine-lab-run-history.component.ts:78-91` (outside my unit, same service) | 3 | P2 | `goldenCatalog.hasValue() ? value() : []` makes a failed list read as "no goldens", so every Python run offers "Mark golden". The workbench opened from there shows its own load error. See E | fix (owner: Strategy Lab history reader) | none |
| C7 | `golden-validation.service.spec.ts:60-71` | 6 | hygiene | "uses the local API proxy so protected writes receive the control secret" only asserts `url.startsWith("/api/")`. Tests 1-2 already `expectOne(BASE)`. Proxy and manifest coverage lives in `tests/test_data_plane_control_security.py:167`. Fails the bar (duplicate/trivial) | delete test (not part of the cut) | none |
| C8 | `golden-search-manual.ts:25` | 7 | hygiene | Manual says "Retain current settings". The UI button is "Keep current settings" (`golden-search-retain.component.html:2`, `golden-search-approval.component.html:35`) | fix wording | none |
| C9 | `golden-search-manual.ts:4` | 7 | hygiene | "Read Plan check": the only "Plan check" is an `sr-only` heading (`golden-search-plan-footer.component.html:3`). It is not visible text | fix wording | none |
| C10 | `golden-search-manual.component.html:22,24` | 7 | hygiene | "Metric explanations" → `/strategy-lab/docs` and "Engine assumptions" → `/engine/docs`, which redirects to `strategy-lab/docs` (`app.routes.ts:254-258`). Two labels for one page. `/data-quality` also redirects (`:176`) | fix: one link, or link final URLs | none |
| C11 | `golden-search-manual.component.spec.ts:13-14`, `golden-search-report.component.spec.ts:43-44` | 6 | owner-ruling | `axe.run` assertions enforce a rule the owner ruled out of scope on 2026-10-12. 65 specs repo-wide do this, so it is a cross-cutting call | owner-ruling | n/a |
| C12 | `golden-search-manual.component.html:43`, `report/golden-search-report.component.html:49`, `report.component.ts:45-49` | 7 | hygiene | Hard-coded #3025 link and "while the tracked repair is open" copy. v1 carry-over is still the only schema produced (`chance_final.py:90`), so the report hides the rank for every Sharpe-objective study. The workbench Final step still shows the rank, with no caveat beyond the manual | leave-with-reason (tracked); delete when #3025 closes | `incompatibleRank`, `[showCarryOver]` input |
| C13 | `golden-validation-workbench.component.spec.ts:120-121` | 7 | hygiene | Comment cites retired "eyebrow" copy (#2184). Test 2 also asserts "Deviations" twice (`:137-138`) | optional | none |

## D. Considered and kept

- `golden-validation.types.ts` duplicates generated `GoldenValidationResponse` (`api/broker.types.ts:18326`). Generated `validation_case` and `parity_evidence` are `Record<string, never>`, so a hand-written type is needed. Shapes match `_case_snapshot` and the schemas. Only `evidence_applicability?` optional-versus-required drifts (C1).
- `parityPayload ?? parityEnvelope` and the second `parityStatus` operand are redundant. The envelope always carries `payload` and `status` (`service.py:366-380`). No effect, not worth a cut.
- `flattenScope` silently drops arrays in `data_policy` and `execution`. The `DataPolicy` model (`schemas/engine_backtest.py:331`) has no list field and the parameters pane prints raw JSON. Robustness only.
- `strategy-lab` `?run=` link target: consumed by `strategy-lab-config.store.ts:74`.
- The guide restates many constants. Every sampled one is true today: 10,000 resamples and mean block 5 (`chance.py:72-73`), 0.95/0.80, 99 percentiles, rungs 1-4, `MIN_CELL_TRADES=5`, `PAIR_GRID_SIZE=5`, 8 end labels, 12 hover rows, ≤60 bins, Sharpe √252 with ddof=1 (`chance.py:163-171`). No test pins the numbers. Acceptable for a served product guide with a contract spec.
- Guide anchors: all `](#…)` links and all manual `link.anchor` values resolve (checked by script). The 24 anchors not linked from the reading path or manual are reached via "About this chart" (`golden-search-panel.component.ts:88`).
- `golden-search-report.component.spec.ts` test 2's "no approve/deploy/open-final-test button" is an absence check that catches a specific false claim. Keep.
- `reuses the same review command…` and the stale-409 reload tests prove fencing/idempotency, the kind of property the test bar says to keep.
- Print SCSS and the component `@media print` block overlap only in hiding `.report-tools`/`.fit-picker`. No conflict.

## E. Pointers

- **Consumers of the service and types:**
  - `strategy-lab.component.{ts,html}` hosts the workbench (`goldenCandidateRunId`, `goldenRefreshToken`, `selectGoldenCandidate`, `refreshGoldenHistory`).
  - `strategy-validation.component.html:240` hosts it with no inputs.
  - `engine-lab-run-history.component.ts` injects the service and reads only `source_run_id` and `state`.
  - `shared/run-history` takes the `goldenValidationState` string and emits `goldenRequested`.
  - Spec stubs in strategy-lab and strategy-validation provide `{ list }`.
- **Backend (backend-validation reader):** `service.py:101-108` (C4), `routers/golden_validation.py:65` default `limit=100` (C3). `get_dossier` is still used (`strategy_validation_admission.py:277`), so it is not dead after #2706.
- **Charts reader:** `charts/golden-search-chart-guides.ts` and its contract spec (the guide's only machine guard). `CHART_GUIDES` ↔ guide ↔ rendered panels is a closed 43-id set.
- `Frontend/ci-spec-durations.json:187` names the manual spec. It follows any spec deletion.
- AOT warnings seen while running tests, outside my unit: NG8113 `DecimalPipe` unused in `data-lab/returns-distribution/returns-distribution.component.ts:81`, and NG8102 `run().metricDocumentation ?? []` at `strategy-lab/run-stats/strategy-lab-run-stats.component.html:5`.
- No #2900 overlap. `shared/markdown-drawer` carries a `methodology` doc id (not mine).

## F. P0/P1 candidates

None. Highest is the P2 C4. Repro for C4 (the dossier property the Frontend renders), run with the host venv from `PythonDataService/` with dummy `POLYGON_API_KEY=dummy` and `DATA_PLANE_CONTROL_SECRET=""`:

```python
from types import SimpleNamespace as NS
from app.research.golden_validation.service import EvidenceView, GoldenValidationDossier
review = NS(decision="accept", classification="engine_agreement",
            expected_evidence_revision="a"*64, evidence_json="{}")
d = GoldenValidationDossier(golden_run=None, validation_case={"evidence_provenance": None},
    evidence=EvidenceView(state="deviations", revision="b"*64, parity_verdict_id=1, payload={}),
    reviews=(review,))
print(d.state, d.review_is_current)   # accepted_engine_agreement False
```

The script is at `/private/tmp/claude-501/-Users-inkant-learn-ai--claude-worktrees-cleanup-3156-golden-search-4af8ab/25c84c08-edcf-45fc-bcf7-1fd6bbb35b70/scratchpad/F4/stale_state_repro.py`.

## G. Coverage

- **Read in full:**
  - the workbench dir (5 components, 5 SCSS, 1 spec);
  - the service, spec and types;
  - the manual dir (ts, html, scss, spec, `MANUAL_STEPS`);
  - the report dir (ts, html, scss, spec);
  - `_golden-search-print.scss` and the `styles.scss` import;
  - all 1,472 lines of `golden-search-guide.md`;
  - `app.routes.ts` golden routes, `app-menu.ts`, `markdown-drawer.model.ts`;
  - the Python `golden_validation` router, schema, service (designate/review/evidence/dossier) and repository list query.
- **Skimmed:** `golden-search-chart-guides.ts` (keys only), the review-tour, the plan-footer, approval, retain, study, page and exam-result components (only to verify named UI elements), `proxy.conf.js` and the control-intent interceptor.
- **Tests run (host, `ng test --no-watch`, exact specs):**
  - workbench spec, service spec, manual spec, report spec and guide contract spec → 5 files, 57 tests, all passed (7 + 3 + 1 + 2 + 44).
  - `npx eslint --max-warnings 0` over the unit's files: clean.
- **Not run:** `engine-lab-run-history`, strategy-lab and strategy-validation specs (consumers, other readers). No Postgres-dependent Python tests, which are not in this unit.
- **Not covered:**
  - issue #3025's current state (no GitHub access; the context files hold only a one-line mention);
  - runtime/browser rendering of the print stylesheet;
  - a line-by-line proof of every guide statement. The guide was checked by label/anchor/constant sampling, not re-derived chart by chart.
