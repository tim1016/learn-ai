# ADR 0052 — Archive is an operator-declared terminal exit, and terminal rows leave the read path

**Status:** Accepted 2026-08-31
**Provenance:** Decision ticket [#1911](https://github.com/tim1016/learn-ai/issues/1911). Source: `docs/audits/read-latency-profile-live-2026-08-31.md` §13 — a profiling session ended with 142 stopped, flat bots that `retire` refused, because `STRATEGY_STILL_RUNNABLE` is correct for every one of them.
**Decision drivers:** #1801 measured read *and* deploy cost as linear in roster rows (~2.9 ms/row live; deploy 6.3× from 53 → 144 rows), and roster rows only ever accumulate. The 2026-08-26 fleet-stress run described its 94 leftover rows as "legacy roster rows retained deliberately as read-scale ballast" — i.e. the baseline was 94 rows before a single bot was deployed that day.
**Vocabulary:** `CONTEXT.md` § "Registration exit (resolved 2026-08-31)" — the two exits, what proves each, and the inert-terminal test the read path keys on.
**Related:** #1795 (Retire cleared a *provably dead* registration — untouched by this ADR; Retire itself was removed by #2578, see the 2026-09-29 amendment), #1778 (a retired bot holding stranded exposure keeps its authored cure), ADR 0051 (cohort-scoped flatten — the affordance shape a cohort archive should follow), #1776 (reads project the sweep's verdict; no second reconciler), #1801 (the cost curve this reduces).

## Context

Three findings, in the order they were established.

**1. `retire` never reached the authority.** `bot_runner.retire` wrote the file lifecycle record, and nothing anywhere wrote `strategy_instances.retired_at_ms` — no `UPDATE`, no transition kind, no fold; the only `INSERT` hardcodes `NULL`. So the V2 catalog, which derives phase from that column, rendered a retired bot as `OFF_DUTY`, and `_authority_phase` read the same `NULL` and projected `clear_retirement=True`, silently un-retiring the bot on the next routine refresh. The one test that produced a `RETIRED` roster row returned `retired_at_ms` from a literal dict, which proved the column mapping and never the write. **Fixed** — see the `STRATEGY_INSTANCE_RETIRED` commit on this branch.

**2. Retirement did not reduce read cost.** Even a correctly retired row was projected in full — a custody projection and a lifecycle file read per row, per poll. **Fixed** — see the inert-terminal commit on this branch.

**3. A healthy stopped bot still could not be removed.** #1795's Retire contract is correct for what it covers and is not re-litigated here; it explicitly deferred "a healthy stopped bot… a destructive lifecycle action with its own safety story". This ADR is that story, and `archive` is its implementation.

## Decision

### 1. `archive` is a new action id, not a change to `retire`

Issue `#1795`'s guard, copy and contract stay exactly as they are. `archive` is the separate, confirmation-gated action for a registration the operator is finished with. Its guard requires: the process is not running **and** the durable phase has settled to `OFF_DUTY` (two different facts — a task that dies before its stop transition commits reads not-running while the authority still holds an ACTIVE run, and archiving there would stamp `retired_at_ms` on a run that never ended); the account can prove flatness; and there is no attributed exposure, no working order, and no unresolved effect.

The effect count is bot-scoped and visible only at commit — the panel has no such view — so an accepted effect can arm the button and still be refused on click. That asymmetry is the action's existing contract rather than a gap in it: the presented decision is always older than the click, and it fails in the safe direction.

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

## Amendment 2026-09-28 — the batch is "Clear" on Home (PRD #2560, #2567)

The owner made §4's batch the only way to take finished bots off Home: `POST /{broker}/accounts/{account_id}/bots/clear` names the bots (`BotClearRequest`: an idempotency key and the bot ids, never an action) and answers `CohortActionResult`, one leg per bot in request order. Each leg is this ADR's unchanged `archive` run through `cohort_execution` under `{key}:{sid}`, with the concurrency token its own panel presents.

Two refinements, both about how a refusal reads rather than what is refused:

- **No presentation read.** The Finished rows on Home are what the owner chose from, and each leg's executability is read when that leg is prepared; §3's commit-time re-proof is what decides. A presentation fetched first would only be older than both.
- **A commit-time refusal is typed.** `bot_runner.archive` refuses under the bot's lock before any write, so the panel performer now reports it as the guard's own `ActionNotAvailableError` (headline, why and cause code, key released) rather than the "outcome unknown" an untyped performer error becomes. A presented-but-disabled action likewise refuses with its first blocker's headline and condition code. `_ARCHIVE_REFUSAL` also gained the `BOT_DUTY_NOT_SETTLED` wording its lookup was missing, and a custody read whose counts are unknown (Alpaca unreadable) refuses as `ARCHIVE_CUSTODY_UNPROVABLE` instead of failing on the missing numbers.

§3's fresh custody is **one reconciliation pass per batch**, not one per leg. Per leg, clearing 150 bots made 600 Alpaca requests against its 200-a-minute allowance; a throttled pass is a stale one, which puts the account on hold for every running bot. The batch now reconciles once before its legs are prepared, and each leg's unchanged guard is answered under the bot's lock against that pass (or a later sweep's) with the bot's own orders, effects and fills read at that moment — but only while the pass still proves the bot's custody: no custody transition of that bot since the pass began (`SqliteAlpacaClerkFacade.reconciliation_covers`). That is sound because of what the guard admits: a bot not running, settled, flat, with nothing working or outstanding. The pass then saw every order the bot has, an order reaches Alpaca only after its effect's transition is written, and starting the bot again takes the lock the leg holds — so no fill can land for it unseen. A bot with any activity since the pass, a Dry Run in its own ledger, or a batch whose pass raised, reconciles for itself exactly as a single archive does. A same-key resend runs one pass too.

A cleared (archived, inert terminal) bot is no longer returned by the catalog at all, and a cleared Dry Run's sealed simulator is no longer opened by the catalog poll; every record it wrote stays readable by id. `retire` (#1795) is unchanged and no longer offered by the UI.


## Amendment 2026-09-29 — Retire is removed; archive is the one exit (#2578)

With Clear the only way off Home (#2567), `retire` (#1795) and everything that existed only for it are deleted: its policy, guard, confirmation and `evaluate_retirement`, the `retire` action id and copy, the `_retire` performer and `bot_runner.retire`, and the symbol-validity chain behind its second proof (the store, the reconciliation sweep's post-pass probe and its roster-symbols wiring). The panel no longer presents `retire`. Its id survives only in `RetiredActionId`, so a bot's receipt ledger written before the removal still reads as history. References to retire in the sections above are the decision's history; the terminal phase, `STRATEGY_INSTANCE_RETIRED` and `retired_at_ms` are archive's and are unchanged.

Retire admitted one registration archive refuses: a bot that is provably dead but whose last run never settled (§1's not-running-but-not-`OFF_DUTY` window). **Owner decision 2026-09-29: remove the dead machinery and add no new settle path.** Such a bot stays on Home, and Clear refuses it `BOT_DUTY_NOT_SETTLED`, until recovery that already exists settles its run — the boot scan a restart runs, or the lease-revival repair (ADR 0050). Pinned by `test_bot_clear.py::test_a_dead_bot_whose_run_never_settled_stays_on_home_until_recovery_settles_it`.

**Amendment 2026-09-30 (#2589): the reconciliation sweep settles a provably dead run.** The owner chose option (b) of #2578: each sweep pass hands the runs it retired because their in-process runner is gone (#2369) to the runner, which settles each with the boot scan's own repair — `EXITED_UNVERIFIED` / `INTERRUPTED_BY_RUNNER_GONE` — with no restart. A run whose process still lives is never settled, whatever the sweep hands over. A dead bot sealed on a foreign `shadow:` account — a rehearsal binding after its live account graduated — is settled through the sealed account's own authority, opened the way Dry Run restoration opens a `sim:` account; the installed authority writes no duty state for it (ADR 0050's posture holds). Clearing such a bot still waits: archive's enabling proof *is* custody, and proving a graduated rehearsal bot holds nothing needs the sealed account's own evidence, which a graduated lane cannot re-read — the live account now carries the lane's own orders, so the shadow composition's namespace check refuses by design. Archive answers `ARCHIVE_SEALED_ACCOUNT_CUSTODY` with an owner sentence instead of failing on an instance the installed Clerk has never seen; taking a shadow-sealed bot the rest of the way off Home needs that recorded-evidence custody projection, which remains open (#2589).
