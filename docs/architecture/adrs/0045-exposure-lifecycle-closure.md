# ADR 0045 — Exposure lifecycle closure: recovery flatten executor, EXIT refusal taxonomy, stuck-EXIT watchdog

**Status:** Accepted 2026-08-24
**Provenance:** Authored with the exposure-lifecycle-closure implementation (PRD #1752, PRs #1763/#1765/#1768 and this PR), from `docs/superpowers/plans/2026-08-24-exposure-lifecycle-closure.md`; spec: `docs/audits/strategy-execution-research-directions-2026-08-24.md` Direction 1. Supersedes [ADR 0010](0010-operator-action-contract-flatten-pause-stop.md) for the active Alpaca/SQLite control plane.
**Decision drivers:** F18/F19 (ops study 2026-08-24 §8–§9); the "correct mechanism exists, unwired" failure mode named by the same-day research directions.
**Related:** ADR 0035 (SQLite sole Alpaca custody authority), ADR 0038 (Alpaca sole bot control plane), ADR 0041 (generated Button Reference), ADR 0010 (superseded).
**Vocabulary:** `CONTEXT.md` § "Exposure lifecycle closure" — Recovery EXIT, Safe flatten, Redrive, `EXIT_STUCK`.

## Context

When a bot crashed or was stopped while holding a position, the operator had no in-platform way to make that position flat. Stop cancels working entries and deliberately leaves attributed exposure; every recovery pointer dead-ended (the safe-flatten plan was built but nothing executed it, the panel presented only Resume, carryover was globally disabled, manual tickets were unqualified). Two adjacent failures compounded it: a reducing order that lands terminal-but-not-flat (`EXIT_NOT_FLAT`) was never retried and had no age watchdog, so a stuck exit was silently permanent; and a *retryable* Clerk refusal on the EXIT path (stale broker snapshot during same-clock cohort exits) escalated to a fatal bot crash instead of a deferral. At fleet scale, every crash-with-exposure demanded out-of-band broker intervention (F18), and cohort exits fired the retryable-refusal crash routinely (F19).

## Decision

1. **Flatten executor = a recovery-catalog action, not the `flatten_stop` performer.** `execute_safe_flatten` consumes the already-built versioned `SafeFlattenPlan` and drives reduction-only recovery EXITs to flat through the existing claimed-broker-IO EXIT machine (`resolve_exit` → per-op claim CAS → `ClaimedBrokerIO`). Presentation and dispatch come free from the recovery surface; the frozen `live_instances.py` router is untouched. The legacy `flatten_stop` performer is **not** used: it carries two latent defects proven in the tree — its `accept_exit` runs `require_active_run`, but a crashed bot has no active run and `_flatten_stop`'s own first step (`registry.stop`) retires the run its second step then requires; and its `decision_id=f"panel-flatten:{…}"` violates the colon-free id grammar `accept_exit` enforces (`reject_colon`), so it would fail before broker contact even on a running bot. `flatten_stop`, `pause`, `continue`, `retire` remain unpresented dead vocabulary whose fate belongs to Direction 5's one-lifecycle-surface work. **Removed (2026-09-29, #2595):** the `flatten_stop` performer, its action id, its guard and confirmation, the panel profile's `flatten_supported` flag, and the unreachable Stop and Reconcile performers beside it are gone; a request naming `flatten_stop` is refused at the request schema, and a receipt ledger that already records one still loads as history (`RetiredActionId`). A stopped bot's flatten is `execute_safe_flatten`, as decided here.
2. **Recovery EXITs are run-fence-exempt but exposure-anchored and reduction-only.** `accept_recovery_exit` binds the EXIT's `run_id` to the run recorded on the originating entry's effect operation (not to a live run, which `runtime.recover()` retires on restart). Admission is owned by the SafeFlattenPlan gates, the stuck-EXIT watchdog policy, or -- on a `sim:` authority only -- a Dry Run's run-end close (amendment 2026-09-29, below), plus the downstream `require_capability(Capability.REDUCE, …)`, which authorizes only movement toward zero per leg. The no-active-run fact is enforced twice: `RUN_STILL_ACTIVE` at presentation/recheck, and again inside the capture transaction (`accept_recovery_exit(forbid_active_run=True)` raising `RecoveryRunActiveError`) so a Resume landing between recheck and capture fails closed. Execution is gated to a single strategy-owned leg; account-wide and manual-custody (NULL-strategy) plans stay prepare-only, so `execute_safe_flatten` never advertises an action the executor cannot deliver. Each leg's outcome is classified by the EXIT's terminal state — a broker-rejected reduction (folded `failed`, order row present) is an honest failure, never a false "completed"; a transient-deferred EXIT is a durably-accepted, sweep-pending reduction, not an effect-free rejection.
3. **The refusal taxonomy** lives beside `AdmissionBlockedError` in `uncertainty.py`: `TRANSIENT = {BROKER_SNAPSHOT_STALE, RECONCILIATION_INCOMPLETE, RECONCILIATION_IN_PROGRESS}`, everything else `TERMINAL` (fail-closed). The Clerk's accepted-exit resolution owns this deferral: `resolve_accepted_exit` returns the durable accepted snapshot on a transient refusal, so the periodic reconciliation sweep retries without another strategy decision. The runner commits the accepted evaluation. The former runner-side blocked-receipt/continue handler had no production pre-acceptance producer and is removed by the PR #2495 follow-up (#2482); it must not become a competing retry owner. Unknown refusals still propagate as terminal failures.
4. **Stuck-EXIT policy:** the account reconciliation pass re-drives an `EXIT_NOT_FLAT` episode older than 120 000 ms through a fresh recovery EXIT, at most 3 times, then raises a durable, operator-visible `EXIT_STUCK` uncertainty (blocks new exposure, allows reduction toward zero). The age and attempt budget live in the `uncertainty_policies.py` reason registry; `exit_watchdog.py` consumes them. A closed session, missing quote or unpriceable spread defers without consuming an attempt. Extended-hours retries use the current IBKR touch and the selected authority's allowance; an existing order identity and an operator-confirmed price remain immutable (ADR 0059 D5). Redrive identity is episode-scoped — counted by the `exit-redrive-<sha256(uncertainty_id)[:12]>-<attempt>` command namespace, not a mutable timestamp — because `_exit_identity` keys idempotency on `(strategy_instance_id, decision_id)` alone, so a timestamp-anchored count would reset when a completed-but-non-flat redrive refreshes the episode's `observed_at_ms`, and the watchdog would loop at attempt 1 forever. `EXIT_STUCK` is cleared by the same attributed-flat proof that clears `EXIT_NOT_FLAT`, so a now-flat strategy is never permanently barred from new exposure.
5. **Deferred (with reasons):** the third sweep comparison of strategy intent vs journal exposure (RQ4) — it needs a `SignalSession` read-back seam that Direction 2's replay work will also want, so deferring avoids designing it twice; the concrete harm it cited (a strategy that believes it is flat stops trying to exit) is removed by the stuck-EXIT watchdog, which re-drives regardless of strategy belief. Also deferred: enabling exposure carryover on Resume (the allowlist stays empty), manual order-ticket qualification, account-wide/manual-custody flatten execution, persisting operator provenance in the recovery-EXIT facts (the recovery EXIT uses the generic `strategy_decision` facts; carrying operator action/reason into the durable capture transition is a follow-up audit improvement), and IBKR-stack surfaces.
6. **ADR 0010's `FLATTEN_NOW` / durable-desired-state vocabulary** was retired with the IBKR control plane (evaluator control plane #1678, legacy broker control #1679); the Alpaca/SQLite plane's operator flatten contract is this ADR.

**Superseded by the PRD #2504 recovery-status amendment below — clarification from 2026-09-25 (PR #2495 follow-up, #2493):** `next_attempt_at_ms` means the earliest session-eligible retry after the episode's age gate. Proven custody and a sweep pass are still required. A usable quote is required only outside the regular session; regular-session retries are market orders and read no quote. Closed sessions project to the next permitted opening; on reopening the displayed timestamp is anchored to that trading day's eligibility, never yesterday's expired time. Until #2501 records actual retry reasons, the UI says only `Automatic retry: allowed from <time ET>`. No timer infers a waiting status. Working EXITs and stopped automatic retries keep their existing precedence. Projection readers must explicitly carry the same recovery-pricing policy as the authority they describe.

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

### Immutable exit terms, deployment and arming (2026-09-25, PRD #2504)

An **exit obligation** begins with the program's proposal. A proposal is not
an order: the Clerk accepts a **created order**, the broker establishes a
**working order**, and a **recovery attempt** addresses any remaining
attributed position. **Permitted repricing** is limited to the initial send,
a fresh recovery attempt, and the regular-open replacement above. The
**price authority** for each is the bot's immutable exit terms, applied by
Python to a retained decision close or a fresh IBKR bid/ask; Alpaca executes.

Every new deployment explicitly chooses an exit allowance, band multiple and
spread cap. These `ExitTerms` are recorded atomically with registration,
separate from signal configuration and its hash. Resume cannot change them.
Every exit pricing path, including the operator's ticket and synthetic
execution, resolves that instance's seal. Profile edits affect future deploys.
Existing registrations receive a one-time `backfilled` seal: their own latest
armed envelope, otherwise the effective revision, plus the old band/spread
settings (or 2 / 50). An unavailable allowance is recorded as unset; extended
exits hold while the regular-open market reduction remains possible.

Profiles schema v4 adds optional defaults for both Paper and Live. Absent
defaults are omitted from content hashes, preserving existing history. The
live envelope's exit allowance supplies the new-deploy default; entry pricing
is unchanged. The former band/spread environment knobs are upgrade inputs
only, never a registered bot's runtime price authority.

**Superseded 2026-09-25:** the 2026-09-19 hard band refusal now permits an
operator override. The check displays Python's distance through the bid/ask
and the bot's cap. An explicit acknowledgement obtains a fresh check, followed
by a separate Send confirmation. Execution rechecks the quote, terms and
acknowledgement, and records `band_override` with the accepted EXIT.

Regular-hours Start and Resume share the deploy view's canonical calendar
window: pre-market open through regular close, including early closes.
After-hours, overnight, holidays and weekends refuse with the next pre-market
open. Dry Run is exempt. The backend authors dated exit steps; the UI retains
its ticket through account refreshes and displays refusals at the button.
USD loss caps use one whole-cent rule on new writes, with four-ULP float noise
tolerance; historical revisions continue to load with unchanged hashes.

Live arming is available on the deploy receipt and bot page. HTTP plan/apply
wrap the CLI ceremony, reobserve drift, require the typed content token and
append the envelope plus instance exit terms. Plans last 120 seconds (maximum
300). Every dialog opening reads a fresh plan. Disarm revokes directly from
the selected account's verified ledger even when configuration or binding
reads fail. Status and the lane header use the same custody-world filter.
CLI status lists instances by account and normalizes explicit roots like
defaults. Neither deployment nor a plan alone authorizes live entry orders.

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

**Amended 2026-09-29 (owner decision): a Dry Run is exempt.** It holds nothing
real, so leaving its simulated position open only strands a chore and a false
alarm. When a Dry Run's run ends holding -- a crash, a feed death, the
operator's Stop, a restart -- its own reconciliation pass closes the position
through a recovery EXIT under the `dry-run-close-<sha256(entry_order_ref)[:16]>`
namespace (`clerk/sqlite/dry_run_close.py`). That close fills at the last price
the run saw, not at a live quote, because the feed may be why the run died and
no quote exists overnight; so it is outside the send-time rule and the session
calendar. Operatively that price is the newest bar the market delivered to the
Dry Run's own bar ledger, fill-evidence streams excluded; only the bot's runs
append to that ledger and a close is owed only while no run is active, so it is
the last bar the last run received. The price is re-stated on the
`sim.run_end_close` stream at the instant the simulation sells. The pass runs when the
runner releases the ended run's authority, when boot restores a Dry Run whose
process died, and on every read that reopens a stopped Dry Run; its worklist
is derived from durable facts, one close per exposure. Only a `sim:`
authority is exempt: real accounts and a live account's shadow keep the rule
above. A close that cannot be sent folds like any EXIT (`EXIT_NOT_FLAT`); the
stuck-EXIT watchdog's bounded re-drive and the operator's Flatten, both at the
live IBKR quote, remain the fallback. The fill-evidence streams
(`FILL_EVIDENCE_PROVIDERS`: the recovery quote and the run-end close) are never
any run's replay evidence. The close also supersedes the bot's own EXIT when
one is still waiting as the run ends: a simulation reads no market holds, so
the pass first resolves that EXIT -- it fills at its decision bar, or cannot go
out and ends `EXIT_NOT_FLAT` -- and the close then sells what is left and
clears that notice in the same pass. An EXIT whose order was already sent is
never raced.

**Amended 2026-09-29 (owner decision, #2607): the owner-scheduled end sells a
dead run's shares.** Each bot may carry a one-time end the owner chose at
Deploy and may change while it runs -- an instant, and SELL (the default) or
KEEP. It is the bot's desired state, never a sealed term. The Clerk carries
out every bot's end on its reconciliation pass, whether the bot is running or
its run already died (`clerk/sqlite/scheduled_end.py`): the pass first commits
Stop's own STOP for the bot's ACTIVE run (before any broker read) and asks the
runner to stop the bot's process; the #2362 step cancels the run's working
entries; for SELL, each holding is closed by one recovery EXIT under the
`scheduled-end-<sha256(entry_order_ref)[:16]>` namespace, through the same
exposure scan and driver as the Dry Run close (`ended_run_close.py`); then the
end is recorded carried out. The sale goes out only as a market order inside
the regular session: outside it the EXIT holds (`EXIT_MARKET_HOLD`,
`SCHEDULED_END_WAITS_FOR_OPEN`, never re-priced as an extended-hours limit) and
sells at the next open -- which is how an end missed while the Clerk was down
is carried out when it is back. That rule is a property of the sale's EXIT, not
of its decision-id namespace: its acceptance records `regular_session_only`,
and when Alpaca refuses the sale the stuck-EXIT watchdog re-drives it only as
that sale would go -- a market order in the regular session, holding for the
next open otherwise -- and records the property on every re-drive. While a
sale waits for the open it is a line in the lane's attention bell, naming the
bot, the symbol and the open; the line clears once the sale is sent. This is
the one sale a dead run makes by itself; every other dead-run position keeps
the rule above. On a `sim:` authority the end is a Stop and the Dry Run's
run-end close does the sale; a Dry Run is never offered KEEP, and a stopped
Dry Run's end is recorded carried out when it comes, since its run-end close
already sold. Every operator Stop -- the panel's, the raw lifecycle Stop route's
(`runs/stop`, fleet op `custody_runs_stop`, #2664), and the lane-wide Stop that
installation migration, lane retirement and the budget cutover run -- ends the
bot and its end with it: Stop does not sell, so no sale is left scheduled
behind it. Each makes the same record in the bot's desired state
(`BotTaskRegistry._record_operator_stop_locked`): it cancels the end first --
durably, whether or not the runner still has the bot's process, and whatever
the intent already says -- then records STOPPED, a write that never touches
the end. A crash, a restart or a service shutdown keeps the end, so the Clerk
still carries it out; a crash records STOPPED with the end kept, and a later
operator Stop, the lane-wide one included, still cancels it. The panel's Stop
and the raw route's are one sequence (`recovery_execution.operator_stop_run`),
an operator Stop of the run it names: when that run is the bot's current
one -- its ACTIVE run, else its latest -- the Stop cancels the end before it
commits its STOP and then stops the process, so nothing that runs in between
(the runner's end watch, a Clerk pass) reads the end as still to be carried
out. It does so whether that run is live, died in a crash the sweep or a
restart then stopped, or was stopped by the Clerk at its end. A run is stopped
once, under its first STOP's reason, so a retry of either Stop -- after a lost
response, or after its process stop failed -- replays that STOP and does the
cancel and the process stop again. A Stop naming an earlier run (a retry
landing after a redeploy) changes nothing: its STOP is replayed, and the later
run's end and process are left as they are -- the runner, too, acts on a Stop
only for the run its live process runs. The runner stops a process "at its
end" only when the run's STOP is the Clerk's own at the end (`operator_reason`
`scheduled_end`, which the raw lifecycle Stop route refuses as reserved); a
run any other Stop ended is that Stop's. A Stop landing
after the Clerk's STOP at the end still cancels the end: its STOP is the one
already committed, and the pass reads the end again before it sells. Changing
the end to KEEP after its sale was accepted does not stop that sale: once
accepted it is its EXIT's, like any sale already sent. A Stop does not call it
off either, a sale waiting for the next open included: the Stop cancels the
end, never a sale the Clerk has accepted (owner decision 2026-09-30, #2666).

The stop at the end is proven by the pass that ended the bot, never by a pass
of its own: every bot on the default end stops in the same minute, and a
reconcile each would be one whole account pass per bot. The runner reads the
latest published pass (`published_custody`) once that pass's final broker
comparison saw the bot's every transition and no longer lists any of its
orders as working, and waits for it with the bot's operation lock released. A
proof that lands after a later run of the bot began is recorded as the stopped
run's alone -- its receipt and the replay receipt it owes -- never projected
over the later run's.

Shadow cancellation records `untouched` when eligible later bars existed and
`no_evidence` when they did not. Twin reconciliation maps the latter on a reducing
order to `execution_evidence_missing`, counting neither a pass nor a divergence.
The report names the affected orders. Expiry uses the canonical after-hours close,
including early closes. Recovery binds only a retained bar from its send session;
otherwise it refuses cleanly. A Dry Run's run-end close is the one exception:
it binds the last price its run saw (amendment above). The completed decision bar never fills a resting
extended-hours limit.

## Consequences

- Every attributed position now has a presented, executable, Clerk-custody path to flat; the operator never has to intervene at the broker out-of-band (F18 closed).
- A retryable refusal on an accepted EXIT leaves it in Clerk custody for reconciliation instead of requiring another decision bar. The final-bar stale-snapshot regression proves the sweep submits the retained after-hours limit once evidence recovers, with no further strategy bar.
- A stuck exit is bounded-re-driven and then becomes a durable operator decision instead of a silent permanent state.
- The recovery surface's promise holds: an enabled `execute_safe_flatten` is a promise, not a lie — it is presented only where the executor can deliver, and reports honest per-leg outcomes.

Recovery observation persistence (PR #2505 review correction): repeated successful checks of an unchanged hold update the per-bot `exit_recovery_checks` operational row. Outcome, reason, allowed-from time and failure-budget changes remain immutable custody transitions. Reconciliation collects explicit results and commits them only after its final broker verdict; no implicit buffering mode or queued escalation exists. The sweep's configured cadence and observed pass boundaries distinguish slow work from downtime. This operational row is not custody authority: after restart or mirror rebuild, the new lease owner must complete a check before status is current.


## 2026-09-27 amendment: stopped custody never transfers to a new deployment

[ADR 0038's lifecycle amendment](0038-alpaca-sole-bot-control-plane.md#2026-09-27-amendment-deploy-is-the-only-start-path)
supersedes this ADR's Resume and deferred carryover requirements. Pause, Continue
and Resume are removed. A new deployment has a fresh identity and current
consent; the stopped deployment keeps all existing orders, exposure, receipts and
sealed exit terms until the Clerk proves their resolution. Guarded Flatten and
same-order recovery remain available after Stop or terminal failure. Historical
carryover checkpoints are evidence, never permission for a new run to adopt
custody. This changes no exit allowance, market-data provider or recovery fence.


## 2026-09-27 amendment: budget release follows custody (#2540 / #2545)

Live consent moves into the ordinary Deploy review of exact account/world,
configuration, dollars, immutable ExitTerms and current risk revision. This
supersedes this ADR's separate arming placement. Stop and terminal failure end
new decisions and release only uncommitted free cash; pending orders, actual
unobserved debits and unsettled fees retain their original custody attribution.
The original commitment and launch/stop evidence replay through the Clerk's
existing command journal. They do not form a second balance or FIFO ledger.
A repeated command returns its recorded outcome and cannot relaunch a stopped
run. Guarded Flatten, reducing order recovery and existing exit seals survive.
