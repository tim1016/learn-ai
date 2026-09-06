# ADR 0050 — Supervised execution-lease revival, fenced at the store

**Status:** Accepted 2026-08-31
**Provenance:** Decision ticket [#1800](https://github.com/tim1016/learn-ai/issues/1800) (T7a/T7b), deferred from #1794 (which shipped T7c, the surface-honesty slice). The incident and its characterization are recorded in `docs/audits/bot-fleet-stress-2026-08-26.md` §3 T7 and §6.
**Decision drivers:** A ~50 s `podman pause` (SIGSTOP) let the SQLite execution lease expire while the process was frozen. On SIGCONT the heartbeat failed once and exited permanently; every write on the handle fail-closed; the ~24 still-running bots crashed; their terminal STOP evidence could not commit on the SQLite side; recovery required a full container restart. The same freeze is produced by ordinary events — laptop sleep, VM migration, prolonged CPU starvation — so "restart the data plane" as the *only* cure is an availability defect for a fail-closed condition that is frequently self-curable.
**Related:** ADR 0047 (authority *replacement* is an offline ceremony — untouched by this decision; where revival is refused, that ceremony plus a process restart remains the only cure), ADR 0048 Decision 4f (*expiry is not exclusion; a fencing generation validated atomically at the store is* — the safety rule this design is judged against), ADR 0035/0037 (SQLite is the sole Alpaca custody authority), #1776 (the sweep is the sole automatic reconciler), #1794 (the `EXECUTION_LEASE_LOST` account-scoped blocker, which stays honest under this decision: it clears on the next successful read after revival).
**Vocabulary:** none owed. "Execution lease" and "authority generation" already exist; revival introduces no new operator-facing term (nothing is presented — revival is a background self-cure, visible only as the lease-lost blocker clearing).

## Context

The execution lease is a **topology fence**: it exists to keep a second live process from writing an account's `clerk.db`, not to police the holder's own liveness. Three store-level mechanisms enforce exclusion independently of any process's opinion of itself:

1. **Acquisition is an atomic conditional UPDATE** (`repository_lifecycle._acquire_execution_lease`): a new owner can take the lease only where it is unheld, self-held, or expired. Taking it *changes `execution_lease_owner`*.
2. **Every mutation re-validates the lease** under the repository write lock before it begins (`repository._renew_execution_lease`), and
3. **the custody append itself is store-fenced**: `custody_transitions` sequence uniqueness and the hash chain reject a stale writer's commit if any other writer appended meanwhile — the exact property ADR 0048 D4f names as what makes this lease safe where a bare TTL would not be.

What the incident exposed is not a hole in that fence but the absence of any path *back* through it for the original holder:

- `_renew_execution_lease` refuses an expired lease even when `execution_lease_owner` still names this process — the conditional includes `expires_at_ms >= now`, and nothing in-process ever runs the acquire again.
- The heartbeat (`ReconciliationSweep._run_lease_heartbeat`) exits permanently on **any** exception — a lease loss, but also a single transient disk stall. After it exits, nothing renews, so even a lease that was still valid at the moment of a transient error dies of neglect three heartbeats later.
- Terminal STOP evidence for bots that crash on the dead handle commits file-side (the lifecycle record — which held on 2026-08-26 and is why the T6-fixed roster kept telling the truth) but the SQLite side stays open until the boot scan of the next restart.

## Decision

### 1. A supervised revival path exists, and it is fenced at the store, not by the writer's recollection

The repository gains `revive_execution_lease()`: one atomic conditional UPDATE that re-extends the expiry **only where `execution_lease_owner` still equals this process's owner token AND `authority_generation` still equals the generation read when this handle was opened**. Zero rows updated raises `ExecutionLeaseLost`; the handle is then permanently dead and ADR 0047's restart-plus-ceremony shape is the only cure.

The safety argument, in ADR 0048 D4f's terms: any competing writer must first flip `execution_lease_owner` through the atomic acquire, and any authority ceremony bumps `authority_generation` (reset) or replaces the database file (rebuild — see residual risk below). *Owner unchanged and generation unchanged therefore proves no other writer ever held this account since our last renewal.* Reviving in that state is indistinguishable, in the store's history, from a TTL that had been long enough to cover the freeze. The revival validates this **at the store, atomically, in the same statement that extends the lease** — never by the paused writer's own recollection, which D4f observes a paused writer will always pass.

The paused-mid-mutation hazard the issue names is fenced independently and identically before and after this decision: a writer frozen between its lease check and its SQLite commit is rejected at commit by sequence/hash-chain uniqueness if any other writer appended meanwhile — and if no other writer ever held the account (the only state revival accepts), there is nothing to conflict with.

### 2. The heartbeat exits only on proven loss

`_run_lease_heartbeat` no longer dies on first failure:

- **Transient renewal errors** (anything but `ExecutionLeaseLost`) log CRITICAL and retry next tick. Writes remain independently fail-closed throughout — every mutation revalidates.
- **`ExecutionLeaseLost` on renewal** triggers one revival attempt per tick.
- **Revival refused** (`ExecutionLeaseLost` from the CAS — owner or generation changed) is the one proven-terminal case: log CRITICAL, exit the heartbeat, leave every write fail-closed. The #1794 `EXECUTION_LEASE_LOST` blocker keeps presenting the restart cure, which is now actually the only remaining one.
- **Revival succeeded**: log the recovery (CRITICAL-class event, with the outage bounds), fire the `on_lease_revived` hook, continue heartbeating.

### 3. Terminal evidence's durable home during an authority outage is the file-side lifecycle record; revival closes the SQLite side in-process

T7(b) asked where terminal STOP evidence goes when the authority that owns it cannot be written. The answer is: **where it already durably goes** — `BotRunTerminalRecorder` catches the failed SQLite STOP and records the terminal duty outcome file-side, which the roster projects truthfully since T6. No second evidence store is built; the audit's own weighing ("narrow, bounded — boot scan closed it in practice") is accepted.

What changes is *when* the SQLite side closes. The boot scan's repair pass (`BotBootRecovery._repair_lifecycle_artifacts`) — which commits the missing SQLite STOPs for runs whose tasks are gone — no longer runs only at boot: the sweep's `on_lease_revived` hook invokes it in-process (`BotTaskRegistry.run_lease_recovery`, a narrow re-run: one reconcile pass plus the lifecycle repair, with revival-stamped provenance; no synthetic-authority recovery, no replay-receipt healing — those are boot concerns). In the revivable case the hole now closes seconds after the freeze ends instead of at the next restart. In the non-revivable case, boot scan remains the closer, exactly as today.

A hook failure is isolated: the lease stays revived, the heartbeat continues, and the boot scan at the next restart remains the backstop for whatever the failed pass left open.

## Considered and rejected

**Restart remains the only cure (do nothing).** Legitimate per the issue, and ADR 0047 chose exactly that shape for authority *replacement*. Rejected for lease expiry because the two conditions are not alike: replacement invalidates every control identity and requires broker-captured proof that cannot exist inside the process being replaced; a lease that expired under a freeze with owner and generation unchanged requires no proof beyond what the store itself validates in one statement. Making the ordinary case (laptop sleep) pay the ceremony designed for the extraordinary one conflates D4f's distinction between expiry and exclusion.

**Re-run the full acquire (`owner IS NULL OR owner = self OR expired`) from the heartbeat.** Rejected: the acquire's `expired` arm would let this process *take over* a lease another process held and let lapse — a genuinely contested account. Revival must never break another owner's claim, even a lapsed one; contested accounts get a human and a restart. The revival conditional is deliberately narrower than the acquire.

**A distinct "revived" generation (bump `authority_generation` on revival).** Rejected: the generation's contract is "increments only on explicit reset" (schema §control_meta) and every generation-`N` control identity would be invalidated by a bump — turning a self-cure into a fleet-wide identity invalidation, which is the blast radius of the ceremony ADR 0047 reserves for deliberate acts.

**A durable pending-terminal-evidence journal for T7(b).** Rejected as elaborate relative to the bounded hole: the file-side lifecycle record already *is* the durable record, the roster already projects it, and the repair pass already reconciles the SQLite side idempotently. A second journal would be a second freshness authority for the same fact.

## Consequences

**Positive**

- A frozen-then-thawed data plane (sleep, migration, CPU starvation) self-cures within one heartbeat tick when no other writer ever touched the account, and the ~24-bot crash cascade of 2026-08-26 resolves without operator action: revival → recovery pass → STOPs committed → panel actions live again.
- A transient renewal hiccup no longer bricks the account by killing the heartbeat.
- The fail-closed posture is not weakened anywhere: every refusal that exists today still exists; revival succeeds only in the state where the store can prove nothing needed refusing.

**Negative / accepted**

- **A CRITICAL log stream while dead-but-retrying.** A permanently frozen sibling process that never revives logs each tick. Accepted: the alternative is the silent dead heartbeat that produced T7.
- **Residual: file replacement under a frozen process.** An offline rebuild that replaces the database file while the old process is frozen leaves the old connection pointing at the retired inode; its revival CAS would pass against the *old file's* control row. This is governed — before and after this decision — by the recovery runbook's stop boundary (the operator must stop the data plane before the ceremony; a frozen process is not stopped). The reset path is covered by the generation conjunct; the file-swap path is operator-governed, unchanged, and recorded here as residual rather than silently assumed away.
- **Revival provenance is process-local.** The revival itself appends nothing to the custody ledger (it is a `control_meta` lease-column update, deliberately outside the hash chain, like every lease write today). The observable receipts are the CRITICAL logs and the revival-stamped lifecycle repairs.

**Follow-up (recorded, not blocking):** once #1808's `RecoveryEvaluationObservation` and this decision are both merged, `run_lease_recovery` should reset the facade's evaluation anchor so post-revival unresolved intents present as "sweep still evaluating" during the settle window, exactly as post-boot ones do.

## Addendum (2026-09-06): the write path reports a retryable refusal instead of auto-retrying

The write path's own `ExecutionLeaseLost` handling (`panel_data_source.run_action`, first shipped after this ADR as a follow-up) went through two rounds that retried the same mutation in-process, under a derived idempotency key, immediately after a successful revival. Two independent reviews blocked that design: the derived key gave the retried performer a new broker decision identity that neither the in-process nor the durable `(sid, decision_id)` dedupe recognised, so a second EXIT could reach the broker after the first attempt's already had; the original key stayed burned `failed` for a leg that had, from the operator's perspective, just self-cured rather than failed; and a store error other than `ExecutionLeaseLost`/`RepositoryPoisoned` from the revival call reached no `except` clause and leaked as a raw 500.

Round 3 (`panel_data_source._revive_lease_or_raise`) keeps the revival itself unchanged — still exactly one call to `ReconciliationSweep.revive_now()`, the same entry point the lease heartbeat's own tick uses — but never re-drives the mutation. A successful revival now ends in `ExecutionAuthorityRevivedError`, a typed, retryable 503 telling the operator the lease is good again and nothing was applied; the operator's next click is a genuinely new request under a fresh idempotency key. A `RepositoryPoisoned` raised by the revival's own admission check is translated to `AuthorityPoisonedError`; any other exception (a transient store error, e.g. "database is locked") is reported as a transient `ExecutionAuthorityLostError`, mirroring the heartbeat's own "errored; retrying" vocabulary, instead of leaking unhandled. A cohort batch (ADR 0051) treats this new error as a per-leg refused/retryable outcome, not the account-scoped authority loss that ends the batch early — the revival already succeeded, so the next leg's own lease renewal is expected to succeed too. One consequence of calling `revive_now()` from a request rather than the heartbeat: a client can abandon the request mid-flight, and the CAS runs on a worker thread that a cancelled await does not unwind, so the lease can come back revived after its caller is gone. The CAS-log-hook unit therefore runs as its own task that callers observe under `asyncio.shield`; a cancelled caller waits for the unit to finish before the cancellation is honoured, so the revived lease still gets its recovery pass instead of being quietly renewed by the heartbeat with its lifecycle artifacts unrepaired.

The refusal-then-restart cure for a revival the store actually refuses is unchanged: that copy, and the terminal disposition it carries, stand exactly as decided above.

## Addendum (2026-09-06, round 4): the burned idempotency key blocked the retry it promised

The final independent review of round 3 blocked on one scenario: a cohort flatten (ADR 0051) derives each leg's idempotency key as `{cohort_key}:{sid}`. When a leg hit `ExecutionLeaseLost`, `action_execution_service.execute_action`'s exception handling called `ledger.fail(...)` on that derived key before re-raising — burning it `failed` — and then `_revive_lease_or_raise` classified the leg `refused` on the promise that "a re-POST under the same key covers it." A re-POST under that same key instead hit the burned record ("This action previously failed; the idempotency key cannot be reused"), so the leg could never be flattened under its own key and the bot stayed exposed. `StaleRevisionError`, the contract's other `refused` member, releases its key on the identical pre-exec-rejection path, which is why its promise held and this one didn't.

The fix: `ExecutionLeaseLost` releases its idempotency key instead of failing it, in both places that resolve it ahead of the revival catch — `action_execution_service.execute_action` and `sqlite_panel_source.execute_sqlite_panel_action`'s equivalent exception handling. This is sound because every repository mutation renews the execution lease as its very first statement under the write lock (`repository.py`'s `_renew_execution_lease`/`_assert_not_poisoned` pattern) — a lost lease means nothing else in that call ran, exactly the "nothing applied" contract a pre-execution rejection already gets. `RepositoryPoisoned` keeps its existing `fail` treatment: unlike a lost lease, a poison can be raised *after* a transition's SQLite commit already succeeded (`append_transition`'s own mirror-finalize failure) — a mutation may really have applied, so a blind same-key retry stays unsafe.

Two smaller findings from the same review landed alongside it. First, copy consistency: `REVIVAL_OUTCOME_TRANSIENT_STORE_ERROR`'s "retry this action shortly" was appended to `ExecutionAuthorityLostError`'s fixed detail template, which always closed on "Restart the data plane…" — one blocker naming two contradictory remedies. `ExecutionAuthorityLostError` now takes an explicit `remedy` override (the transient case gets a retry-only sentence, no restart claim) and an `attempted` flag (`REVIVAL_OUTCOME_AUTHORITY_UNAVAILABLE`/`REVIVAL_OUTCOME_NO_SWEEP` never called `revive_now()` at all, so the old fixed lede claiming "a supervised, store-verified revival attempt did not restore it" was false for those two; they now say "no supervised revival could even be attempted"). Second, outcome-vocabulary alignment: the router mapped a revived lease to `outcome: "failure"` while the cohort batch mapped the identical condition to `refused`/`conflict`. Both now report `"conflict"` — the same retryable reading `StaleRevisionError` already gets, since nothing applied and a same-key re-POST (a fresh key for a solo action, the same derived key for a cohort leg) resolves it.
