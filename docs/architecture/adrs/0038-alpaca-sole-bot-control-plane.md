# ADR 0038: One bot control plane; SQLite holds the duty facts it already fences

- **Date:** 2026-08-17
- **Status:** Accepted
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1598](https://github.com/tim1016/learn-ai/issues/1598); the
  census in `docs/audits/state-writer-census-2026-08-17.md`.
  Grilling session: `grill-with-docs` + `domain-modeling`, 2026-08-17.
- **Supersedes:** ADR 0026 §4 and its 2026-07-21 amendment, for Alpaca bots.
- **Vocabulary:** `CONTEXT.md` § "Bot control plane (resolved 2026-08-17)".

## What the census actually found

The census reported "five bypass writers" of `lifecycle_state.json` and
`desired_state.json` violating ADR 0026's single-writer evaluator. That count is
mechanically correct and architecturally misleading. Those five are not scattered
violations inside one control plane — they **are** a second, complete control
plane:

| | Evaluator plane | Runner plane |
|---|---|---|
| Entry point | `routers/live_instances.py`, `host_daemon.py` | `routers/broker_bots.py` |
| Writer | `bot_lifecycle_evaluator.py` | `bot_runner.py`, `bot_boot_recovery.py`, `bot_run_evidence.py`, `bot_run_terminal.py` |
| Serialization | `bot_lifecycle_operation_fence` (cross-process flock) | per-instance `asyncio.Lock` (in-process) |
| Pre-commit protocol | `prepare_start` → `record_start_accepted`, JSONL receipts | `register_order_capable_run` → SQLite |
| Broker | IBKR lineage | Alpaca |

`routers/broker_bots.py` contains **zero** references to `BotLifecycleEvaluator`.
The live Alpaca path has never used it. `bot_runner.py`'s
`_manages_boot_recovery` already discriminates the two planes by broker binding,
with the comment "Keep daemon-owned broker artifacts out of this runner's sweep"
— the boundary exists in code as a private method, never as a stated rule.

Two further findings shaped this decision:

- **ADR 0026 §4 was never built as written.** It specifies the evaluator as a
  pure function `f(evidence) → (phase, offers, conditions)`, phase as a
  rebuildable projection, and reads that return "a drift flag when it disagrees
  with the persisted phase". No drift flag exists anywhere in the repo, and the
  evaluator has no evidence-folding function — it is a command handler with a
  write-ahead receipt log. The 2026-07-21 amendment quietly replaced the
  derived-projection design with a single-writer-command design; the ADR now
  carries both, and they are incompatible.
- **SQLite already fences the duty facts, harder than the files do.**
  `ux_runs_one_active_per_instance` is a partial unique index on
  `runs(strategy_instance_id) WHERE state = 'ACTIVE'` — one active run per bot,
  enforced by the database and impossible to violate.
  `strategy_instances.retired_at_ms` is set once and never cleared.
  `execution_lease_owner` + `execution_lease_expires_at_ms` + `authority_generation`
  are the owner-generation and TTL fences the census reported missing on the
  file paths. `lifecycle_state.json` expresses those same facts with advisory
  flocks, no compare-and-swap, and four writers.

## Decision

**1. Alpaca is the only bot control plane. The evaluator plane retires with the IBKR bot-control surface.**

`bot_lifecycle_evaluator.py`, its disposition receipt log, the
`lifecycle_operation_fence`, and the `live_instances.py` deploy/start path are
IBKR lineage. They go when that surface goes. Alpaca does not adopt them, and no
new Alpaca control work routes through them. This mirrors ADR 0037: one lineage
survives, the other is retired rather than reconciled with.

**2. For an Alpaca bot, SQLite is the authority for duty facts it already holds.**

Specifically: *is this bot on duty and under which run* (`runs.state = 'ACTIVE'`
plus the unique index) and *is it retired* (`strategy_instances.retired_at_ms`).
`lifecycle_state.json`'s `phase`, `active_run_id`, and `retired_at_ms` become a
**derived projection** of those columns — readable, rebuildable, never a second
source of truth. A disagreement between the file and SQLite is resolved in
SQLite's favour, always.

**3. Two duty facts stay file-backed, deliberately.**

`desired_state.json` (cross-run operator intent) and `lifecycle_state.on_roster`
(roster membership) are **not** in SQLite and must not move there. ADR 0026's
amendment requires durable control intent to work "without a Clerk or broker
connection" — a STOPPED bot must refuse to self-restart even when the Clerk is
down. Putting intent behind the custody authority would make the safety latch
depend on the thing it exists to survive. These remain files, and the runner is
their single writer.

**4. The launch sequence needs a recovery rule, not a distributed transaction.**

`_activate_binding` writes SQLite registration → runner JSON → desired state →
lifecycle state → task install. That is not atomic and will not be made atomic.
Instead: SQLite registration is the **commit point**. Everything after it is
reconstructible from SQLite on boot, and boot recovery's job is to make the files
agree with SQLite — not to guess. A crash after registration leaves a registered
run whose file projection is stale, which is a repair, not an ambiguity.

**5. "Deploy state" is retired as a term.** See Consequence 6.

## Considered and rejected

- **Route the Alpaca runner through the evaluator.** Buys the cross-process
  fence, prepared-start receipts, and a disposition audit trail. Rejected because
  the Alpaca plane already has a *stronger* version of each: SQLite registration
  is revision-bound, generation-fenced, and transactional, versus a JSONL receipt
  under an flock; and the one-active-run unique index is a database constraint
  where the evaluator has a convention. The evaluator was designed around a
  daemon spawning a child process; the Alpaca runner supervises in-process
  asyncio tasks. This would have imported a weaker protocol at real cost.
- **Codify the split — one plane per bot, decided by broker.** The honest
  description of today's code, and cheap. Rejected because it blesses two control
  planes permanently, and the map's premise is single authority. It survives only
  as the transitional rule in Consequence 1.
- **Move `desired_state` into SQLite for one true store.** Rejected on
  Decision 3's reasoning: the stop latch must outlive the Clerk.
- **Enforce ADR 0026 §4 as written — derive phase, add the drift flag.** The
  most faithful reading of the original design. Rejected because SQLite now holds
  the evidence that design wanted to fold, so the deriving function would be
  reimplementing a query the database answers with an index.

## Consequences

These are **not implemented**. This ADR is a decision; the corrections belong to
separate work.

**Correction 2026-08-18.** The consequences below were re-verified line-by-line
while landing them in `docs/known-gaps.md` (register ticket
[#1610](https://github.com/tim1016/learn-ai/issues/1610)). The **decision above
is unchanged**; two consequence statements were factually wrong and are corrected
here rather than rewritten in place, so the record of what was believed on
2026-08-17 survives:

- **Consequence 2 mis-cites two of its four call sites.** `bot_boot_recovery.py:125`
  and `bot_run_terminal.py:52` are `desired_state.json` writers, not
  `lifecycle_state.json` writers; those files' lifecycle writes are at
  `bot_boot_recovery.py:153` and `bot_run_terminal.py:69`. The two artifacts fall
  under different decisions — Decision 2 (projection of SQLite) versus Decision 3
  (deliberately file-backed, must survive a Clerk outage) — so the mis-citation
  would have subordinated the stop latch to the authority it exists to outlive.
  Correcting the citations also changes the count: there are **three** direct
  `lifecycle_state.json` projection writers — `bot_runner.py:471`,
  `bot_run_evidence.py:72`, `bot_boot_recovery.py:153` — and boot recovery must be
  inside the write-what-SQLite-committed rule, since Decision 4 gives it the job
  of making the files agree with SQLite.
  See [#1634](https://github.com/tim1016/learn-ai/issues/1634).
- **Consequence 3's "never read" is wrong.** `BotLifecycleStateRecord.version` is
  read three times, all in `bot_lifecycle_evaluator.py`: `:672` as a receipt
  `sequence`, `:673` to synthesize a `receipt_id`, `:731` as the `state_version`
  on every terminal disposition receipt. The defect stands in corrected form: it
  is *receipt metadata*, never *compared*, and there is no `expected_version`
  parameter or compare-and-swap anywhere. See
  [#1631](https://github.com/tim1016/learn-ai/issues/1631).

Consequence 4 is narrower than it reads. The silent refusal is deliberate and
pinned by `test_stale_terminal_fact_cannot_supersede_a_newer_on_duty_run` — the
receipt records that *the old run* ended while the file correctly stays `ON_DUTY`
on *the new run*, which is not a contradiction. What survives is an observability
defect, and `bot_boot_recovery` reporting success unconditionally. Consequence 8's
AST hole is confirmed by the contract test passing green against
`services/bot_runner.py:468`, but that call is *allowed* under Decision 3 — the
defect is that the test cannot distinguish it from an unauthorised writer using
the same idiom.

Each consequence now has an issue: [#1630](https://github.com/tim1016/learn-ai/issues/1630),
[#1631](https://github.com/tim1016/learn-ai/issues/1631),
[#1632](https://github.com/tim1016/learn-ai/issues/1632),
[#1633](https://github.com/tim1016/learn-ai/issues/1633),
[#1634](https://github.com/tim1016/learn-ai/issues/1634),
[#1635](https://github.com/tim1016/learn-ai/issues/1635),
[#1636](https://github.com/tim1016/learn-ai/issues/1636).

1. **Transitional rule while the IBKR surface still exists.** A bot identity
   belongs to exactly one plane, decided by its broker binding. The discriminator
   currently lives in `bot_runner.py:1025` `_manages_boot_recovery` and applies
   only to the boot sweep; it must become an explicit, tested precondition on
   every duty-state write on both planes. Until then, an operator invoking a
   `live_instances.py` lifecycle action against an Alpaca `strategy_instance_id`
   writes duty state under a fence the runner does not hold.
2. **`lifecycle_state.json` phase writes must be reclassified as projection
   writes.** Four call sites — `bot_runner.py:471`, `bot_run_evidence.py:72`,
   `bot_boot_recovery.py:125`, `bot_run_terminal.py:52` — are correct as
   *projection* writers under Decision 2 and are **not** defects to be routed
   elsewhere. What they lack is a rule that they may only write what SQLite
   already committed.
3. **`BotLifecycleStateRecord.version` is an unused fence.** It is incremented on
   every `update()` and never read. Either wire it as a compare-and-swap token
   for the file projection or delete it; a counter nothing checks reads as a
   safety property that is not there.
4. **`update(expected_active_run_id=...)` fails silently.** On mismatch it
   returns the existing record rather than raising, so a caller that ignores the
   return value believes its write landed. Every caller must be audited, or the
   seam must raise.
5. **`services/end_day_intent.py` is dead code.** Nothing outside the module
   imports it. It is evaluator-plane and retires under Decision 1; recorded here
   so its deletion is not mistaken for a behaviour change.
6. **"Deploy state" names four artifact families and should stop.** SQLite
   registration/run folds (survives, canonical); runner JSON instance/run records
   (survives, process-restoration evidence); `run_ledger.json` (retires with the
   IBKR plane); IBKR-lineage account binding `DEPLOYED`/`ACTIVE`/`RETIRED`
   (retires with it — its only consumers are `account_directory.py` and
   `routers/account_reconciliation.py`, both IBKR-lineage). Post-retirement two
   families remain, and they need two names. This is the vocabulary half of the
   ticket and feeds [#1595](https://github.com/tim1016/learn-ai/issues/1595).
7. **ADR 0026 must be marked superseded for Alpaca**, and its internal
   contradiction — §4's derived-projection design versus the 2026-07-21
   amendment's single-writer-command design — noted at the top so a reader does
   not implement the half that was never built. Its Button Rule (§3) and run
   identity (§6) are unaffected.
8. **The single-writer contract test is weaker than it reads.** Its AST visitor
   recognizes only repositories assigned from direct constructor calls, so
   `self._desired_repo(...).set(...)` and injected repository callables pass
   through it. Under Decision 2 the test is asking the wrong question anyway; it
   should assert projection-write discipline, not writer identity.
