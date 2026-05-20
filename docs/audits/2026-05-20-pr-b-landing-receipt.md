# PR B — Unified Engine Lab — Landing Receipt

**Date:** 2026-05-20
**Branch audited:** `master`
**Auditor branch:** `audit/pr-b-landing-receipt` (forked from `master` at
HEAD `4e60f5ac`)
**Scope:** End-to-end sanity check across all five PR B sub-PRs + the
spec/plan PR, after every sub-PR landed on `master`.

---

## 1. PRs merged

| PR    | Title                                                                                 | Merge SHA   | Merge time (UTC)         |
|-------|---------------------------------------------------------------------------------------|-------------|--------------------------|
| #302  | docs: PR B design + implementation plan (unified Engine Lab)                          | `bc925b8c`  | 2026-05-20 04:41:23      |
| #303  | feat(lean-sidecar): PR B.1 — DataPolicy contract + LEAN request shape refactor        | `e3a715ed`  | 2026-05-20 08:48:44      |
| #304  | feat(persistence): PR B.2 — both engines persist DataPolicy + Commission + BrokeragePolicy | `04c59965`  | 2026-05-20 10:04:07      |
| #305  | PR B.3 — unified history surface (engine + dataPolicy + filters + notes)              | `04c57077`  | 2026-05-20 10:45:53      |
| #306  | feat(compare): PR B.4 — /api/runs/compare endpoint + /runs/compare UI view            | `58f91237`  | 2026-05-20 11:13:45      |
| #307  | feat(engine-lab): PR B.5 — unified Engine Lab UI; retire /lean-lab                    | `4e60f5ac`  | 2026-05-20 12:02:18      |

Merge order and times line up with the plan's phase boundaries. No
hot-fix or revert commits were needed between phases.

---

## 2. Lint status

| Stack       | Command                                                       | Result            | Notes |
|-------------|---------------------------------------------------------------|-------------------|-------|
| Python      | `ruff check PythonDataService/app/ PythonDataService/tests/`  | **clean**         | "All checks passed!" |
| .NET        | `dotnet format podman.sln --verify-no-changes`                | **clean**         | No formatting diff. |
| TypeScript  | `cd Frontend && npx eslint src/ --max-warnings 0`             | **0 errors, 171 warnings** | All warnings pre-existing (`no-non-null-assertion`, `no-explicit-any`, `unused-imports/no-unused-vars`) in `payoff-chart`, `lean-engine`, `date-validation.spec`, `test-setup`, `lightweight-charts.mock`. PR #307 brought the count down from a 179 baseline by deleting `LeanLabComponent`. No new warnings introduced. The non-zero exit is solely the `--max-warnings 0` gate biting pre-existing debt. |

---

## 3. Test status (PR-B-touched code areas)

### Python (`tests/lean_sidecar/` + `tests/unit/lean_sidecar/`)

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/ tests/unit/lean_sidecar/ -v -k "not slow"
```

**Result:** `19 failed, 418 passed, 10 deselected, 12 warnings in 23.35s`

The 19 failures are **all** the known pre-existing
`RunnerConfigurationError: podman is required but was not found on PATH`
baseline — they hit any test that exercises the actual `podman` CLI from
inside the `polygon-data-service` container (which deliberately does not
ship `podman`). Affected modules: `test_hardening_profile.py`,
`test_launcher_client.py`, `test_launcher_service.py`, `test_runner.py`.

No new fixture-conflict failures appeared (brief expected 1; we got 0 —
a slight improvement vs the pre-PR-B baseline). All PR B-touched test
modules pass.

### .NET (filtered to PR B surfaces)

```
cd Backend.Tests && dotnet test --filter "FullyQualifiedName~BacktestRun|CompareController|RunCompareService|PersistEngine|PersistLean"
```

**Result:** `Passed: 61, Failed: 0, Skipped: 0, Total: 61, Duration: 1 s`

This covers:
- `BacktestRunsQueryTests` (engine filter + 2-state)
- `BacktestRunResolverTests` (engine + dataPolicy derivation)
- `BacktestRunMutationTests` (`updateBacktestRunNotes`)
- `CompareControllerTests` + `RunCompareServiceTests` (compatibility
  gate)
- `PersistEngine*` and `PersistLean*` (DataPolicy + Commission +
  BrokeragePolicy persistence on both engines)

### Frontend (filtered to PR B specs)

```
podman exec my-frontend npx ng test --watch=false \
  --include='**/lean-engine.component.spec.ts' \
  --include='**/lean-script-editor.component.spec.ts' \
  --include='**/engine-lab-run-history.component.spec.ts' \
  --include='**/runs-compare.component.spec.ts' \
  --include='**/backtest-runs.query.spec.ts'
```

**Result:** `5 test files, 54 tests passed, 0 failed (1.51s)`

All five PR-B-touched component/service specs are green.

---

## 4. Structural checks

| Check                                                              | Expected   | Actual   |
|--------------------------------------------------------------------|------------|----------|
| `Frontend/src/app/components/lean-lab/` exists                     | **No**     | absent (Glob returns no files) |
| `Frontend/src/app/components/engine-lab/engine-history/` exists    | **No**     | absent (Glob returns no files) |
| `LeanLabComponent` referenced anywhere in `Frontend/src/`          | 0 hits     | 0 hits   |
| `EngineHistoryComponent` referenced anywhere in `Frontend/src/`    | 0 *live* hits | 4 hits, **all are doc-comments / template comments** referring to the now-deleted component (in `lean-engine.component.html`, `engine-lab-run-history.component.ts`, `study-list-item.ts`). No `import`, no use. |
| `/lean-lab` route redirects to `engine`                            | yes        | yes — `app.routes.ts` line 8: `{ path: "lean-lab", redirectTo: "engine", pathMatch: "prefix" }` |
| `/runs/compare` route resolves `RunsCompareComponent`              | yes        | yes — `app.routes.ts` line 135 |

---

## 5. Database migration applied

`StrategyExecutions` table now has the three PR B.2 columns and the
B.3 JSONB index:

| Column                | Type             | Nullable |
|-----------------------|------------------|----------|
| `BrokeragePolicy`     | `varchar(40)`    | yes      |
| `CommissionPerOrder`  | `numeric(18,8)`  | yes      |
| `DataPolicyJson`      | `jsonb`          | yes      |

Indexes added (confirmed via `\d "StrategyExecutions"`):

```
ix_strategyexecution_datapolicy_symbol  btree (("DataPolicyJson" ->> 'symbol'::text))
```

Existing PR-A indexes (`IX_StrategyExecutions_Source`,
`IX_StrategyExecutions_Source_LeanRunId`, etc.) remain intact.

---

## 6. New endpoints

| Endpoint                                                  | Source                                      | Notes |
|-----------------------------------------------------------|---------------------------------------------|-------|
| `POST /api/lean-sidecar/lint`                             | `PythonDataService/app/routers/lean_lint.py:75` | Ruff via `asyncio.subprocess.create_subprocess_exec` (no shell), 5s timeout, 413 on oversize body, 504 on timeout. Source bytes never reach argv. |
| `POST /api/lean-sidecar/reconcile-trades`                 | `PythonDataService/app/routers/reconcile_trades.py:95` | Wraps the canonical `reconcile_trade_lists` helper. Emits `matched_pairs` / `python_only` / `lean_only` / `first_divergence` per spec § 6.5. |
| `GET  /api/lean-sidecar/calendar/next-trading-day-open`   | `PythonDataService/app/routers/lean_sidecar.py:922` | Returns next-trading-day open in `int64 ms UTC`. Used by the LEAN-engine path to advance `end_ms_utc` past the user-picked end (so single-day runs are not rejected as `start == end`). |
| `GET  /api/runs/compare?left=&right=`                     | `Backend/Controllers/CompareController.cs`  | Minimal-API endpoint backed by `RunCompareService`. Strict equivalence gate + summary deltas + trade-by-trade reconciliation (delegated to the Python endpoint above). |

---

## 7. Notable deviations from the spec

These are deviations the implementers called out in PR descriptions, not
bugs. All have explicit justification.

1. **Editor: CodeMirror, not Monaco** (PR #307). Plan said Monaco;
   CodeMirror 6 (`@codemirror/{state,view,lang-python,commands,language}`)
   was substituted to keep the bundle ~150 KB instead of ~3 MB. No
   functional difference for the operator: syntax highlighting + click-
   to-scroll lint diagnostics work.
2. **State-trace detection is a v1 stub** (PR #306). `RunCompareService
   .DetectStateTrace` always returns `false` — `StrategyExecution`
   doesn't yet carry a workspace-path column, so the comparator can't
   locate the artifact. The contract is "never raises"; the UI hides
   the section when both sides come back empty. Phase 5 wires the
   artifact lookup.
3. **`RawRunLinks` returns nulls** for the same reason. Frontend hides
   the section when both sides are empty.
4. **EMA-crossover template is an inline TS constant**
   (`EMA_CROSSOVER_SOURCE_TEMPLATE`), not a server-side template fetch
   (PR #307). Simpler, and the operator is expected to replace it
   anyway.
5. **Concurrency test on `POST /lint` deferred** per spec section 10.5
   ("not architecturally critical") (PR #307).
6. **`CompareController` uses minimal-API style** (PR #306) instead of a
   conventional Controller class. Matches the codebase's existing
   convention for new endpoints.
7. **Legacy `run-comparison` Angular component left in tree, unrouted**
   (PR #306). Replaced by `runs-compare` but the older GraphQL-backed
   component was not deleted — only its route was removed.

---

## 8. Open issues / followups

These are deferred items the implementers explicitly noted. None block
shipping.

1. **State-trace + RawRunLinks v1 stubs** (PR #306 caveats). When
   `StrategyExecution` gains a `WorkspacePath` (or equivalent) column,
   `DetectStateTrace` and `RawRunLinks` can become real. Phase 5 work.
2. **`test_runs_compare_e2e.py` is gated on `LEAN_LAUNCHER_URL` +
   `BACKEND_URL`** and skipped in CI (PR #306).
3. **Legacy `Frontend/src/app/components/run-comparison/`** directory
   should be deleted in a follow-up cleanup PR; currently dead code
   (still has a spec file, still type-checks, but never resolved by any
   route). Not deleted in #306 to keep that PR scoped.
4. **`@pytest.mark.asyncio` on six non-async tests** in
   `tests/lean_sidecar/test_router_lean_sidecar.py` (lines 1952, 1977,
   2006, 2036, 2063, 2091) emits `PytestWarning`. Trivial cleanup.
5. **ESLint warning baseline of 171** in unrelated files
   (`payoff-chart`, `lean-engine`, helpers). PR B reduced this from
   179. Worth a dedicated PR to drive to zero so `--max-warnings 0`
   becomes a hard gate.
6. **Pre-existing podman-on-PATH failures (×19)** in
   `tests/lean_sidecar/test_{runner,launcher_client,launcher_service,
   hardening_profile}.py`. These don't block PR B; they require a
   container topology change (running `podman` inside
   `polygon-data-service`, or factoring those tests behind a `pytest
   --runslow` style flag).
7. **HotChocolate.Language 15.1.12 critical-severity advisory
   (`GHSA-qr3m-xw4c-jqw3`)** surfaces during `dotnet restore`. Not
   introduced by PR B; track in a separate dependency bump.

---

## 9. Sample run instructions (verify end-to-end tomorrow morning)

1. **Open the unified Engine Lab** in your browser:
   `http://localhost:4200/engine`. The page should render with the new
   **Engine** dropdown at the top (default: **Python**).
2. **Python run.** Leave the dropdown on **Python**. Pick the
   `spy_ema_crossover` spec, symbol **SPY**, set the window to
   **2025-01-13 → 2025-01-17**, leave initial cash at the default, hit
   **Run**. Confirm the run lands in the unified history strip below
   with `engine = PYTHON` and a `data_policy` summary
   (`m/1 → m/15`, session `regular`, `adjusted=true`).
3. **LEAN run.** Flip the dropdown to **LEAN**. The form swaps in the
   CodeMirror editor pre-seeded with the EMA-crossover template. Set
   the same window (**2025-01-13 → 2025-01-17**), symbol **SPY**, hit
   **Run**. The Problems panel should be empty (no ruff diagnostics on
   the seed template). When the run finishes, confirm it appears in the
   unified history with `engine = LEAN` and the **same**
   `m/1 → m/15` / `regular` / `adjusted=true` `data_policy` summary.
4. **Filter + select.** In the unified history table, use the **Engine
   = All** filter to confirm both rows are visible, multi-select both
   rows (one Python, one LEAN), click **Compare**. You should land on
   `/runs/compare?left=<py-id>&right=<lean-id>`.
5. **Verdict.** Expect a green **Comparable** verdict with sub-claims:
   *Data policy: match*, *Run params: match*, *Brokerage: soft-match
   (algorithm default)*. The summary cards should show small or zero
   deltas; the trade-by-trade diff table should show two matched
   trades (entry + exit) with `first_divergence` empty. The
   state-trace section will be hidden (v1 stub returns false).
6. **Smoke the legacy redirect.** Navigate to
   `http://localhost:4200/lean-lab` — should redirect to
   `http://localhost:4200/engine`. Sidebar should no longer list "LEAN
   Lab" as a separate entry.
7. **Smoke the lint endpoint.** In the LEAN script editor, introduce a
   deliberately broken import (e.g. `import foo_does_not_exist`). The
   Problems panel should show an `F401`-style diagnostic within ~500
   ms. Clicking the diagnostic should scroll the editor to the
   offending line.

If any of those steps deviates from the above, capture the discrepancy
and route to the relevant phase's followup list (Section 8).

---

## 10. Verdict

All five PR B sub-PRs plus #302 docs are on `master`. Lint clean
(Python, .NET) / no-new-errors (Frontend). All PR-B-touched test files
pass. Database migration applied. New endpoints present. Deletions
verified. Redirect in place. Spec deviations are documented and
intentional. **PR B is landed.**
