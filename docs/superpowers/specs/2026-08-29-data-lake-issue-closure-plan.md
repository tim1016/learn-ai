# Data-lake issue closure plan

**Date:** 2026-08-29
**Scope:** the nine open issues under PRD [#1825](https://github.com/tim1016/learn-ai/issues/1825) — #1840, #1856, #1859, #1860, #1861, #1862, #1869, #1870, and the parent itself.
**Out of scope:** #1846 (TradingView CSV overlay into Data Lab) — spun out of the shell-nav redesign, no dependency on lake work, owned elsewhere.
**Authority:** `docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md`, ADR 0001, `.claude/rules/numerical-rigor.md`, `.claude/rules/temporal-rigor.md`.

The goal of this document is not "a list of fixes". It is to stop the pattern where closing one issue in this family opens another.

---

## 1. Why the last round didn't converge

Three distinct mechanisms, all verified against current `master` (`da1c37d9`).

### 1.1 The lake is a linear chain of gates, and each issue is one gate

`app/lean_sidecar/lake_mount.py::resolve_lake_artifacts` refuses in a fixed order. Each gate is only reachable once every gate above it passes:

| # | Gate | Site |
|---|---|---|
| 1 | `lake_mount_not_configured` / `lake_read_only_mount` | `lake_mount.py:215` |
| 2 | `lake_root_mode` | `:282` |
| 3 | `lake_incomplete_trade_coverage` | `:303` |
| 4 | `lake_incomplete_quote_coverage` | `:329` |
| 5 | `lake_missing_daily_artifact` | `:426`, `:434` |
| 6 | `lake_missing_required_metadata` | `:490` |
| 7 | interest-rate subtree divergence — a **silent warning**, not a gate | `:370-391` |

The code already knows this. `lake_mount.py:325` carries a comment reading *"the next call here will raise `lake_missing_daily_artifact`"* — written by whoever fixed gate 4, describing gate 5, which was then filed as **#1869**. #1859 is the thing past gate 6.

**Consequence:** fixing gate *N* advances the operator to gate *N+1*, which gets discovered in review and filed as a fresh issue. Closing these one at a time cannot converge. The scenario is only done when it runs clean through all seven.

### 1.2 Three issues carry acceptance criteria that are factually wrong or unachievable

Closing an issue against a wrong AC is how a "closed" issue becomes a reopened one.

- **#1840** — its tombstone test ("nothing in the codebase resolves data roots outside the lake") **fails today** on three surfaces the issue never mentions: `app/routers/spec_strategy.py:116-133`, `app/research/runs/ledger.py:90-100`, `app/research/ml/generate_prediction_set.py:140-158`. Each does its own `LEAN_DATA_ROOT` env walk. Separately, ADR 0049 Decision 2 places a **hard obligation** on this slice that its AC omits entirely: the legacy `provenance/<symbol>.json` documents must be migrated into the lake's tree **before** the policy tree is deleted, or Postgres becomes the sole custodian of the audit trail — the exact arrangement ADR 0001 forbids.
- **#1860** — its title says the receipt must be built ".NET side". ADR 0049 Decision 3b, which spawned it, says the opposite: *"whether that is finally populating `data_lake_runs` from Python **or** a different durable receipt a later slice chooses."* The issue is narrower than its own authority.
- **#1869** — its fix section is correct, but `lake_mount.py:326-328` (which the issue quotes as context) states the daily rollup requires "a provider call, since the daily artifact is not derivable from per-day trade zips alone." **This is false.** `_process_daily_trade_artifact` derives daily bars by reading minute zips already on disk (`ensure_data.py:1382` → `_read_minute_trade_bars`, a pure `zipfile` read). After a backfill those rows are catalog cache hits. The rollup costs **zero** provider calls.

### 1.3 The carry-forward ledger was not durable

Issues #1859, #1860, #1861, #1862 each cite "#1839's carry-forward item" A3(a), B14, B12, A8. That ledger exists **nowhere** — not in the repo, not on issue #1839, not in PR #1868's body. It lived in a session scratchpad that is gone.

So there is no way to prove the four filed issues were the *only* carry-forward items. Un-filed items in that ledger are, by construction, the next crop of surprise issues.

---

## 2. Three rules that prevent recurrence

These are the operative part of this plan. Everything in §4 is downstream of them.

**Rule 1 — Correct the acceptance criteria before writing any code.**
The AC edits in §5 land as issue edits/comments *first*, in one pass, before the first branch. An issue whose AC is wrong cannot be reliably closed no matter how good the code is.

**Rule 2 — Every gate fix must name the next gate.**
The chain in §1.1 is fully knowable by reading `resolve_lake_artifacts` top to bottom. Any PR that fixes gate *N* states in its description what gate *N+1* is and whether it is in scope. A PR that silently advances the operator to an unnamed next failure is not done.

**Rule 3 — No issue closes on a test that skips in CI.**
59 Postgres-gated test functions (67 collected items) skip in CI today because the `Python Tests` job sets no `POSTGRES_URL`. Those are precisely the tests that would prove #1861 and #1870 correct. This is why #1862 goes first, not last.

**Rule 4 (process, not code) — carry-forward items live on the parent PRD issue.**
Any item deferred out of a PR in this family goes on #1825 as a checklist line in the same action that defers it. Never in a session scratchpad.

---

## 3. Verified dependency graph

Arrows mean "must land before". Every edge below is evidence-backed, not inferred from issue text.

```
#1862 (CI Postgres) ──┬──> #1861 (data_root_id)
                      └──> #1870 (daily rebuild)   [proof, not function]

#1870 (daily rebuild) ──> #1869 (backfill rollup)  [HARD: see 3.1]

#1859 (interest-rate) ──> launcher /extract-metadata contract change [external]

#1861, #1869, #1870, #1859 ──> #1840 (retire policy store)
ADR 0049 D2 provenance migration ──> #1840  [HARD]

#1856 (JobsService hook) — independent of all of the above
```

### 3.1 The hard edge: fixing #1869 alone *causes* #1870

This is not a risk assessment. It is a straight-line data dependency:

1. `expand_required_artifacts` derives its sessions **only** from `[spec.start_trading_date, spec.end_trading_date]` (`ensure_data.py:313-317`).
2. Pass 1 populates the daily artifact's source map exclusively from that list (`ensure_data.py:1574`, inside `for identity in required:`). There is no catalog query for the symbol's other minute artifacts. **The source set is window-scoped by construction.**
3. `_daily_dch(source_ids, source_shas, mode)` (`:240-255`) therefore **changes whenever the window changes**.
4. But the catalog identity it is stored under is **windowless** — `ON CONFLICT ("Market","Symbol","Resolution","DataType","Provider","PriceAdjustmentMode") ... DO NOTHING` (`catalog_client.py:826-830`), no date column.
5. Second window ⇒ `claim_aggregated_bar_artifact` returns `None` ⇒ hash comparison fails at `ensure_data.py:1341` ⇒ `data_contract_mismatch` at `:1354` ⇒ **early return before `atomic_write_and_promote` at `:1403`**. The zip on disk still spans window A only.

**Net:** ship #1869 alone and today's *first-run-only* failure becomes a *silently stale daily zip on every subsequent window*, surfacing as `lake_daily_artifact_does_not_cover_window` — the exact error #1869 exists to eliminate. `materialize_engine_run` then **hides it** from minute-resolution runs (`run_materialization.py:345-349`), so it degrades into a wrong number rather than a refusal.

The bug is already asserted by an existing test: `tests/unit/data_lake/test_run_materialization.py:877-900`, `test_a_second_window_leaves_the_daily_artifact_contract_mismatched`. That test must be **inverted**, not deleted.

### 3.2 The remedy #1870's error message names does not exist

`data_contract_mismatch`'s detail string tells the operator to "re-run with `force_refresh=True`" (`ensure_data.py:1358`). Verified repo-wide:

- `DataRunSpec.force_refresh` is declared at `types.py:154` and **read nowhere in Python** (`git grep "\.force_refresh"` over `PythonDataService/**` → zero results).
- There is **no aggregated-bar rebuild primitive at all**. `refresh_complete_minute_bar` (`catalog_client.py:852`) is minute-only and has **no production caller** — only two test call sites.
- `DataAvailabilityResult.refreshed_artifact_count` is hardcoded `0` (`ensure_data.py:1707`).
- The Data Lake Observatory ships a **live "Force refresh" checkbox** — signal at `lake-backfill-panel.component.ts:75`, handler `:177-179`, wired through `:198`, the OpenAPI contract, and the Pydantic model — and it is **silently discarded**. The operator's advertised remedy is a no-op control.

This is an operator-facing correctness bug in its own right and is folded into #1870's corrected scope (§5).

### 3.3 #1862 needs dotnet, not just Postgres

The lake schema is created **exclusively** by .NET EF Core migrations. There is no Python DDL, no Alembic, no migration runner Python can call (`catalog_schema.py` is a read-only *mirror*, explicitly). So the `Python Tests` job needs `postgres:16` **plus** `actions/setup-dotnet` **plus** a migrate step.

Copy-ready precedent exists: the `Backend Tests` job already does exactly this (`.github/workflows/ci.yml:147-173`), and `Backend.Tests/Helpers/PostgresIntegrationTestDatabase.cs:72` already calls `DatabaseInitializer.MigrateAsync`.

Second, compounding finding: `tests/integration` is **not in the CI baseline at all** (`PYTHON_FAST_BASELINE_TEST_DIRS`, `ci.yml:271-274`). Setting `POSTGRES_URL` alone recovers 55 of the 67 items; the remaining 12 also need the baseline dirs widened.

---

## 4. The convergence test

The family is not closed by nine green checkboxes. It is closed by **one scenario running clean end to end**, exercising all seven gates in §1.1 plus the second-window path in §3.1:

> **Scenario.** An operator with a `cache_import`'d lake runs a flag-on LEAN sidecar backtest over window A, then runs the same symbol over a **different** window B.
>
> **Expected:** both runs materialize and complete. Zero provider calls on covered days. No `lake_*` refusal. No silent input divergence warning. The daily artifact covers whichever window is being read.

This scenario becomes a committed integration test in `tests/integration/data_lake/`. Per Rule 3 it must run in CI, which is why #1862 precedes it.

It lands in two stages, because gate 7 is a warning rather than a refusal:

- **Batch 2** adds the test with the gate 1–6 assertions and the second-window assertion. That is the joint acceptance criterion for **#1869 and #1870** — neither is closeable without it green.
- **Batch 4** adds the final assertion (no silent input-divergence warning), which is the acceptance criterion for **#1859**.

**Any of those three that can be "closed" without its stage of this test going green is not actually closed.**

---

## 5. Work orders

Each batch is one PR unless stated. Per `CLAUDE.md`, the `thermo-nuclear-code-quality-review` skill runs once before the first push of each PR, and project-scope lint plus the relevant test surface must be green.

### Batch 0 — Correct the acceptance criteria (no code)

One pass of issue edits, before any branch. Nothing here touches the tree.

| Issue | Edit |
|---|---|
| #1840 | Add the ADR 0049 D2 provenance-migration obligation as a blocking AC. Add the three unconverted root-resolver surfaces and change the tombstone AC to "ships with an explicit allowlist naming them plus a tracking issue". Note that `policy_store.py` must be **split**, not deleted. |
| #1860 | Retitle away from ".NET side" — the receipt is Python-written (decision below). Cite ADR 0049 D3b as the permission. |
| #1869 | Correct the "requires a provider call" claim; the rollup is a pure on-disk aggregation. Add "blocked by #1870" with the §3.1 reasoning. |
| #1870 | Add the dead `force_refresh` field and the inert Observatory checkbox to scope. |
| #1862 | Add the dotnet + EF-migrate requirement and the `PYTHON_FAST_BASELINE_TEST_DIRS` widening. |
| #1825 | Add a durable carry-forward checklist section (Rule 4). Record that #1839's ledger was lost. |
| #1859 | Add the launcher `/extract-metadata` two-field contract as a named external blocker. |

### Batch 1 — #1862: CI Postgres (verification foundation)

`postgres:16` service + `actions/setup-dotnet` + migrate step on the `Python Tests` job; widen `PYTHON_FAST_BASELINE_TEST_DIRS` to include `tests/integration`. Measure and report wall-time delta against the CI-levers ledger (#1815 history) — this repo gets 2 vCPU on `ubuntu-latest`.

**Done when:** the 67 previously-skipped items execute in CI, and the run's wall time is recorded in the PR body.

### Batch 2 — #1870 + #1869 as ONE PR: the daily-artifact chain

**One PR by default.** The §4 scenario is the proof for both, and the intermediate state (#1869 landed, #1870 not) is strictly worse than either endpoint — it converts a first-run-only refusal into a silently stale artifact, per §3.1. Split into a stacked pair only if the PR proves too large for one thermo review, and in that order: #1870 first, #1869 second, never the reverse.

- Give the daily artifact a real rebuild path — either make its `DataContractHash` range-independent, or add `refresh_complete_aggregated_bar` (modelled on `refresh_complete_minute_bar:852-905`) and wire `force_refresh` through.
- Resolve the inert `force_refresh` field: wire it or delete it and remove the UI control. Do not leave an operator-facing no-op.
- Add the full-range daily rollup after `run_backfill`'s per-day loop (`backfill.py:516`), fulfilling its own docstring.
- Correct `lake_mount.py:321-337`'s stale comment and its false "provider call" claim.
- Invert `test_run_materialization.py:877-900`.
- Land the §4 convergence test.

### Batch 3 — #1856: JobsService per-job event hook (Frontend, parallelisable)

Fully independent; can run concurrently with any other batch. `onEvent(jobId, handler)` fanning out inside `applyEvent` **before** its `!prev` early return, returning an unsubscribe, with the handler typed on the open `{ type: string }` shape (both consumers handle domain events outside `JobEventType`'s 7-member union).

**Watch item:** `run-session.service.spec.ts`'s `ControllableEventSource` stub captures the *last constructed* EventSource. Removing run-session's own source makes it capture JobsService's instead — the helper needs rewriting even where assertions don't. And `jobs.service.spec.ts` has **no EventSource stub at all** today, so the hook ships with new coverage there.

### Batch 4 — #1859: interest-rate subtree

Blocked on a launcher-side change: `/extract-metadata` returns exactly two byte fields and needs a third, plus a third metadata artifact kind in the catalog vocabulary. The bytes are obtainable from the pinned image — four copies already exist under `PythonDataService/artifacts/lean-sidecar/metadata-*/` — so no external fetch is needed, but they are un-catalogued.

### Batch 5 — #1861: `data_root_id` / mode-in-path redesign

Requires Batch 1. Touches 40 catalog columns across 2 tables, 14 `catalog_client` functions, 21 call sites in `app/`, plus a new EF migration and the `catalog_schema.py` mirror in the same PR (`test_schema_drift.py` goes red by design if they diverge).

Also carries the deferred **wire-date body fields**: `DataRunSpec.start_trading_date`/`end_trading_date` remain `date`-typed on two POST bodies (`/ensure-data`, `/backfill`) — a live temporal-rigor deviation, self-documented as deliberate at `routers/data_lake.py:296-304`.

**Stale premise to correct:** #1861's body assumes a live root marker. `.cache_import_adjustment_mode` was **deleted by #1839** (`path_policy.py:86-92`); the only surviving reference is a legacy reader in `scripts/migrate_lake_to_mode_roots.py`.

### Batch 6 — #1860: durable run receipt (Python-written)

Decision taken: **Python owns the write path.** Python is the sole lake writer (ADR 0049 D1, spec §2.1) and materialization is in-process Python (D3). Permission comes from ADR 0049 D3b.

**Keep the .NET EF model.** `AppDbContext.cs:580-582` declares the fluent config "the authoritative EF model state used by the schema-drift test on the Python side". Deleting it would convert `test_schema_drift.py` from a two-way cross-stack pin into a self-referential one that still passes while testing nothing. Nothing in .NET gets deleted by this batch.

### Batch 7 — #1840: retire the policy store (last)

Preconditions: Batches 1–6, plus the ADR 0049 D2 provenance migration as its **first commit**.

- `policy_store.py` is **split, not deleted**: `resolve_data_roots()` (:106), `snapshot_minute_trade_zips()` (:147, feeds the golden reconciliation fixture via `lean_sidecar_service.py:604,744`), and the CodeQL-pinned path guards all survive and move.
- `availability.py` is split — `check_availability` survives (`engine_bars_service.py:28` imports it); `ensure_range` and `_missing_spans` go.
- `POST /api/engine/export-lean` (`routers/engine.py:786-843`) is the full-range export and is **not flag-gated**; removing it regenerates the OpenAPI contract and the Frontend type surface.
- The flag's 7 runtime read sites include `chart_service.py:1002`, where it is a **resample cache-key component** — removing it changes cache keys.
- `data-lake-observatory.component.html:19` renders the flag name to the operator and must be edited.
- Tombstone ships with the allowlist naming the three unconverted research surfaces plus a new tracking issue. `tests/unit/data_lake/test_no_lean_paths_outside_policy.py` already exists in embryo form — shrink its `ALLOWLISTED` tuple rather than writing a new test.

**Blast radius:** ~127 tests — ~33 deleted, ~19 rewritten, ~75 fixture edits. `tests/integration/data_lake/test_flag_flip_parity.py` (8 tests) is deleted entirely: with no flag there is nothing to flip.

### Finally — #1825

The parent PRD closes when Batches 1–7 close, with the carry-forward checklist (Rule 4) empty or migrated to new issues.

---

## 6. What this plan deliberately does not close

Named here so their absence is a decision, not an oversight discovered later.

- **ADR 0049 Decision 1b's zombie-writer race** — an expired-lease writer's `os.replace()` can clobber the winner's bytes after the catalog marked the row complete under the new owner's hash. Named in the ADR, unfixed, not in this family's scope. Would need the ADR 0048 fencing-generation shape.
- **`check_availability`'s weekday-only walk** (`availability.py:55`) — a holiday window still reports incomplete on every call. Costs zero provider calls post-#1830, so it is cosmetic until the store is retired, at which point it disappears with it.
- **The provider-licensing gate** (ADR 0049 D4) — a human task, still open, still not assumed passed.
- **#1846** — out of this lane.

## 7. Residual risks

1. **The lost #1839 ledger (§1.3).** The largest unquantifiable risk. Mitigation: Rule 4 going forward, and treating the §4 convergence test — not the issue list — as the completeness check.
2. **`ledger.py`'s run fingerprints.** If a future round converts `research/runs/ledger.py:90-100`, `resolve_data_root_revision()` changes and already-recorded research run identities are invalidated. That is a data-migration problem, and it must not be smuggled inside a cleanup PR. The Batch 7 allowlist exists specifically to keep it out.
3. **Migrations-snapshot contention.** Batch 5 (#1861) definitely adds an EF migration for `data_root_id`. Batch 6 (#1860) needs one **only if** the receipt does not fit `DataLakeRuns`' existing 14 columns — check that first, since a fitting receipt makes Batch 6 pure Python. If both need one, they must not be in flight simultaneously: `AppDbContextModelSnapshot.cs` will conflict, and a mis-resolved snapshot conflict produces a migration that applies cleanly and diverges silently from the model.
4. **Concurrent sessions.** This checkout is shared. Work in a dedicated worktree; verify a worktree's tests on the host, not via `my-frontend`/`polygon-data-service`, which read the main checkout.
