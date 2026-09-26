# ADR 0045 — Exposure lifecycle closure: recovery flatten executor, EXIT refusal taxonomy, stuck-EXIT watchdog

**Status:** Accepted 2026-08-24
**Provenance:** Authored with the exposure-lifecycle-closure implementation (PRD #1752, PRs #1763/#1765/#1768 and this PR), from `docs/superpowers/plans/2026-08-24-exposure-lifecycle-closure.md`; spec: `docs/audits/strategy-execution-research-directions-2026-08-24.md` Direction 1. Supersedes [ADR 0010](0010-operator-action-contract-flatten-pause-stop.md) for the active Alpaca/SQLite control plane.
**Decision drivers:** F18/F19 (ops study 2026-08-24 §8–§9); the "correct mechanism exists, unwired" failure mode named by the same-day research directions.
**Related:** ADR 0035 (SQLite sole Alpaca custody authority), ADR 0038 (Alpaca sole bot control plane), ADR 0041 (generated Button Reference), ADR 0010 (superseded).
**Vocabulary:** `CONTEXT.md` § "Exposure lifecycle closure" — Recovery EXIT, Safe flatten, Redrive, `EXIT_STUCK`.

## Context

When a bot crashed or was stopped while holding a position, the operator had no in-platform way to make that position flat. Stop cancels working entries and deliberately leaves attributed exposure; every recovery pointer dead-ended (the safe-flatten plan was built but nothing executed it, the panel presented only Resume, carryover was globally disabled, manual tickets were unqualified). Two adjacent failures compounded it: a reducing order that lands terminal-but-not-flat (`EXIT_NOT_FLAT`) was never retried and had no age watchdog, so a stuck exit was silently permanent; and a *retryable* Clerk refusal on the EXIT path (stale broker snapshot during same-clock cohort exits) escalated to a fatal bot crash instead of a deferral. At fleet scale, every crash-with-exposure demanded out-of-band broker intervention (F18), and cohort exits fired the retryable-refusal crash routinely (F19).

## Decision

1. **Flatten executor = a recovery-catalog action, not the `flatten_stop` performer.** `execute_safe_flatten` consumes the already-built versioned `SafeFlattenPlan` and drives reduction-only recovery EXITs to flat through the existing claimed-broker-IO EXIT machine (`resolve_exit` → per-op claim CAS → `ClaimedBrokerIO`). Presentation and dispatch come free from the recovery surface; the frozen `live_instances.py` router is untouched. The legacy `flatten_stop` performer is **not** used: it carries two latent defects proven in the tree — its `accept_exit` runs `require_active_run`, but a crashed bot has no active run and `_flatten_stop`'s own first step (`registry.stop`) retires the run its second step then requires; and its `decision_id=f"panel-flatten:{…}"` violates the colon-free id grammar `accept_exit` enforces (`reject_colon`), so it would fail before broker contact even on a running bot. `flatten_stop`, `pause`, `continue`, `retire` remain unpresented dead vocabulary whose fate belongs to Direction 5's one-lifecycle-surface work.
2. **Recovery EXITs are run-fence-exempt but exposure-anchored and reduction-only.** `accept_recovery_exit` binds the EXIT's `run_id` to the run recorded on the originating entry's effect operation (not to a live run, which `runtime.recover()` retires on restart). Admission is owned by the SafeFlattenPlan gates or the stuck-EXIT watchdog policy, plus the downstream `require_capability(Capability.REDUCE, …)`, which authorizes only movement toward zero per leg. The no-active-run fact is enforced twice: `RUN_STILL_ACTIVE` at presentation/recheck, and again inside the capture transaction (`accept_recovery_exit(forbid_active_run=True)` raising `RecoveryRunActiveError`) so a Resume landing between recheck and capture fails closed. Execution is gated to a single strategy-owned leg; account-wide and manual-custody (NULL-strategy) plans stay prepare-only, so `execute_safe_flatten` never advertises an action the executor cannot deliver. Each leg's outcome is classified by the EXIT's terminal state — a broker-rejected reduction (folded `failed`, order row present) is an honest failure, never a false "completed"; a transient-deferred EXIT is a durably-accepted, sweep-pending reduction, not an effect-free rejection.
3. **The refusal taxonomy** lives beside `AdmissionBlockedError` in `uncertainty.py`: `TRANSIENT = {BROKER_SNAPSHOT_STALE, RECONCILIATION_INCOMPLETE, RECONCILIATION_IN_PROGRESS}`, everything else `TERMINAL` (fail-closed). The Clerk's accepted-exit resolution owns this deferral: `resolve_accepted_exit` returns the durable accepted snapshot on a transient refusal, so the periodic reconciliation sweep retries without another strategy decision. The runner commits the accepted evaluation. The former runner-side blocked-receipt/continue handler had no production pre-acceptance producer and is removed by the PR #2495 follow-up (#2482); it must not become a competing retry owner. Unknown refusals still propagate as terminal failures.
4. **Stuck-EXIT policy:** the account reconciliation pass re-drives an `EXIT_NOT_FLAT` episode older than 120 000 ms through a fresh recovery EXIT, at most 3 times, then raises a durable, operator-visible `EXIT_STUCK` uncertainty (blocks new exposure, allows reduction toward zero). The age and attempt budget live in the `uncertainty_policies.py` reason registry; `exit_watchdog.py` consumes them. A closed session, missing quote or unpriceable spread defers without consuming an attempt. Extended-hours retries use the current IBKR touch and the selected authority's allowance; an existing order identity and an operator-confirmed price remain immutable (ADR 0059 D5). Redrive identity is episode-scoped — counted by the `exit-redrive-<sha256(uncertainty_id)[:12]>-<attempt>` command namespace, not a mutable timestamp — because `_exit_identity` keys idempotency on `(strategy_instance_id, decision_id)` alone, so a timestamp-anchored count would reset when a completed-but-non-flat redrive refreshes the episode's `observed_at_ms`, and the watchdog would loop at attempt 1 forever. `EXIT_STUCK` is cleared by the same attributed-flat proof that clears `EXIT_NOT_FLAT`, so a now-flat strategy is never permanently barred from new exposure.
5. **Deferred (with reasons):** the third sweep comparison of strategy intent vs journal exposure (RQ4) — it needs a `SignalSession` read-back seam that Direction 2's replay work will also want, so deferring avoids designing it twice; the concrete harm it cited (a strategy that believes it is flat stops trying to exit) is removed by the stuck-EXIT watchdog, which re-drives regardless of strategy belief. Also deferred: enabling exposure carryover on Resume (the allowlist stays empty), manual order-ticket qualification, account-wide/manual-custody flatten execution, persisting operator provenance in the recovery-EXIT facts (the recovery EXIT uses the generic `strategy_decision` facts; carrying operator action/reason into the durable capture transition is a follow-up audit improvement), and IBKR-stack surfaces.
6. **ADR 0010's `FLATTEN_NOW` / durable-desired-state vocabulary** was retired with the IBKR control plane (evaluator control plane #1678, legacy broker control #1679); the Alpaca/SQLite plane's operator flatten contract is this ADR.

**Clarified 2026-09-25 (PR #2495 follow-up, #2493):** `next_attempt_at_ms` means the earliest session-eligible retry after the episode's age gate. Proven custody and a sweep pass are still required. A usable quote is required only outside the regular session; regular-session retries are market orders and read no quote. Closed sessions project to the next permitted opening; on reopening the displayed timestamp is anchored to that trading day's eligibility, never yesterday's expired time. Until #2501 records actual retry reasons, the UI says only `Automatic retry: allowed from <time ET>`. No timer infers a waiting status. Working EXITs and stopped automatic retries keep their existing precedence. Projection readers must explicitly carry the same recovery-pricing policy as the authority they describe.

### Exit obligation lifecycle amendment (2026-09-25, PRD #2504)

The owner approved persisting **accumulated regular-session failure time**,
rather than measuring eight minutes from a first-refusal timestamp. Holds,
broker outages and Clerk downtime pause the budget without erasing time
already spent. The original first-failure time remains audit evidence, not
the clock that drives escalation. A submitted recovery order clears that refusal budget while its outcome
is pending; an unsent hold does not reset it; a resolved episode cannot lend its budget to a later episode.

Recovery observations belong in separate custody transitions. They must not
refresh `EXIT_NOT_FLAT` itself, change its original raise time, or continually
restart its retry-age gate. A restart requires a fresh observation before any
additional time can accrue. Only intervals bounded by consecutive, current
regular-session failure observations may count; no process may infer that a
failure continued throughout an unobserved gap.

The continuity bound is 30 seconds (two normal reconciliation intervals).
A longer gap adds no time, even if the same writer returns with the same
failure. Both observations must be inside the same canonical regular session;
the calendar therefore handles early closes, weekends and holidays. The
separate `EXIT_RECOVERY_EVALUATED` facts retain the episode identity, writer,
outcome, reason, check time, first-failure time and accumulated duration.
They replay from the mirror without changing the original uncertainty.

This amendment supersedes the earlier 2026-09-25 projection-only retry
clarification above. `RecoveryStatus` is the last actual Clerk evaluation:
`working`, `on_hold`, `allowed_now`, `allowed_from`, `broker_unreachable`,
`stuck`, or `unknown`. It includes the reason and explanation, the last check,
the original `EXIT_NOT_FLAT` raise time, and a future session eligibility time
when one is known. Reads never infer broker failure from elapsed time. A
restart starts at unknown until a fresh check; incomplete or stale sweeps do
not advance the last successful check or spend failure time. Recovery-only
observations commit after the pass's final broker snapshot succeeds.

The canonical calendar owns actual session opens and early closes. The
five-second transport guard protects closes only: it never authorizes a
03:59:55 pre-market send or a 09:29:55 market send. Every reducing create and
resubmit checks live market evidence on the event loop. A retained positive
IBKR halt continues to hold an Alpaca exit until explicitly cleared (ADR
0067), even if the surrounding liveness fact is unknown. A fresh emergency
market close during scheduled RTH also holds; unknown or stale evidence
without a positive halt otherwise falls back to the calendar. Synthetic
execution uses the same calendar with the runner's injected clock.

A Clerk-priced extended-hours DAY limit still working at regular open is
canceled by its existing EXIT. Only exact terminal evidence releases its
custody. The Clerk reads the broker's remaining position after cancellation,
then accepts a new market EXIT with a new episode-derived identity. Partial
fills reduce its quantity; a full fill racing cancellation suppresses it;
an uncertain cancellation cannot create a second live order. An operator's
confirmed safe-flatten limit is never replaced automatically. No automatic
limit is chased repeatedly within its extended session.

An own working EXIT is a hold. Only submitted, terminal, non-flat regular
market redrives consume the three-attempt budget. Extended-session attempts,
closed sessions, missing or unusable pricing, halts, and outages do not
consume that count. The independent eight-minute refusal budget counts only
observed regular-session failures and the escalation names the actual cause.

### Abnormal run endings and shadow evidence (PRD #2504)

Every abnormal terminal run outcome, including feed loss, missed decision bars,
startup refusal and crash, now projects reconciled exposure independently of the
startup phase. A nonzero position reads “Bot is not managing this position”; a
missing or stale reconciliation reads “Position could not be verified; check the
broker”. A working entry has its own warning. These facts reach the bot panel,
account desk and lane attention bell; Flatten opens the existing guarded recovery
flow. An operator Stop retains its own explanation.

The 2026-09-25 holding-Resume allowance bypass is superseded. Carry-over remains
disabled; a held position requires Flatten before Resume. There is no automatic
exit on run death, no broker-side protective stop (the contract supports market
and limit orders only; stops do not trade in extended hours), and no carry-over
without a per-strategy replay-equivalence proof.

Shadow cancellation records `untouched` when eligible later bars existed and
`no_evidence` when they did not. Twin reconciliation maps the latter on a reducing
order to `execution_evidence_missing`, counting neither a pass nor a divergence.
The report names the affected orders. Expiry uses the canonical after-hours close,
including early closes. Recovery binds only a retained bar from its send session;
otherwise it refuses cleanly. The completed decision bar never fills a resting
extended-hours limit.

## Consequences

- Every attributed position now has a presented, executable, Clerk-custody path to flat; the operator never has to intervene at the broker out-of-band (F18 closed).
- A retryable refusal on an accepted EXIT leaves it in Clerk custody for reconciliation instead of requiring another decision bar. The final-bar stale-snapshot regression proves the sweep submits the retained after-hours limit once evidence recovers, with no further strategy bar.
- A stuck exit is bounded-re-driven and then becomes a durable operator decision instead of a silent permanent state.
- The recovery surface's promise holds: an enabled `execute_safe_flatten` is a promise, not a lie — it is presented only where the executor can deliver, and reports honest per-leg outcomes.
