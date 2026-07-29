# Bot-Control 8-Bot Call-Graph Audit — 2026-07-28

**Goal:** run 8 concurrent Interactive Brokers (IBKR) paper bots tomorrow without repeating the
2026-07-27 failure. **Method:** traced every bot-control UI action → FastAPI router → host daemon →
Clerk → broker, stack-trace style, then adversarially re-verified every cross-agent conflict and every
high-severity finding against the actual code. All tracing/verification ran on Sonnet subagents; this
synthesis is the only Opus step.

Line numbers are as cited by the tracing agents and are approximate — open the file to confirm before
editing. Findings tagged **[verified]** were re-read and quoted in a second pass; **[single-pass]** come
from one tracer at the stated confidence.

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

**BUG-4 — `cancel_exact_order` has only a 30s RPC budget but holds `_broker_write_lock`** **[verified]**
- Severity: HIGH · Confidence: high (refines/reattributes the earlier "cancel_namespace" claim)
- Pathway: `account_clerk_rpc_protocol.py:290–306` maps `cancel_exact_order` → 30s NORMAL; `account_clerk.py:1313`
  `async with self._broker_write_lock, self._cancel_operation_lock, ...`.
- What goes wrong: under 8-bot A1 contention, up to 8 broker writes (25s each) sit ahead on `_broker_write_lock`
  → ~200s worst case, exhausting the 30s budget after ~2 concurrent writes → RPC timeout → an uncertain cancel
  that can durably block that bot's next submit. (Note: `cancel_namespace`, `recovery_flatten`,
  `recovery_flatten_batch`, `fold_binding_retirements`, `record_binding_decision`, `drain_events` do **not**
  take `_broker_write_lock` and are adequately budgeted — the earlier BLOCKER on `cancel_namespace` is
  downgraded.)
- Fix: promote `cancel_exact_order` to `ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S` (120s) or a fleet-size-aware
  timeout. Small, safe change.

**BUG-5 — Stop on a crashed subprocess returns without flattening → position left open** **[single-pass, high]**
- Severity: HIGH · Pathway: `live_instances.py stop_run` → `host_daemon.py:1831` `if current.process.poll() is
  not None: return ... stop_outcome="exited"` (no flatten). The in-process recovery flatten
  (`run.py:2888–2976`) is itself fallible (broker disconnected → `"no live broker for recovery flatten"`).
- Fix: when a dead process still has owned-position evidence, auto-route to the Clerk emergency workflow or
  write a durable incident, instead of silently returning `exited`.

**BUG-6 — CLOCK_OUT leaves a bot alive-PAUSED with an open position if flat-evidence doesn't clear in 30s** **[single-pass, high]**
- Severity: HIGH · Pathway: `live_instances.py end_day_now` → `live_engine.py:_complete_clock_out` →
  `_await_fresh_flat_broker_evidence` (30s, `CLOCK_OUT_FLAT_EVIDENCE_TIMEOUT_S`) → on timeout writes
  `CLOCK_OUT_BROKER_NOT_FLAT`, acks "failed", `shutdown_event` NOT set → bot stays up, desired state PAUSED,
  no retry.
- Fix: on `CLOCK_OUT_BROKER_NOT_FLAT`, escalate to hard STOP + recovery flatten rather than staying alive
  PAUSED with exposure.

**BUG-7 — 8-bot end-day: serialized cancel starves later bots' 30s flat-evidence window** **[single-pass, medium-high]**
- Severity: HIGH (wind-down) · Pathway: 8× CLOCK_OUT → each `_cancel_namespace_through_clerk` → single
  per-account `cancel_operation_lock` (`account_clerk_operations.py:1289`, 25s each) → worst case ~200s while
  each bot's outer `CLOCK_OUT_FLAT_EVIDENCE_TIMEOUT_S` is only 30s → later bots write NOT_FLAT and strand.
- Fix: don't start the 30s flat-evidence window until the cancel actually completes, or wind down via
  per-bot Stop instead of 8 concurrent CLOCK_OUTs.

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

**BUG-10 — `ended_without_status` crash leaves a RETIRED row that reads as "clean stopped" but is unreconciled; crash-recovery-override can't clear it** **[single-pass, high]**
- Severity: MEDIUM · Pathway: `exit_taxonomy.py:39 TERMINAL_RESTART_BLOCKING_BINDING_SOURCES` includes
  `ended_without_status` but is **never imported/consumed**; `account_registry.py:424
  crash_retired_restart_blocking_binding` only checks `RECOVERY_REQUIRED_RETIRED_BINDING_SOURCES`
  {process_crashed, boot_liveness_unproven}. A SIGKILL/OOM exit reads as clean-stopped though exposure is
  unreconciled, and `record_crash_recovery_override_evidence` raises `CrashRecoveryNotRequiredError`.
- Fix: consume `TERMINAL_RESTART_BLOCKING_BINDING_SOURCES` in `crash_retired_restart_blocking_binding`.

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
`...=recovery_action_required` → if any rows, they hold order-slots. Clear each via
`POST /api/accounts/{account_id}/journal-cures` (Clerk must be running) before the session.

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

1. **Before the run:** execute CHK-1..CHK-4 for all 8 accounts. These alone prevent the two morning-launch
   blockers (BUG-2, BUG-3) and the mid-session slot exhaustion (BUG-9), and de-risk BUG-1.
2. **Quick safe code fix worth landing first:** BUG-4 (timeout bump for `cancel_exact_order`).
3. **Wind-down hardening (needed to end the day cleanly):** BUG-5, BUG-6, BUG-7.
4. **Follow-ups:** BUG-1 deadline widening (if drills show A0 pressure), BUG-8, BUG-10..BUG-15.
