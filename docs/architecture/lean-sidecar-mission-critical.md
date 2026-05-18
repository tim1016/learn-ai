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

### D3 — LEAN-Lab-vs-Engine-Lab reconciler scope (Phase 5g)

Phase 5a is **self-reconciliation** (LEAN's recorded fees vs the canonical
IbkrEquityCommissionModel). Phase 5g would be **cross-engine reconciliation**
(LEAN trades vs our Engine Lab's trades on the same algorithm + inputs).

**Defer the entire Phase 5g design.** The qc_reconciler taxonomy in
`PythonDataService/app/research/parity/qc_reconciler.py` is the right
shape, but extending it to LEAN-Lab-vs-Engine-Lab requires deciding:
- which divergence categories are gating vs informational for *this* pairing
- whether the engine-lab side runs the same algorithm verbatim or a port
- how to handle algorithms that have no engine-lab equivalent yet

Tim must scope Phase 5g before any code lands.

### D4 — Phase 5 → Phase 6 transition

Phase 6 is "persistence model for run metadata (file-backed vs DB)". The
sidebar/index currently does scan-on-demand and works fine at ≤200 runs.

**Defer if** a tick wants to add a database dependency (sqlite/postgres),
introduce SQLAlchemy/asyncpg, or migrate run metadata into a DB. Tim
must decide the trigger for Phase 6 — likely "we hit 1000 runs and
scan latency exceeds 500ms".

### D5 — Real quote data source (replace Phase 5c synthetic zero-spread)

Phase 5c stages bid=ask=trade-close with size 0. That's synthetic and
unsafe for algorithms that consume quotes.

**Defer if** a tick wants to fetch real NBBO from Polygon (or any other
vendor). That's a vendor-cost decision (Polygon's NBBO endpoint may
have separate billing) and a freshness decision (NBBO is millisecond-
granular real-time data).

### D6 — Algorithm class-name configurability

`MyAlgorithm` is hardcoded as the only acceptable class name in
buy_and_hold.py, buy_and_hold_reconciliation.py, the API validation,
and the LeanConfig.algorithm_type_name default.

**Defer if** a reviewer or a tick wants to make this configurable (let
operators paste an algorithm whose class is `MyCustomAlgo` and have
the launcher pass `algorithm-type-name=MyCustomAlgo`). Surface-area
expansion that needs Tim's UX call.

### D7 — Frontend equity-chart spec flake — "rerun forever" vs "rewrite"

`Frontend/src/app/components/lean-lab/lean-lab-equity-chart/lean-lab-equity-chart.component.spec.ts`
flakes intermittently with `vi.fn() called 0 times` due to Vitest's
worker module cache + `vi.mock("lightweight-charts", ...)` ordering.
We've layered a defensive vi.mock in `lean-lab.component.spec.ts`; it
mostly works. Reruns succeed.

**Defer if** a tick wants to delete or substantially rewrite the equity-
chart spec. That's a "do we trust the workaround or invest in a real
fix" decision Tim owns.

### D8 — CodeRabbit "Major" findings policy

Memory rule: P1 findings block merges. CodeRabbit emits Major/Minor/Nitpick.

**Defer if** a Major finding is on `*.py`/`*.ts` code (not docs/style).
Decline-with-reply is fine for Minor/Nitpick on style. Tim has
historically split: "P1 from Codex" = always fix, "CodeRabbit Major" =
case-by-case judgement. Default to fix-it-or-defer for Major.

### D9 — New `requirements-dev.txt` entries

A reviewer asking for `pytest-mock` or `factory-boy` etc. is fine to add
if it's already widely used in the Python ecosystem AND the version is
pinned.

**Defer if** the suggested dep is exotic (under 100 stars on GitHub), or
if the test it enables could be written without it.

### D10 — Reconciler endpoint output schema change

`POST /api/lean-sidecar/runs/{id}/reconcile` returns `RunReconciliationReportModel`
with specific fields. The Phase 5a UI consumes it.

**Defer if** a tick wants to add/remove top-level fields on the response.
That's a wire-format change that needs Tim's UX read (and frontend test
updates).

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
