# Alpaca Stop and Clear Hold — hard exposure fencing design

**Date:** 2026-08-03

**Input:**
- [`docs/audits/alpaca-bot-control-panel-architecture-audit-2026-08-02.md`](../../audits/alpaca-bot-control-panel-architecture-audit-2026-08-02.md) — P0-1, P0-2, P0-3
- [`docs/architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md`](../../architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md) — Workstream A (§4), specifically A1, A1.1, A2, A3 and the Workstream A exit gate
- [`docs/architecture/adrs/0034-immutable-strategy-instances-append-only-runs.md`](../../architecture/adrs/0034-immutable-strategy-instances-append-only-runs.md) — accepted design for A1

**Purpose:** Close the Workstream A exit gate. The gate requires all three P0s (P0-1 deploy-rebind, P0-2 clear-hold, P0-3 orphaned-stop) closed together, each with a deterministic failing-before/passing-after test, with no fix weakening another. This document is scoped to Workstream A only — Workstreams B–E (command state machine, causal evidence, projection/journal scale, durability/security boundary) are explicitly out of scope; they are unrelated to exposure fencing and remain gated separately.

**Status of the gate today:** two of three P0s are closed in code but the gate line-item ("every P0 has a deterministic test") is not yet fully true — see Section 1. P0-2 and P0-3 are open in code. This document designs their closure and specifies the one missing test plus one stale doc line for the already-closed P0-1.

## 1. Current-state findings (verified against code on `master`, 2026-08-03)

### 1.1 P0-1 / A1 (deploy can overwrite a stopped instance's binding) — closed in code, missing one regression test

`evaluate_run_admission` (`PythonDataService/app/services/run_admission.py:164-179`) blocks Start whenever `bot.process.state != "ABSENT"`, returning `STRATEGY_INSTANCE_ALREADY_EXISTS` for a terminal (stopped) instance or `RUN_ALREADY_ACTIVE` for a live one. `process_fact()` (`bot_runner.py:759-775`) derives that state from durable artifacts (`binding` + `lifecycle_repo`), not the in-memory task dict, so the block survives reap and process restart. This shipped via the `codex/alpaca-control-pr*` commit stack (`7c08c1a6` "feat: add safe bot run actions" and siblings), landed on `master` before this document, and is covered by ADR 0034.

Gap: no test exercises the literal audit reproduction end-to-end through `BotTaskRegistry` — deploy → stop → redeploy the same SID with a *different* strategy/symbol must be rejected, with the original binding bytes unchanged. `test_deploy_while_running_is_refused` (`tests/services/test_bot_runner.py:655`) covers the *running* case only; its stopped-instance sibling doesn't exist.

**Required action:** add `test_deploy_after_stop_with_changed_configuration_is_refused` (name indicative) to `tests/services/test_bot_runner.py`. No production code change — the behavior is already correct.

### 1.2 P0-1 sibling / A1.1 (Continue, Resume, Dry Run as distinct operations) — closed in code, one doc line is stale

All three operations are correctly identity-scoped and tested:

- **Continue** (`bot_runner.py:591-616` `continue_paused`, paired with `pause` at `:557-589`) keeps the same `run_id`, requires `DesiredState.PAUSED`, requires an actually-alive task (`require_live_managed_bot`). Proven by `test_pause_and_continue_keep_the_same_live_run_id` (`test_bot_runner.py:1110-1127`).
- **Resume** (`bot_runner.py:346-398`, `BotResumeAdmission` in `bot_resume_admission.py`) mints a new `run_id` via `new_run_binding()`, carries `prior_run_id`, and only activates under a fresh Clerk custody cut with exact checkpoint/exposure-carryover matching (`run_admission.py:278-325`). Proven by `test_resume_existing_creates_new_run_and_preserves_action_plan` (`test_bot_runner.py:909-951`).
- **Dry Run** (`run_dry_run_bot`, `bot_trade_strategy.py:202-238`) never imports or calls the Clerk. Proven by `test_dry_run_records_simulated_round_trip_with_zero_broker_writes` (`test_bot_runner.py:1686-1715`), which asserts `clerk.calls == []`.

Note: the research plan named the existing IBKR `shadow`/no-submit engine path as "the leading implementation candidate" for Dry Run. What actually shipped is a separate, purpose-built Alpaca mechanism (`bot_dry_run.py`) that doesn't reuse that path. This is a reasonable, defensible divergence (the zero-broker-write guarantee is independently proven by the test above) — not a defect, and not something this document proposes to unwind.

Stale doc line: the research plan (§2.1 item 3, and the A1.1 table) says the panel "must not render a Resume button" until new-run behavior "exists end to end." It now does — Resume is implemented, admission-gated, and tested — so the panel correctly renders it **disabled with a reason** (`action_policy.py:129-155` `_guard_resume`, driven by the same typed `RunAdmissionDecision` the backend evaluates before mutation) rather than hidden. This satisfies the *intent* of the precondition (no dishonest availability) now that the precondition itself is met.

**Required action:** update the research-plan doc's A1.1 table and §2.1 item 3 to record Resume-button visibility as resolved (disabled-with-reason, backend-authored), so it stops reading as an open gap. No code change.

### 1.3 P0-3 / A2 (Stop ignores cancellation timeout; no fence at the broker-write boundary) — open

Confirmed directly against current code:

- `_stop_locked` (`bot_runner.py:618-674`) writes `DesiredState.STOPPED` durably *before* cancellation (correct ordering), calls `managed.task.cancel()`, then `await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)` — **the returned `(done, pending)` tuple is discarded** (line 653). `finalize(kind="STOPPED", ...)` (657-661) and `reap()` (662) then run unconditionally, regardless of whether the task actually terminated.
- `_resolve_enter` (`app/broker/alpaca/clerk/effects.py:233-252`) — the Clerk's ENTER admission path, reached by every bot decision — checks only `derive.hold_state(entries).active`. It does not read `DesiredState`, `RunProcessAdmissionFact`, or any run-generation value for the instance. A coroutine that survives `task.cancel()` and later calls `execute_for_instance(purpose=ENTER, ...)` is admitted exactly as if Stop had never been called, unless an unrelated account-wide hold happens to be active.

### 1.4 P0-2 / A3 (Clear Hold does not prove the hold's root condition recovered) — open

Confirmed directly against current code: `clear_hold` (`clerk.py:563-582`) checks only `self._channel_fresh()` before appending `HOLD_CLEARED` via `_clear_hold_locked` (`clerk.py:584-619`). For an `UNEXPLAINED_ORDER_HOLD`, nothing re-verifies the foreign order is actually gone — an operator-supplied free-text `reason` is sufficient as long as the (unrelated) stream-health channels are up.

## 2. Design: A2 — Stop fencing

### 2.1 The fence

Add a check at the top of `_resolve_enter`, immediately above the existing `hold.active` check, under the same `_intake_lock` the effect resolution already runs inside:

- Read the instance's durable `DesiredState` (or `RunProcessAdmissionFact.state`, which already models `STARTING | RUNNING | STOPPING | EXITED | UNKNOWN` — this schema was built for A2 but is currently only consulted at Start/Resume admission time, never at the Clerk's per-decision boundary).
- If the state is not `RUNNING`, reject with a typed terminal receipt (new reason code, e.g. `RUN_GENERATION_REVOKED`) instead of proceeding to the exposure/working-order checks.

This reuses the exact durable field `_stop_locked` already writes *first* — no new durable field, no new lock, no new cross-module wiring beyond a read the Clerk doesn't currently perform.

Effects already past this point when Stop lands (i.e., already inside `_submit_leg`, "accepted, shielded") are not retroactively cancelled — they resolve to their real terminal state. Only effects that reach `_resolve_enter` *after* the durable Stop write are refused. This matches the audit's framing precisely (a coroutine that decided *before* Stop but reaches the Clerk *after* Stop is the exact race being closed) and is why the check must sit at this exact choke point rather than earlier in the bot's own decision loop — an early check can itself be raced by the same class of bug it's trying to close.

EXIT/reduction operations remain exempt from this fence (per the research plan's working hypothesis and the repo's existing P6 invariant that reductions are never blocked) — they reduce exposure, not increase it, and closing this specific race is about preventing *new* exposure after Stop.

### 2.2 Fix `_stop_locked`'s unconditional finalize

Stop inspecting the `asyncio.wait` result instead of discarding it:

- If the task is in `done`: proceed exactly as today (finalize STOPPED, reap, prove terminal outcome).
- If the task is in `pending` (survived the timeout): do **not** call `finalize(kind="STOPPED", ...)` and do **not** call `reap()` — those are precisely the two calls that let a live task be reported terminated and then orphaned from supervision (nothing can find or cancel it again after `reap()` removes the registry entry). Instead:
  - Persist a non-terminal outcome (`EXITED_UNVERIFIED`-shaped, or a new `STOPPING`-with-timeout state — reuse the existing exit-taxonomy pattern documented at the top of `bot_runner.py` rather than inventing a fourth kind).
  - Do not remove the task from `self._bots` — it must remain reachable for a later cancellation retry or operator-visible escalation.
  - Log and surface an operator-facing alert. Per the accepted scope decision (Section 4 below), this is **per-instance only** — it does not touch any other bot or place an account-wide hold. The fence in 2.1 already makes the survivor harmless to the broker; this escalation is about honesty and operator awareness, not an additional safety backstop.

### 2.3 What this does not change

- The Clerk remains the sole broker-write authority (unchanged architectural invariant, per the 2026-08-03 hardening doc).
- No new lock is introduced. The fence read happens inside the Clerk's existing `_intake_lock` critical section.
- IBKR is out of scope for this slice — it is a structurally different Clerk (RPC/host-daemon-based per the "two-Clerk correction"). Extending this fence pattern to IBKR is a separate, later slice; this document's fence is Alpaca-only.

## 3. Design: A3 — Clear Hold reason-specific proof

### 3.1 Reason-code dispatch

`derive.hold_state(entries)` already returns a `reason_code`. Add a dispatch inside `_clear_hold_locked` (or immediately before it, still under `_intake_lock` for the append itself):

- `STREAM_HEALTH_HOLD_CODE` → keep the existing `_channel_fresh()` check (unchanged; already reason-appropriate).
- `UNEXPLAINED_ORDER_HOLD_CODE` → require a fresh reconciliation proving zero unexplained orders, zero unresolved intents, no incompatible working orders, and no custody freeze (see 3.2).
- Any other/unregistered reason code → refuse the clear outright (fail closed). This makes a future new hold reason safe-by-default: it cannot be cleared until someone deliberately registers a proof for it.

### 3.2 Reconciliation timing and the TOCTOU gap

`_reconcile_with_proof` (`clerk.py:1019-1093`) already exists and does the real broker round-trip (`list_orders` + `list_positions`, a genuine 5-15s REST call per its own comment). It cannot run while holding `_intake_lock` for its full duration without blocking every other submission on the account for that long.

Per the accepted scope decision (Section 4 below — one-step UX), the design is:

1. Operator calls Clear Hold once. The backend runs `_reconcile_with_proof` **outside** the intake lock (as it already does).
2. If the reconciliation verdict is not clean, refuse the clear immediately with the reconciliation's own explanation — no HOLD_CLEARED entry is written.
3. If clean, re-acquire `_intake_lock` and, before appending `HOLD_CLEARED`, re-check that no new `UNEXPLAINED_ORDER` (or other hold-relevant) journal entry was appended after the reconciliation's `observed_at_ms`. If one was, refuse — a new foreign order arrived in the window between the broker read and the lock, and the stale proof must not clear the hold. If none arrived, append `HOLD_CLEARED` with the reconciliation's proof reference, observation time, and journal sequence recorded on the receipt.

This closes the exact race the research plan's required experiment #3 names ("reconcile cleanly, then race a new foreign observation against clear under the intake lock") without holding the lock for the broker round-trip.

### 3.3 Receipt shape

`HOLD_CLEARED` receipts gain: `reason_code` (already present), a proof reference (the reconciliation verdict ID or `"stream_health_check"` for the other branch), the observation timestamp, and the journal sequence at observation. This is additive to the existing entry — no schema break for existing readers that only look at `reason_code`.

## 4. Accepted scope decisions

Two judgment calls were made explicitly with the user rather than assumed:

1. **Stop-timeout escalation is per-instance only** (mark `STOPPING`/`EXITED_UNVERIFIED`, alert) — not an account-wide hold. Rationale: the ENTER fence (Section 2.1) already makes a timed-out survivor harmless to the broker regardless of escalation scope; an account-wide hold would stop healthy, unrelated bots for a narrow single-instance race, which is a real availability cost not justified once the fence exists. Account-wide hold remains available as a *manual* operator escalation path if a timeout pattern later suggests something systemic, not as this fix's automatic behavior.
2. **Clear Hold is one-step**: reconciliation happens inside the Clear Hold call itself, not as a separately-required prior step. Rationale: simpler operator flow (one button, one wait) with no loss of safety — the reconciliation proof is still fresh, still checked under the lock at commit time, and still fails closed on any drift.

## 5. Regression test plan

### A2 — Stop fencing (`tests/services/test_bot_runner.py`, `tests/broker/alpaca/clerk/`)

Reusing the audit's cancellation-suppressing feed fixture:

1. Stop lands before the coroutine's decision — trivial baseline; decision path sees `STOPPED` and never reaches the Clerk.
2. Stop lands after the coroutine decides, before `_resolve_enter` — refused at the fence. **This is the core case.**
3. Stop lands after `_resolve_enter`'s fence check passes but before `_submit_leg` returns — **not** refused; the in-flight submit completes and resolves to its real terminal state (proves the fence doesn't retroactively break "accepted, shielded" effects).
4. Stop lands during a broker timeout / delayed ack on an already-in-flight submit — that submit still resolves normally; the *next* decision from the same survivor is refused.
5. A lifecycle event (fill, foreign order) arrives concurrently with Stop — fence and journal ordering stay consistent; no interleaving lets a late ENTER slip through.
6. Process restart while `STOPPING` — boot recovery preserves the fence; Resume is blocked until terminal evidence exists; the instance is not silently promoted back to `RUNNING`.
7. The `_stop_locked` timeout path itself: cancellation-suppressing feed survives past `_STOP_TIMEOUT_S` → assert no `OPERATOR_STOP` terminal receipt, no reap, a non-terminal state persists, and a subsequent decision from the survivor is still refused by the Section 2.1 fence. (Tests 6 and 7 together are what make P0-3 "closed together, not fixed by weakening the other" — the fence alone without the finalize/reap fix would still let the UI lie about the bot being stopped.)

### A3 — Clear Hold (`tests/broker/alpaca/clerk/test_clerk_reconciliation.py`)

Per the research plan's required experiments:

1. Foreign order present → clear rejected.
2. Foreign order removed but reconciliation never run → clear rejected (no stale-assumption clear).
3. Clean reconcile, then a *new* foreign order arrives before the lock-protected re-check → rejected (the TOCTOU case, Section 3.2 step 3).
4. Stream-health freshness boundary conditions (immediately below / at / above the TTL) for the `STREAM_HEALTH_HOLD` branch.

Note: `test_clerk_reconciliation.py:530-543` currently *pins the bug* (asserts the unsafe clear-while-foreign-order-present behavior succeeds). That assertion must be inverted as part of this work, not left in place alongside the new safe-path tests.

### A1 backfill (`tests/services/test_bot_runner.py`)

5. Deploy → stop → redeploy same SID with a changed strategy/symbol → rejected with `STRATEGY_INSTANCE_ALREADY_EXISTS`; original binding bytes unchanged (Section 1.1).

## 6. Decision artifacts and definition of done

- ADR amendment extending 0033 (account custody clocks and safety contract) with the run-generation fence and the non-terminal timeout-escalation behavior. (0033 already covers account-epoch fencing; this is the bot-run-granularity extension the original audit noted was missing.)
- The `HoldClearAdmission`-shaped reason-code dispatch documented alongside `derive.hold_state` in the Clerk module docstring.
- Research-plan doc updated per Section 1.2 (Resume-button line).
- All tests in Section 5 green; the existing 78 tests in `test_run_admission.py` + `test_bot_runner.py`, plus the Clerk/panel suites, stay green.
- Workstream A exit gate (research plan §4) fully satisfied:
  - Every P0 (P0-1, P0-2, P0-3) has a deterministic test against current behavior — true after Section 5.
  - The chosen design closes all three together without weakening any — true: A1's existing fix is untouched; A2/A3 are additive checks at existing choke points.
  - Migration and crash-cut behavior specified — Section 2.2 (task pending at restart) and Section 3.2 (reconciliation observed-at vs. lock re-check) cover the two relevant cut points.
  - No implementation issue for Start, Stop, Deploy, or Clear Hold was opened before this document — this document *is* that gate-closing artifact for A2/A3; A1/A1.1 were already implemented and are backfilled here, not re-opened.

## 7. Non-goals

- Workstreams B (command state machine), C (causal evidence), D (projection/journal scale), E (durability/security boundary) — untouched by this document; they remain separately gated.
- IBKR Stop/Clear-Hold fencing — the IBKR Clerk is architecturally distinct (RPC/host-daemon); extending this pattern there is a future slice, not this one.
- Re-litigating the Dry Run implementation choice (Section 1.2) — functionally proven correct; not being changed to match the doc's original "reuse shadow path" suggestion.

## 8. Addendum (2026-08-03, during plan-writing)

Section 1.3's description of Stop is accurate but incomplete. Current
`_stop_locked` already calls, after the provisional finalize/reap,
`prove_terminal_stop_outcome` → `prove_stop_outcome`
(`app/services/bot_carryover.py:122-171`), which:

1. cancels Clerk-owned working ENTER orders
   (`clerk.cancel_working_entries_for_instance`);
2. obtains a fresh custody proof (`clerk.prove_instance_custody`,
   backed by the same `_reconcile_with_proof` this plan's Clear Hold
   fix also uses);
3. records one of `STOPPED_FLAT`, `STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE`,
   `STOP_REQUIRES_FLATTEN`, or `STOPPED_CUSTODY_UNPROVABLE` as the
   terminal `reason_code`, persisted as a carryover checkpoint.

This is independent of, and unaffected by, Sections 2.1/2.2's design
(the ENTER fence and the finalize/reap-on-pending fix) — it runs after
both, and neither changes its inputs or outputs.

**Flagged, explicitly out of scope:** `BotRunTerminalRecorder
.replace_provisional_stop` (`bot_run_terminal.py:79-97`) hardcodes
`kind="STOPPED"` regardless of which `StopCustodyOutcome` reason code
is passed — so a `STOPPED_CUSTODY_UNPROVABLE` outcome still reports
`kind="STOPPED"`, only distinguishable by reading `reason_code`. This
is a real "terminal words require terminal evidence" gap (decision
principle #4), but fixing it means changing the shared
`BotDutyOutcomeKind` enum's meaning or adding a new value, which
ripples into every other consumer of that enum (receipt labels,
frontend `receiptLabel` mappings, other terminal-state readers). That
is a broader change than this exposure-fencing slice and is not
included here. Tracked as a follow-up, not fixed in this plan.
