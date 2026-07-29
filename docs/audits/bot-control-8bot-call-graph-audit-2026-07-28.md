# Bot-Control 8-Bot Call-Graph Audit — 2026-07-28

> **STATUS: SUPPORTING HISTORICAL ENGINEERING EVIDENCE — NOT OPERATING AUTHORITY.**
> This point-in-time investigation is not a Bot Control manual, quick procedure, or
> implementation authority. Use
> [`docs/bot-control-operator-manual.md`](../bot-control-operator-manual.md) for
> current Bot Control and Account Clerk behavior, ADRs for decisions, and
> [`docs/known-gaps.md`](../known-gaps.md) for the living open-defect register. The
> findings below preserve provenance and qualification evidence only.

**Goal:** run 8 concurrent Interactive Brokers (IBKR) paper bots tomorrow without repeating the
2026-07-27 failure. **Method:** traced every bot-control UI action → FastAPI router → host daemon →
Clerk → broker, stack-trace style, then adversarially re-verified every cross-agent conflict and every
high-severity finding against the actual code. All tracing/verification ran on Sonnet subagents; this
synthesis is the only Opus step.

Line numbers are as cited by the tracing agents and are approximate — open the file to confirm before
editing. Findings tagged **[verified]** were re-read and quoted in a second pass; **[single-pass]** come
from one tracer at the stated confidence.

---

## Resolution status (PR #1289) — read this first

This audit was authored as a findings list; two findings were then fixed in the same PR. **Do not re-apply
them.** Current status:

- **BUG-4 (broker-write-lock cancel timeouts) — FIXED.** `request_timeout_s` now gives both
  `cancel_exact_order` and `cancel_pending_a0` the 240s submit-sized inner budget, **and** the outer
  container→daemon HTTP hop (`host_daemon_client._CANCEL_TIMEOUT` = 260s) covers that inner budget on the
  operator cancel path (without the outer bump the inner fix was inert — the 10s/130s HTTP reads timed out
  first).
- **BUG-10 (`ended_without_status` restart gate) — FIXED for the direct restart path.** The gate now consumes
  `TERMINAL_RESTART_BLOCKING_BINDING_SOURCES`. A **pre-existing** deploy-only bypass remains (see **BUG-16**),
  tracked as a follow-up — it affects `crashed`/`liveness` too and is not introduced by this PR.
- **BUG-1, BUG-2, BUG-3 — OPERATIONAL, not code.** These are the actual launch blockers; run the pre-run
  checklist. The remaining *code-side* risk for the run is **BUG-1** (A0 admission latency).
- **BUG-5/6/7 (wind-down) — DEFERRED** (live bar-loop / stop state machine; need paper-broker validation).
  BUG-7's original root-cause was mis-diagnosed and has been corrected below.

---

## Headline: the #1285 "240s timeout" fix is inert on the live path

Production IBKR bots do **not** use the RPC `operation="submit"` path (the one that got the 240s
`ACCOUNT_CLERK_RPC_SUBMIT_TIMEOUT_S`). They use the **async-custody A0/A1 split**:

- **A0 (caller-blocking, ~10s):** `run.py:_submit_strategy_custody_at_a0` → `clerk_client.submit_custody_v2(intent)`
  (`account_clerk_rpc.py` op `submit_custody_v2`, deadline `ACCOUNT_CLERK_RPC_CUSTODY_RESPONSE_DEADLINE_S = 10.0s`).
  Durable admission only — no broker ack yet.
- **A1 (async, not caller-blocking):** Clerk worker `_advance_async_custody_intent` takes `_broker_write_lock`
  (serialized across all 8 bots for the account) and calls `_place_broker_order` bounded by
  `_BROKER_SUBMIT_TIMEOUT_S = 25.0s` (`account_clerk.py:157, 824, 2379`). The bot learns the outcome via the
  Clerk journal event stream (`run.py` `drain_events`), not by blocking.

**Real call chain (live bot):**
`live_portfolio.py:1726 submit_pending_orders` → `run.py:2252 _submit_to_account_clerk` →
`run.py:1509 submit_custody_v2` (10s A0) → *[async]* `account_clerk.py:546 submit_async_custody` →
`_enqueue_async_custody` → `_run_async_custody_lane` → `account_clerk.py:824 _broker_write_lock` →
`_place_broker_order` (25s) → `broker/ibkr/orders.py place_paper_order`.

**Consequences:**
- The 240s constant is dead for live bots. Do not rely on it.
- The 2026-07-27 **sibling-cascade is eliminated** — see "Confirmed sound" below.
- The **residual** is A0 admission latency: if the Clerk event loop / journal fsync stalls under 8 bots'
  concurrent writes, the 10s A0 deadline (and the 30s `read_custody_v2` retry) can expire → the submitting
  bot raises `SubmitUncertainHaltError` and exits. Per-bot, not fleet-wide, but still a bot-down event.

---

## Bug list (ranked by risk to tomorrow's run)

### TIER 1 — Blockers (address or explicitly accept before the run)

**BUG-1 — A0 admission can time out under 8-bot I/O contention (residual of the 2026-07-27 mechanism)** **[verified]**
- Severity: BLOCKER (per-bot halt) · Confidence: high on mechanism, unmeasured on likelihood
- Pathway: `run.py:1509 submit_custody_v2` (10s) → on timeout `run.py:1511 read_custody_v2` (30s) → on failure
  `live_portfolio.py:1764 SubmitUncertainHaltError` → bot writes `poisoned.flag` and exits.
- What goes wrong: A0 admission does journal fsync under `_async_custody_admission_lock + _intake_lock`
  (`account_clerk.py:599–710`). With 8 bots fsyncing concurrently on the podman bind mount, admission can
  exceed 10s; the 30s read-retry can also stall if the event loop is blocked. Descendant of last time's
  root mechanism — now scoped to the one bot (no cascade).
- Action before run: **measure A0 admission p99 at 8-bot load** (run the custody qualification drills, or an
  8-bot dry run, and read the admission latencies). If A0 can approach ~5s, widen
  `ACCOUNT_CLERK_RPC_CUSTODY_RESPONSE_DEADLINE_S` and/or reduce fsync contention before committing to the run.

**BUG-2 — Dirty binding-ledger parity blocks ALL 8 bot starts (409)** **[verified]**
- Severity: BLOCKER (morning launch fails) · Confidence: high
- Pathway: `host_daemon.py:1407 _write_account_registry_binding(ACTIVE)` → unconditional
  `account_binding_ledger_parity(...)` → `raise HostRunnerError(409, "Account binding ledger parity is dirty")`.
- What goes wrong: the parity check runs on *every* ACTIVE binding write and is **not** gated by
  `ACCOUNT_BINDING_LEDGER_READ_ENABLED`. Any account with a `legacy_only` registry row (deployed before the
  dual-write ledger) is "dirty," so every start 409s. `is_clean` is false when `legacy_only_instances`,
  `ledger_only_instances`, or `mismatched_instances` is non-empty (`account_binding_ledger.py:247–276`).
- Fix / pre-run: see checklist CHK-1 (baseline the ledger). Code-side, boot reconcile could auto-baseline
  legacy-only rows when parity is the gate.

**BUG-3 — Unidentified Clerk poisons the entire IBKR client-id pool → all starts 409** **[verified]**
- Severity: BLOCKER (morning launch fails) · Confidence: high
- Pathway: `account_clerk_supervisor.py:297 in_use_client_ids()` → `if self._unidentified_live_clerk_accounts:
  in_use.update(self._client_id_pool())` → `host_daemon.py:2043 _allocate_ibkr_client_id` → 409 "No IBKR
  client IDs are available".
- What goes wrong: if a live Clerk's lease has `ibkr_client_id=None` (legacy Clerk, or a daemon restart that
  adopted a Clerk with an incomplete lease), the supervisor marks it unidentified (`:525`) and reports the
  **whole pool** as in-use, so all 8 allocations fail.
- Fix / pre-run: see checklist CHK-2.

### TIER 2 — High

**BUG-4 — broker-write-lock cancels timed out under 8-bot contention (both layers)** **[verified] — RESOLVED in PR #1289**
- Severity: HIGH · Confidence: high (refines/reattributes the earlier "cancel_namespace" claim)
- Pathway: `account_clerk_rpc_protocol.py:290–306` mapped `cancel_exact_order` **and** `cancel_pending_a0` → 30s
  NORMAL; both hold `_broker_write_lock` (`account_clerk.py:1313`, `:1385`).
- What goes wrong: under 8-bot A1 contention, up to 8 broker writes (25s each) sit ahead on `_broker_write_lock`
  → ~200s worst case, exhausting the 30s budget → an uncertain cancel that can durably block that bot's next
  submit. (Note: `cancel_namespace`, `recovery_flatten`, `recovery_flatten_batch`, `fold_binding_retirements`,
  `record_binding_decision`, `drain_events` do **not** take `_broker_write_lock` and are adequately budgeted —
  the earlier BLOCKER on `cancel_namespace` is downgraded.)
- **Fix (landed): two layers.** (1) `request_timeout_s` returns the 240s submit-sized budget for both cancels.
  (2) The operator cancel path also crosses container→daemon HTTP via `host_daemon_client`, whose outer reads
  were 130s (`operator_exact_cancel`) / 10s (`operator_pending_cancel`) — **below** the 240s inner budget, so
  they timed out first and the inner fix was inert on the operator path. Added `_CANCEL_TIMEOUT` (260s) and
  wired both operator cancel forwards to it. The shared `_FLATTEN_TIMEOUT` / `_START_ADMISSION_TIMEOUT`
  constants stay short (used by other operations).

**BUG-5 — Stop on a crashed subprocess returns without flattening → position left open** **[single-pass, high]**
- Severity: HIGH · Pathway: `live_instances.py stop_run` → `host_daemon.py:1831` `if current.process.poll() is
  not None: return ... stop_outcome="exited"` (no flatten). The in-process recovery flatten
  (`run.py:2888–2976`) is itself fallible (broker disconnected → `"no live broker for recovery flatten"`).
- Fix: when a dead process still has owned-position evidence, auto-route to the Clerk emergency workflow or
  write a durable incident, instead of silently returning `exited`.

**BUG-6 — CLOCK_OUT leaves a bot alive-PAUSED with an open position if flat-evidence doesn't clear in 30s** **[single-pass, high; deferred]**
- Severity: HIGH · Pathway: `live_instances.py end_day_now` → `live_engine.py:_complete_clock_out` awaits
  `_flatten(...)` (cancel + liquidate) **first**, then `_await_fresh_flat_broker_evidence` (30s,
  `CLOCK_OUT_FLAT_EVIDENCE_TIMEOUT_S`) → on timeout writes `CLOCK_OUT_BROKER_NOT_FLAT`, acks "failed",
  `shutdown_event` NOT set → bot stays up, desired state PAUSED, no retry.
- Scope note (per review): the 30s deadline is created **inside** `_await_fresh_flat_broker_evidence`, i.e.
  only *after* `_flatten` returns — so this is the genuine "flatten submitted but the broker is slow to confirm
  the position is flat" case, not a serialization victim. The concurrent-fleet failure is BUG-7's cancel
  timeout, not this window.
- Fix: on `CLOCK_OUT_BROKER_NOT_FLAT`, escalate to hard STOP + recovery flatten rather than staying alive
  PAUSED with exposure.

**BUG-7 — 8-bot end-day: serialized namespace-cancel exhausts the 25s cancel timeout → uncertain, CLOCK_OUT_FAILED** **[single-pass, medium-high; deferred] — diagnosis corrected per review**
- Severity: HIGH (wind-down) · Corrected pathway: 8× CLOCK_OUT → each `_flatten` calls
  `_cancel_namespace_through_clerk` → Clerk `cancel_namespace` serializes on `cancel_operation_lock`
  (`account_clerk_operations.py`), and the broker cancel is wrapped in `asyncio.timeout(
  ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S = 25s)` (`account_clerk_operations.py:1305`). If the first bot's
  cancel occupies most of the 25s window, queued bots exhaust *that* timeout → `AccountClerkCancelNamespaceUncertainError`
  → propagates out of `_flatten` → `_complete_clock_out` records a **`CLOCK_OUT_FAILED_*`** receipt and leaves
  the namespace cancel in `uncertain_requires_reconciliation`.
- Correction: the original writeup blamed serialized cancel *consuming the 30s flat-evidence window*. That is
  wrong — the flat-evidence timer only starts after `_flatten` returns (see BUG-6). The real failure is the
  25s cancel timeout under `cancel_operation_lock` serialization.
- Fix: wind down via per-bot Stop instead of 8 concurrent CLOCK_OUTs, and/or make the cancel budget
  fleet-size-aware so serialized cancels don't reach the 25s bound.

**BUG-8 — `CLERK_ASYNC_ENTRY_QUEUE_FULL` on simultaneous burst → per-bot halt** **[suspected]**
- Severity: HIGH · Confidence: suspected. Pathway: entry queue capacity 8 (`account_custody_topology.py:13`);
  if 8 bots submit before the worker dequeues, `submit_async_custody` (`account_clerk.py:699/749`) can reject
  with `CLERK_ASYNC_ENTRY_QUEUE_FULL` → `SubmitUncertainHaltError`.
- Action: confirm the worker drains the entry lane fast enough that a synchronized-bar burst of 8 never finds
  the queue full at admission. Verify in the qualification drills.

### TIER 3 — Medium

**BUG-9 — Stale nonterminal intents silently consume order-slots → 9th order of the day rejected mid-session** **[verified; original "blocks starts" framing REFUTED]**
- Severity: MEDIUM · Pathway: `account_clerk_journal.py:284` terminal set = {economic_terminal,
  expired_before_submit, cancelled_before_submit}; `nonterminal_async_custody_depths` counts everything else
  (queued, submitting, uncertain_requires_reconciliation, recovery_action_required, submission_hold) against
  the 8-slot budget (`account_clerk.py:747`).
- Correction: this does **not** block bot *starts* (there is no custody check in the start path). It blocks
  the first order that would exceed capacity mid-session — a silent `CLERK_ASYNC_ENTRY_QUEUE_FULL` if stale
  intents carried over from a prior session. See checklist CHK-3.

**BUG-10 — `ended_without_status` (SIGKILL/OOM) did not block same-instance restart** **[single-pass, high] — RESOLVED in PR #1289 (direct path)**
- Severity: MEDIUM · Pathway: `exit_taxonomy.py TERMINAL_RESTART_BLOCKING_BINDING_SOURCES` includes
  `ended_without_status` but was **never consumed**; `crash_retired_restart_blocking_binding` only checked
  `RECOVERY_REQUIRED_RETIRED_BINDING_SOURCES` {process_crashed, boot_liveness_unproven}. A SIGKILL/OOM exit
  could restart the same instance with no recovery gate (the ops surface already flags it critical). Note: the
  account-reconciliation surface *does* surface it as a critical "Retire & Replace required" condition, so the
  original "reads as clean-stopped" wording overstated it — the gap was the start-admission gate, not the ops
  surface.
- **Fix (landed):** `crash_retired_restart_blocking_binding` now consumes `TERMINAL_RESTART_BLOCKING_BINDING_SOURCES`;
  a same-id restart requires an audited recovery override or retire-and-replace. Both cure paths already work
  (`record_crash_recovery_override_evidence` is source-agnostic). Also extracted `terminal_restart_failure_phrase`
  so the operator message is accurate (not "crashed") for this source.
- **Residual (BUG-16):** the gate reads only the *latest* binding, so a deploy-only stage can still mask it.

**BUG-16 — deploy-only staging masks a blocking retirement (pre-existing gate bypass)** **[verified; pre-existing, NOT introduced by PR #1289]**
- Severity: MEDIUM (safety-gate bypass) · Pathway: `latest_account_instance_binding` returns the single most
  recent row regardless of `lifecycle_state` (`account_registry.py` `index_account_instance_bindings`). A
  Deploy with `start=false` after a blocking terminal exit writes a newer `DEPLOYED` binding
  (`_deploy_and_persist_lifecycle`), and the deploy route skips the crash-recovery precheck (it is guarded by
  `if daemon_request.start` in `live_instances.py`). A subsequent Start then sees `latest == DEPLOYED`, so
  `crash_retired_restart_blocking_binding` returns `None` and the same identity restarts without recovery proof.
- Scope: affects `crashed`, `liveness_unproven`, **and** `ended_without_status` identically — it predates this
  PR; BUG-10 inherits but does not create it. Retire-and-Replace (new instance id) is unaffected; only
  deploy-to-**same**-instance-while-blocked bypasses the gate.
- Fix options: (A) run the crash-recovery precheck before every `DEPLOYED` write on the deploy path (3-line
  router change, but touches the frozen `live_instances.py` and blocks deploy-staging during crash review —
  a workflow change requiring operator sign-off); (B) make the gate return the last *unresolved* blocking
  retirement even when a later non-terminal row exists (more complex state logic). **Deferred to a follow-up
  issue; not in PR #1289.**

**BUG-11 — 30s broker-stream-silence watchdog could fire during A0/A1 pressure** **[single-pass, medium]**
- Severity: MEDIUM · `account_clerk.py:163 _CRITICAL_BROKER_STREAM_SILENCE_MS = 30_000` vs a busy Clerk loop
  during 8-bot admissions/fills → possible spurious disconnect. Verify in drills.

**BUG-12 — Concurrent reconcile receipt writes are unguarded** **[verified]**
- Severity: MEDIUM/LOW · `services/account_reconciliation.py write_receipt` →
  `atomic_write_pydantic_artifact` with no advisory lock; under 8 bots' 15s sweeps + operator reconcile,
  last-write-wins leaves orphaned receipt events and fans out concurrent IBKR truth fetches. Fix: advisory
  lock around receipt write; gate `_write_automatic_clean_receipt` on it.

**BUG-13 — Stop desired-state self-heals in ~1s but UI reports `actuated=false`** **[single-pass, high]**
- Severity: LOW/MEDIUM (UX) · `risk_reducing_lifecycle_intent.py:150` returns `actuated=false` on command-
  enqueue OSError though `live_engine.py:2443 _apply_durable_desired_state` self-applies within the 1s poll.
  Fix: render "convergence pending", not "no stop in progress".

**BUG-14 — Narrow Popen→registration window can leak an ACTIVE binding** **[single-pass, high]**
- Severity: LOW/MEDIUM · `host_daemon.py:966` writes ACTIVE binding; a `BaseException` between Popen return
  (`:987`) and `_managed` registration (`:1057`) skips rollback (only `HostRunnerError`/`OSError` are caught)
  → leaked ACTIVE binding invisible to the reaper. Fix: `try/finally` compensating retire when `_managed`
  wasn't set but the binding was written.

**BUG-15 — `flatten_and_pause` after market close never executes (needs a bar)** **[single-pass, high]**
- Severity: LOW/MEDIUM · `live_instances.py:5166` returns `actuated=true` but the flatten runs on the next bar
  (`live_engine.py:1483`); with the source stalled/after close it never fires. Fix: fall back to STOP (which
  fires via the shutdown race, no bar needed) when the market is closed.

---

## Operator pre-run checklist (run TODAY / before first start, per account × 8)

**CHK-1 — Binding-ledger parity (fixes BUG-2):**
`GET /api/accounts/{account_id}/clerk` → if `binding.ledger_parity == "dirty"`,
`POST /api/accounts/{account_id}/binding-ledger/baseline` and confirm `parity_clean: true` in the response.

**CHK-2 — Clerk client-id identification (fixes BUG-3):**
`GET <LIVE_RUNNER_DAEMON_URL>/health` → inspect `clerks[]`; if any entry is `lease_valid: false` /
`status: "UNAVAILABLE"`, kill the orphaned Clerk process, restart the host daemon (boot reconcile clears the
unidentified state), then `POST <LIVE_RUNNER_DAEMON_URL>/accounts/{account_id}/clerk/ensure`. Confirm
`lease_valid: true` for all accounts before any start.

**CHK-3 — Stale nonterminal intents (fixes BUG-9):**
`GET /api/accounts/{account_id}/transactions?lifecycle_state=uncertain_requires_reconciliation` and
`...=recovery_action_required` → if any rows, they hold order-slots. **A journal cure will NOT free the slot**
(corrected per review): `POST /api/accounts/{account_id}/journal-cures` appends an `AccountClerkOperatorAdjustment`
that adjusts the *namespace exposure ledger* projection — it is invisible to `_custody_status_for_entries` and
writes no terminal custody event, so `nonterminal_async_custody_depths` still counts the slot. To actually
release a slot, drive the intent to a terminal custody state:
- `uncertain_requires_reconciliation` → run a reconciliation sweep (`POST /api/accounts/{account_id}/reconciliation`
  or the `reconcile_now` presented action). If broker truth confirms the order's terminal status, `record_broker_event`
  appends the matching `broker_event` which folds to `economic_terminal` and frees the slot.
- `recovery_action_required` → execute the presented `cancel_pending` action (only if still pre-A1/queued) or the
  `flatten` presented action, and await the terminal broker event.

**CHK-4 — A0 latency sanity (informs BUG-1):**
Run the custody qualification drills (or an 8-bot dry run) and confirm A0 admission p99 is comfortably under
the 10s deadline at 8-bot fsync load. If not, widen `ACCOUNT_CLERK_RPC_CUSTODY_RESPONSE_DEADLINE_S` first.

---

## Confirmed sound — do NOT spend time here

- **Sibling-halt cascade eliminated.** `SubmitUncertainHaltError` is a plain `RuntimeError`, not
  `ControlledLiveHaltError` (`live_portfolio.py:282`); it exits only the one bot. A known-intent
  `broker_uncertain` does **not** call `write_account_freeze` — that fires only for unattributed events
  (`receipt.intent is None`, `account_clerk.py:1503/1806`) or reconciliation failures. Siblings are unaffected. **[verified]**
- **Double-order on A0 retry is impossible.** `_enqueue_async_custody` returns early if
  `intent.intent_id in _async_custody_enqueued_ids` (`account_clerk.py:759`); the retry path finds the intent
  already queued/advanced and does not re-place. **[verified]**
- **Journal-cure 503 (SOCKET_MISSING) is fixed.** Container → `apply_journal_cure_endpoint` →
  `host_daemon_client.apply_operator_adjustment` → daemon ensures the Clerk (socket-ready) → local RPC. The
  container never touches the host socket directly. **[verified]**
- **Cross-runtime duplicate-sequence race is fixed** — not by a shared mutex but by per-producer-per-boot
  JSONL files (`producer_operational_log.py`); different runtimes write disjoint files. **[verified]**
- **Capacity gate boundary is correct:** `occupied < 8` admits exactly 8, refuses the 9th
  (`account_clerk.py:747`; drill asserts one refusal). **[verified]**
- **Boot-retire / reaper, `MARK_POISONED`, fleet-contamination, `delete` gate, presented-action O_EXCL claim**
  are all correctly per-bot scoped — one bot's failure does not halt or retire healthy siblings. **[single-pass, high]**

---

## Recommended sequencing

1. **Landed in PR #1289:** BUG-4 (cancel timeouts, inner + outer) and BUG-10 (ended_without_status restart
   gate, direct path). Do not re-apply.
2. **Before the run:** execute CHK-1..CHK-4 for all 8 accounts. These alone prevent the two morning-launch
   blockers (BUG-2, BUG-3) and the mid-session slot exhaustion (BUG-9), and de-risk BUG-1.
3. **Wind-down hardening (needed to end the day cleanly, deferred):** BUG-5, BUG-6, BUG-7 (validate against
   the paper broker).
4. **Follow-ups:** BUG-16 (deploy-only gate bypass — tracked issue), BUG-1 deadline widening (if drills show
   A0 pressure), BUG-8, BUG-11..BUG-15.
