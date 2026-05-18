# LEAN Sidecar Lab — mission-critical decisions

**Read this file FIRST in every autonomous tick** (cron, agent, or otherwise).

This file enumerates every decision the human owner (Tim) has explicitly
NOT delegated. If a tick's plan would trigger any of the **defer** conditions
below, the tick must STOP, surface the question, and do nothing else.

When in doubt: skip the work, never guess on Tim's behalf.

---

## Hard limits per tick

These bound how much damage a runaway autonomous tick can do.

1. **One new PR per tick max.** Never open multiple feature PRs in a single
   tick — let the queue drain through the merger watcher before adding more
   work in flight.
2. **No master commits.** Always branch → PR → wait. Never `git push
   origin master`.
3. **No `.claude/rules/*` edits.** Those are Tim's contract; any change is
   a defer.
4. **No new runtime dependencies.** Anything that adds a line to
   `PythonDataService/requirements-*.txt` or `Frontend/package.json` is a
   defer. (Test-only / dev deps in `requirements-dev.txt` are fine if a
   reviewer asks for them, but pin the version.)
5. **No destructive git ops without an in-doc opt-in.** `git push --force`,
   `git reset --hard origin/main`, `gh pr close`, `git branch -D` of an
   unmerged branch — all defer unless this doc has an explicit "OK for
   the night-shift driver" line for that op.
6. **No merging your own PRs without CI green + zero open P1 threads.** A
   single CodeRabbit "Major" finding on a `*.py`/`*.ts` line is a defer.
   Pure-stylistic Minor findings may be declined with a brief reply +
   thread resolve.
7. **Three-strike rule.** If three consecutive ticks open a PR that fails
   its own CI on first run (not a flake — actual code failure), stop
   opening new PRs and only run Part A (merge) until a human resumes.

---

## Decisions Tim owns — defer until he says otherwise

### D1 — Multi-symbol `bars_consumed_by_symbol` schema

Current state: `observations.csv` has one row per bar, header `ms_utc,close`,
implicitly single-symbol. `_count_bars_consumed` returns `{<request.symbol>: <count>}`.

**Defer if** a tick wants to extend observations.csv to multi-symbol
(adding a `symbol` column, or splitting into per-symbol files). This is
a manifest-schema bump (`MANIFEST_SCHEMA_VERSION`), which invalidates
existing fixtures. Tim must approve schema bumps explicitly.

### D2 — Determinism-gate tolerance for which fields may differ

A determinism test reruns the trusted sample twice with identical inputs.
**Mandatory differences:** `started_at_ms`, `finished_at_ms`, `duration_ms`,
the launcher log's wall-clock timestamps. **Mandatory equality:** every
hash in `staged_data.bar_zips`, the normalized `equity_curve` byte stream,
the `order_events` list serialized.

**Defer if** a tick finds anything else differs (e.g., a fill price by
$0.01, an extra equity point at the end). That's a real determinism bug
and needs Tim's read on whether to fix LEAN config or relax the gate.

### D3 — LEAN-Lab-vs-Engine-Lab reconciler scope (Phase 5g) — RESOLVED 2026-05-18

Phase 5a is **self-reconciliation** (LEAN's recorded fees vs the canonical
IbkrEquityCommissionModel). Phase 5g is **cross-engine reconciliation**
(LEAN trades vs our Engine Lab's trades on the same algorithm + inputs).

Tim's scope decisions:

- **Pairing**: caller-supplied. The request names which Engine Lab
  strategy class to diff against. No auto-pairing convention; explicit
  is safer than `lean_<algo>_port`-style auto-derivation.
- **Gating taxonomy**: **strict** — every category in
  `DivergenceCategory` is gating EXCEPT `COMMISSION_DRIFT`, which
  defaults to diagnostic. Reason: fee-model precision is a tunable
  comparison, not a strategy-logic claim.
- **Per-run override**: when the caller sends `assert_fees=true` AND
  the run was the reconciliation-grade template (Phase 5b — IBKR
  pinned on both sides), `COMMISSION_DRIFT` is also gating. Same
  Branch-A semantics as ``qc_reconciler.py``.
- **Data source**: shared staged data — Engine Lab runs against the
  same ``workspace/data`` zips LEAN saw, not its native fixtures.
  Maximum fidelity, requires the cross-engine reconciler to glue
  Engine Lab's data path to the LEAN workspace path.

Phase 5g is **unblocked**. Build slices:
1. Endpoint scaffold + Pydantic request/response shape (no engine-lab call yet).
2. Engine Lab cross-run primitive that accepts a workspace path.
3. Diff against ``DivergenceCategory``; honor ``assert_fees``.
4. Frontend UI (Phase 5g.4).

### D4 — Phase 5 → Phase 6 transition — RESOLVED 2026-05-18

Phase 6 is "persistence model for run metadata (file-backed vs DB)". The
sidebar/index currently does scan-on-demand and works fine at ≤200 runs.

Tim's trigger decisions:

- **Trigger**: use-case driven, not performance threshold. Phase 6
  begins **when someone needs cross-run queries** (e.g., "list all
  runs where commission_drift > $0.10", "show every reconciliation-
  template run from the last 7 days"). The scan-on-demand index is
  fine until the read pattern needs indexing.
- **Store**: SQLite file in the artifacts root. Single-writer, no
  daemon, no port — fits the current single-host deployment. No
  Postgres dependency until/unless multi-host becomes a thing.

**Defer if** a tick wants to introduce a DB without a concrete
cross-run query use case. "We might want this later" doesn't trigger
Phase 6.

### D5 — Real quote + factor/map data source — RESOLVED 2026-05-18

Phase 5c stages bid=ask=trade-close with size 0. Phase 1c stages
empty `factor_files/` + `map_files/` dirs.

Tim's decisions:

- **Quote source**: synthetic stays. Real NBBO is deferred until a
  real quote-consuming algorithm lands; vendor bandwidth + billing
  cost isn't worth absorbing speculatively.
- **Vendor pre-selection** (recorded for the eventual real-quote
  work): **Massive.com is the candidate vendor**, not Polygon.
  Tim's current Polygon Starter plan is functionally equivalent to
  free-tier and won't get us real NBBO. Massive offers stock
  + options pricing tiers:
  - https://massive.com/pricing
  - https://massive.com/pricing?product=options
  When the time comes, evaluate Massive's NBBO / options trade tier
  against the project's actual needs (granularity, history depth,
  cost).
- **Factor/map files**: synthetic stays. Reconciliation-grade work
  is bounded to **windows with no corporate actions** (i.e., dates
  without splits/dividends on the staged symbol). The trusted-sample
  Jan-2025 SPY window already meets this. Documented limit.

**Defer if** a tick wants to add ANY real-data vendor — including
Massive — without Tim's explicit "go". The vendor pre-selection
above is the direction, not authorization.

### D6 — Algorithm class-name configurability — RESOLVED 2026-05-18

`MyAlgorithm` is hardcoded as the only acceptable class name in
buy_and_hold.py, buy_and_hold_reconciliation.py, the API validation,
and the LeanConfig.algorithm_type_name default.

Tim's decision: **keep hardcoded**. Operators rename their class to
`MyAlgorithm`. The configurability surface isn't worth it; the
existing copy/paste-into-textarea UX already accommodates this
trivially.

**Defer if** a reviewer or a tick wants to make this configurable.
This is no longer something to revisit without a stronger UX case
than "convenience".

### D7 — Frontend equity-chart spec flake — RESOLVED 2026-05-18

The spec at
`Frontend/src/app/components/lean-lab/lean-lab-equity-chart/lean-lab-equity-chart.component.spec.ts`
was flaking on a Vitest worker module-cache ordering bug.

Tim's decision: **delete the spec entirely**. The chart component
renders correctly per the integration-level tests in
`lean-lab.component.spec.ts` (which already mocks lightweight-charts
defensively). The unit-level mock-call-count assertions were not
earning their keep against the flake cost.

This decision was implemented in this same PR — the file is removed.

**No defer condition remains.** Any future flake on lean-lab chart
behavior should be addressed through the integration tests, not by
re-adding a separate unit spec.

### D8 — CodeRabbit "Major" findings policy — RESOLVED 2026-05-18

Memory rule: P1 findings block merges. CodeRabbit emits Major/Minor/Nitpick.

Tim's decision: **case-by-case agent judgement**. P1 from Codex is
always-fix (memory rule unchanged). CodeRabbit Major: the agent
reads the finding, fixes or declines on its own, and only escalates
when the fix would require **substantial rework** (multi-file refactor,
test framework swap, etc.). Minor/Nitpick: decline-with-reply +
resolve.

**Defer if** a Major finding requires changes spanning >1 module
boundary or a structural rewrite. Default: try to fix.

### D9 — New `requirements-dev.txt` entries — RESOLVED 2026-05-18

Tim's decision: **allowed without asking** as long as the package is
widely-used in the Python ecosystem (>1k GitHub stars OR is a
maintained-by-foundation library like pytest plugins from the
pytest-dev org) AND the version is pinned (`==X.Y.Z` or
`>=X.Y,<X+1`).

This widens the prior "100-star" threshold per Tim's explicit OK.

**Defer if** the suggested dep is exotic (<1k stars, unmaintained,
or could be written in a one-file helper), or if it's a **runtime**
dep (not dev) — runtime deps still need Tim's explicit OK.

### D10 — Reconciler endpoint output schema change — RESOLVED 2026-05-18

`POST /api/lean-sidecar/runs/{id}/reconcile` returns `RunReconciliationReportModel`
with specific fields. The Phase 5a UI consumes it.

Tim's decision: **shape changes are allowed with an explicit
`schema_version: int` field on the response**. New optional fields,
field renames (with a deprecation cycle), even removed fields are
fair game — bump `schema_version` to make breakage detectable on
the consumer side. The Phase 5a UI must read `schema_version` and
fail-fast (red error pane) on unrecognized versions.

The current schema is implicitly v1. A future PR may add the
explicit `schema_version: 1` field; agents may make that addition
without deferring.

**Defer if** the proposed change is a SEMANTIC change (same field
name, different meaning). That always needs Tim's call regardless
of versioning.

---

## Allowed without asking

These are the green-lights the night-shift driver may take without
checking back in:

- Read any file in the repo.
- Run any `pytest`, `ruff`, or `npx ng test` invocation.
- Merge a PR that meets: CI all-green + mergeStateStatus CLEAN + zero
  unresolved P1/Major threads + ≥1 logical commit by you OR by Tim.
- Open a PR for any item explicitly cross-listed in the queued list (and
  not on a "defer" line above).
- Resolve trivial conflicts in `docs/architecture/phases/*` (per-phase
  files don't overlap) and in the ADR index (keep both bullet lines).
- Reply + resolve CodeRabbit Nitpick/Minor threads with a one-sentence
  reasoned decline.
- Re-run a single flaky CI job (`gh run rerun <id> --failed`) once per PR.

---

## How to surface a "defer" to Tim

Final output of the tick must be a single short paragraph naming:
1. Which `D<N>` condition fired
2. What the tick was about to do
3. What Tim needs to decide

Example:

> Defer D1 (multi-symbol schema). Tick was about to extend
> `observations.csv` to per-symbol columns to support the Phase 6
> multi-asset trusted sample. Tim needs to decide whether to bump
> MANIFEST_SCHEMA_VERSION now (which invalidates existing fixtures)
> or wait until a real multi-asset algorithm lands.

That's it. Don't try to be clever about the defer condition — just
surface and exit.
