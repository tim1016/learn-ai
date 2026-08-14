# PRD — Alpaca SQLite Historical Execution Recovery and Operator Action Reliability

- **Date:** 2026-08-13
- **Status:** Implemented — all three slices complete; awaiting CodeRabbit review
- **Product surfaces:** Alpaca Broker V2 Account Desk and bot Operator panel
- **Delivery posture:** Three Terra-sized tracer bullets; each slice is independently verifiable and must be labeled `ready-for-agent` when published
- **Authority:** The activated SQLite Account Clerk remains the sole custody and execution authority. Alpaca supplies external broker evidence. Angular renders backend-authored capabilities and never infers whether a recovery is safe.
- **Builds on:** ADR 0035, the pinned SQLite Clerk contracts, execution-coverage quarantine and resolution, the Broker V2 recovery catalog, and the manual paper qualification release gate

---

## 1. Executive summary

The activated Alpaca SQLite Clerk can detect an exact execution that conflicts
with an earlier cumulative recovery fill, quarantine complete exact evidence,
and resolve the closed one-exact-for-one-cumulative replacement case. That path
works for conflicts observed after the quarantine feature is present.

It does not provide a supported recovery path for conflicts that predate that
feature. Two current paper-account conflicts demonstrate the gap: the durable
uncertainties retain an Alpaca execution ID and Clerk order reference, while the
existing fill rows retain only cumulative quantity, price, and side. No exact
execution quarantine was recorded. The current resolver therefore refuses the
operation correctly, and account reconciliation cannot recreate the missing
per-execution economics because it reads order and position snapshots rather
than Alpaca account activities.

The UI compounds the dead end. The top Account Desk posture can recommend an
account comparison without showing an action. The bot Operator panel renders
`resolve_execution_coverage` and `open_custody_timeline` as readiness rows but
never attaches action controls. The account timeline has no search, filter, or
operation selection despite copy directing the operator to select an
operation. Finally, the recovery panel and top posture own independent
projection resources, so a completed action can leave the dominant warning
stale until a page reload.

This PRD adds a receipt-bound, bot-scoped historical execution recovery
ceremony and makes every backend-authored recovery capability discoverable,
accurate, and visibly refreshed. It does not relax any safety gate.

## 2. Problem statement

### 2.1 Historical conflicts cannot enter the existing proof vocabulary

The current execution-coverage resolver requires one durable exact execution
quarantine and one cumulative recovery fill. It proves that both rows describe
the same order, quantity, price, and side before replacing the cumulative row
without changing economic totals.

For a pre-feature conflict, the Clerk has enough identity to ask Alpaca for the
authoritative execution but not enough economics to synthesize it:

- uncertainty ID;
- strategy instance and Clerk order reference;
- Alpaca execution ID;
- cumulative recovery fill quantity, price, and side;
- the account, authority generation, database identity, and control revision.

It does not have the exact execution quantity and price. Copying the cumulative
values into an exact record would fabricate evidence and is forbidden.

### 2.2 Reconciliation is useful but cannot repair execution identity

`Reconcile now` performs the same account-order/position comparison as the
automatic reconciliation sweep and adds an operator receipt. REST order
snapshots carry cumulative fill state; they are not a source of exact execution
slices. Repeated reconciliation can refresh account truth and resolve ordinary
order-state uncertainty, but it cannot prove which exact execution produced an
aggregate fill.

The product must stop presenting reconciliation as the cure for an execution-
coverage conflict when the required next operation is historical exact-evidence
recovery.

### 2.3 Available actions are hidden or misleading

The backend already owns the closed action catalog and availability reasons,
but the two Broker V2 surfaces do not render it consistently:

- the Account Desk posture hides its action whenever backend guidance marks an
  uncertainty as review-only;
- the deeper Account Desk recovery panel has the real account-scoped actions;
- the bot Operator readiness map suppresses controls for coverage resolution
  and timeline opening;
- the Account custody card is explicitly given no Reconcile action;
- error handling discards most backend-authored reason and remediation detail.

An operator cannot tell the difference between “this action is intentionally
blocked,” “this row must be expanded,” and “the client forgot to wire the
action.”

### 2.4 Evidence views do not support the instructed task

The account timeline can contain thousands of transitions but only offers
newest-first paging. It cannot filter by bot, order reference, uncertainty,
execution ID, transition kind, or sequence. The policy copy instructs the
operator to select an operation, but the view provides no selection control.

## 3. Goals

1. Recover authoritative exact Alpaca execution evidence for a historical,
   bot-scoped coverage conflict without broker mutation or manual database
   edits.
2. Reuse the existing quarantine and no-economic-delta resolution semantics;
   do not create a second coverage model.
3. Bind every prepare/confirm operation to the selected account, authority
   generation, database identity, control revision, bot, uncertainty, order,
   execution, and cumulative fill.
4. Render every backend-authored recovery action as either an actionable
   control or an explicit unavailable control with the exact reason and next
   step.
5. Refresh all visible projections after an action so the dominant status,
   detailed recovery panel, bot panel, readiness counts, and timeline agree.
6. Make the relevant evidence directly findable from an uncertainty or action.

## 4. Non-goals and safety boundaries

- No raw SQLite editing, backfill script that invents economics, or mutation of
  immutable historical transitions.
- No generic Clear, Force, Retry, Ignore, or blind Flatten capability.
- No broker order placement, cancellation, position mutation, or live-account
  support in the historical evidence recovery ceremony.
- No development on the deprecated IBKR bot-control or navigation surfaces.
- No automatic resolution merely because an Alpaca activity shares an order
  ID. Exact execution identity and economics must pass the closed proof.
- No enablement of `ALPACA_SQLITE_MANUAL_TRADING_ENABLED`. Manual paper
  qualification remains a separate supervised, dated, human-run release gate.
- No attempt to make `Prepare safe flatten` submit an order; it remains a
  read-only plan.

## 5. Locked recovery contract

### 5.1 Prepare is broker-read-only

For one bot-scoped `EXECUTION_COVERAGE_CONFLICT`, the server queries the
authoritative Alpaca paper account activity source for the retained execution
ID. The query is bounded by the conflict evidence time and paginated with a
strict maximum. It fails closed if the activity is absent, duplicated,
malformed, belongs to another account/order, or cannot be uniquely associated
with the Clerk order.

A successful prepare returns a short-lived, signed recovery plan containing:

- account ID (prepare separately verifies the selected account remains `paper`
  before it issues any plan);
- authority generation, database identity, and control revision;
- strategy instance, uncertainty ID, Clerk order reference, and broker order
  identity;
- exact Alpaca execution ID, quantity, price, side, and source timestamp;
- cumulative fill ID, quantity, price, and side;
- expiry and an opaque confirmation token.

Prepare performs no SQLite write and no broker mutation.

### 5.2 Confirm appends evidence; it never rewrites history

Confirm rechecks the signed plan and current Clerk context. If any bound fact
changed, it returns a typed stale-plan response and appends nothing. Otherwise,
one Clerk transaction:

1. appends the existing typed exact-execution quarantine transition using the
   authoritative Alpaca activity;
2. invokes the existing execution-coverage proof;
3. appends the existing resolution transition only when the closed
   one-exact-for-one-cumulative proof succeeds; and
4. returns a durable receipt covering both appended transitions and the
   unchanged economic total.

A repeated confirmation returns the original durable receipt identity with
`applied=false`. It must not append duplicate quarantine, resolution, fill, or
position changes.

### 5.3 Failure remains actionable and fail-closed

Every refusal returns a stable reason code, backend-authored operator message,
and next step. Required cases include:

- authoritative execution not found;
- more than one matching activity;
- account, order, execution, side, quantity, or price mismatch;
- cumulative fill missing or superseded;
- multiple cumulative fills or exact executions;
- stale authority generation, database identity, revision, or plan expiry;
- live Alpaca account mode;
- broker history temporarily unavailable.

None of these failures clears the uncertainty or permits new exposure.

## 6. Terra-sized tracer bullets

### Slice A — Recover one historical exact execution end to end

**Blocked by:** None — can start immediately.

Deliver one complete path from a bot-scoped conflict through an authoritative
Alpaca activity preview, typed confirmation, durable quarantine, existing
coverage resolution, refreshed bot projection, and operator receipt.

Acceptance criteria:

- [x] A bot-scoped historical conflict with an execution ID and cumulative fill
      offers **Recover exact execution evidence**; account scope with multiple
      conflicts directs the operator to choose a bot.
- [x] Prepare reads only the selected Alpaca paper account and returns the
      signed, expiring plan defined in §5.1 without changing Clerk revision.
- [x] Confirm rechecks every bound identity and appends quarantine plus
      resolution atomically through the Clerk repository.
- [x] Successful recovery changes no account or bot economic quantity, price,
      realized P&L, or FIFO total except for replacing the evidence source used
      by the existing projection.
- [x] Duplicate confirmation is idempotent and returns the original receipt.
- [x] Missing, ambiguous, mismatched, stale, live-mode, and unavailable-broker
      cases remain blocked with typed remediation.
- [x] Regression fixtures reproduce a pre-quarantine conflict and prove the
      full prepare/confirm route, repository transitions, projection, and bot UI
      behavior.
- [x] The existing fresh-websocket quarantine and resolution path remains
      unchanged and green.

### Slice B — Make every recovery capability discoverable at the correct scope

**Blocked by:** Slice A only for the new recovery control; existing action
wiring can be implemented and tested in parallel.

Deliver consistent Account Desk and bot Operator controls driven exclusively by
the backend recovery catalog.

Acceptance criteria:

- [x] The Account Desk dominant posture shows its backend-selected primary
      action when available and an explicit disabled affordance with reason when
      unavailable.
- [x] `Reconcile now`, `resolve_execution_coverage`, historical exact-evidence
      recovery, `cancel_verified_working_orders`, `prepare_safe_flatten`,
      `stop_bot_decisions`, and `open_custody_timeline` render consistently at
      their supported account or bot scope.
- [x] The bot Operator panel exposes real controls for coverage recovery and
      timeline opening instead of readiness-only rows.
- [x] The Account custody card can expose Reconcile without duplicating a
      conflicting second action implementation.
- [x] A control that requires expansion states that clearly; the primary action
      never depends on discovering a hidden inner button.
- [x] Unavailable actions preserve the backend reason code through the shared
      receipt-label vocabulary and render backend-authored prose unchanged.
- [x] The operator manual and pinned recovery vocabulary list the same actions
      as the generated contract snapshot.
- [x] Angular interaction tests cover visible, disabled, confirmed, stale-token,
      success-receipt, and timeline-navigation behavior for account and bot
      scope.

### Slice C — Synchronize post-action state and make custody evidence findable

**Blocked by:** None — can start immediately.

Deliver one shared refresh boundary plus a directly navigable, filtered custody
timeline and lossless typed error presentation.

Acceptance criteria:

- [x] Account posture and detailed recovery controls consume one shared
      projection resource or one explicit shared invalidation mechanism.
- [x] After any recovery success, idempotent replay, or typed refusal, every
      visible custody projection refreshes from the new server revision without
      a browser reload.
- [x] The timeline endpoint and UI support exact filters for bot, order
      reference, uncertainty ID, execution ID, transition kind, and sequence.
- [x] **Open custody timeline** deep-links with the action's evidence filters and
      brings the relevant transition into view.
- [x] Operation selection is real: selecting a timeline row updates the
      transaction/evidence detail using the selected immutable reference.
- [x] Paging remains keyset-based, stable under concurrent appends, and exposes
      total/high-water metadata without scanning the full journal in Angular.
- [x] Typed backend errors retain reason, message, remediation, and refreshed
      capability; the UI no longer collapses all non-409 failures into one
      generic sentence.
- [x] Tests prove cross-component refresh, deep-link/filter round trips,
      keyset stability, empty results, and typed error rendering.

## 7. Verification and evidence

Each slice must ship a regression that fails against the current behavior and
passes after the change.

The minimum release evidence is:

- Python repository tests proving immutable append-only recovery, exact
  economic no-delta behavior, idempotency, and stale binding rejection;
- FastAPI route tests with an Alpaca activity-port fake covering unique,
  missing, ambiguous, mismatched, live-mode, and unavailable cases;
- SQLite projection tests proving the uncertainty clears only after the exact
  proof and that reconciliation alone does not clear it;
- Angular Testing Library tests for action discoverability, bot/account scope,
  refresh synchronization, typed errors, and filtered timeline navigation;
- contract/vocabulary snapshot updates for every new action or reason code;
- a broker-free deterministic historical-conflict rehearsal artifact suitable
  for the dated paper qualification audit.

No slice is complete if it requires a raw database edit, a direct broker-console
order, or manual page reload to demonstrate success.

## 8. Rollout

1. Land Slice A behind the existing SQLite/manual-paper safety posture; the
   recovery endpoint itself accepts paper mode only.
2. Land Slices B and C independently once their contract fixtures are pinned.
3. Rehearse historical conflict recovery against a disposable paper authority.
4. Under human supervision, recover each real conflict one bot at a time and
   archive the receipts.
5. Run a fresh account reconciliation and verify outstanding intents,
   attributed positions, FIFO/account history, mirror/hash head, and bot-start
   admission.
6. Continue the separate manual-order paper qualification ceremony. Enabling
   manual tickets is not an automatic consequence of this PRD.

## 9. Issue publication proposal

Publish the three slices above in dependency order:

1. **Recover one historical Alpaca execution through Clerk proof** — no blocker.
2. **Expose every Clerk recovery action at its correct Operator scope** — the
   new action depends on issue 1; existing wiring can proceed in parallel.
3. **Synchronize Operator recovery state and add evidence-targeted timelines** —
   no blocker.
