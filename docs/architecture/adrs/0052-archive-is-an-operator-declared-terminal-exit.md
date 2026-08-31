# ADR 0052 — Archive is an operator-declared terminal exit, and terminal rows leave the read path

**Status:** Accepted 2026-08-31
**Provenance:** Decision ticket [#1911](https://github.com/tim1016/learn-ai/issues/1911). Source: `docs/audits/read-latency-profile-live-2026-08-31.md` §13 — a profiling session ended with 142 stopped, flat bots that `retire` refused, because `STRATEGY_STILL_RUNNABLE` is correct for every one of them.
**Decision drivers:** #1801 measured read *and* deploy cost as linear in roster rows (~2.9 ms/row live; deploy 6.3× from 53 → 144 rows), and roster rows only ever accumulate. The 2026-08-26 fleet-stress run described its 94 leftover rows as "legacy roster rows retained deliberately as read-scale ballast" — i.e. the baseline was 94 rows before a single bot was deployed that day.
**Vocabulary:** `CONTEXT.md` § "Registration exit (resolved 2026-08-31)" — the two exits, what proves each, and the inert-terminal test the read path keys on.
**Related:** #1795 (Retire clears a *provably dead* registration — untouched by this ADR), #1778 (a retired bot holding stranded exposure keeps its authored cure), ADR 0051 (cohort-scoped flatten — the affordance shape a cohort archive should follow), #1776 (reads project the sweep's verdict; no second reconciler), #1801 (the cost curve this reduces).

## Context

Three findings, in the order they were established.

**1. `retire` never reached the authority.** `bot_runner.retire` wrote the file lifecycle record, and nothing anywhere wrote `strategy_instances.retired_at_ms` — no `UPDATE`, no transition kind, no fold; the only `INSERT` hardcodes `NULL`. So the V2 catalog, which derives phase from that column, rendered a retired bot as `OFF_DUTY`, and `_authority_phase` read the same `NULL` and projected `clear_retirement=True`, silently un-retiring the bot on the next routine refresh. The one test that produced a `RETIRED` roster row returned `retired_at_ms` from a literal dict, which proved the column mapping and never the write. **Fixed** — see the `STRATEGY_INSTANCE_RETIRED` commit on this branch.

**2. Retirement did not reduce read cost.** Even a correctly retired row was projected in full — a custody projection and a lifecycle file read per row, per poll. **Fixed** — see the inert-terminal commit on this branch.

**3. A healthy stopped bot still could not be removed.** #1795's Retire contract is correct for what it covers and is not re-litigated here; it explicitly deferred "a healthy stopped bot… a destructive lifecycle action with its own safety story". This ADR is that story, and `archive` is its implementation.

## Decision

### 1. `archive` is a new action id, not a change to `retire`

#1795's guard, copy and contract stay exactly as they are. `archive` is the separate, confirmation-gated action for a registration the operator is finished with, and its guard proves what retirement's custody guards prove: not running, not already `RETIRED`, no attributed exposure, no working orders, and no account freeze making flatness unprovable.

### 2. It commits the same terminal phase

`archive` writes the same `STRATEGY_INSTANCE_RETIRED` transition, so one terminal phase carries both meanings and the read-path work above covers archived rows without a second case. `run_admission.py:265` already refuses `BOT_RETIRED` and `cutover_roster.py:233` already treats `RETIRED` as quiescent, so the enforcement is structural, not presentational. `updated_by` / `operator_reason` keep the two apart in the audit trail.

### 3. Re-verified at commit, like retire

The presented decision is always older than the click, so `bot_runner.archive` re-answers its guard against a freshly reconciled custody snapshot under the same `_operation_lock` before writing. A fill that landed in between refuses the command rather than being stranded by it.

### 4. A cohort archive follows ADR 0051's shape

Membership is explicit in the request, never inferred at execution time; each leg is the unchanged per-bot `archive` with its own concurrency token and idempotency identity; the presentation is backend-authored and carries each leg's real executability facts. 142 bots is why the affordance exists; ADR 0051 is why it must be N legs behind one affordance rather than an account-level operation.

### 5. Retiring resolves the last run's unclean exit

A terminal row carries `duty_outcome=None` rather than its last run's outcome. Retiring is an operator stating they have dealt with this registration. Live custody is what can still need attention on a retired bot, and that reaches the row through the custody projection — which is why #1778's stranded-retired cure survives the cheap read path.

## Consequences

Read cost becomes linear in *live* rows. Measured on `scripts/bench_panel_read_latency --retired-fraction` at 144 rows: sequential catalog p50 **32.67 ms → 19.37 ms** and 6-concurrent p50 **473.34 ms → 259.55 ms** when half the roster is retired.

The cohort form ships with an operator surface rather than waiting on one: cohort-flatten landed backend-only under ADR 0051 and its UI is still open as #1909, so a second unclicked batch endpoint would have left #1911's motivating 142 bots exactly as unreachable as before. The batch execution taxonomy both cohorts run under now lives in one module (`cohort_execution`), so the contract an operator depends on when a leg fails halfway through a fleet-wide command has one home rather than two that can drift.

**Not decided here, and deliberately so.** The catalog runs N per-bot custody projections where one account-wide projection holds the same rows. Collapsing them would remove the row-count curve for *all* rows rather than only terminal ones — but the account-wide projection applies its limits account-wide, so per-bot slices could be silently truncated. That is a correctness hazard and belongs to #1801's follow-up with its own PR, not to this one.
