> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, ADR-0026, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** The relevant account-triage decisions are shipped and absorbed by the current authority set.

# PRD: Bot Control Account Triage and Reconciliation Pages

**Status:** Draft, revised after skeptical review
**Owner:** Inkant
**Created:** 2026-07-02
**Implementation order:** Start only after PR #761 (`codex/bot-control-inline-workbench`) is merged into `master`.
**Builds on:** PR #761, `docs/architecture/ibkr-account-truth-cross-bot-validation-prd.md`, `docs/architecture/operator-notice-prd.md`, `docs/architecture/bot-control-inspector-receipts-prd.md`, `docs/bot-lifecycle-account-owner-authority.md`, `docs/runbooks/live-trade-reconciliation.md`.
**Surfaces:** `/broker/bots/:id`, `/broker/account-monitor`, `/broker/orders`, `/broker/reconciliation`, and per-bot Activity/Audit drill-downs.
**Data plane:** Python FastAPI. Angular renders backend-authored account triage, recovery actions, labels, and evidence. No GraphQL is introduced for this surface.
**Binding rules:** Python owns all numerical and operational verdicts. Angular may format, filter, route, and render; it must not compute account cleanliness, recovery eligibility, submit safety, ownership, or reconciliation pass/fail.
**Core architectural decision:** Account-level reconciliation is a durable receipt of the existing `account_truth` verdict, not a second cleanliness engine.

**2026-07-02 implementation snapshot:** Initial S0/S1 spine is implemented. Python now has `AccountReconciliationReceipt`, `AccountTriageResponse`, `AccountReconciliationService`, `POST /api/accounts/{account_id}/reconciliation`, `GET /api/accounts/{account_id}/reconciliation/latest`, and `GET /api/accounts/{account_id}/triage`. Account Monitor can display and mint the account-level receipt. Remaining S0 work: durable cancel guard keyed by `perm_id`/`order_ref`, enforced idempotency for recovery mutations, and the full cross-status translation table beyond the current account reconciliation row.

---

## 1. Dependency on PR #761

This PRD is a follow-on to PR #761, not a replacement for it. Implementation must wait until PR #761 is merged and the branch is rebased on top of it.

PR #761 establishes several assumptions this PRD depends on:

1. Bot Control uses an inline workbench for recent Activity and full Audit Trail instead of the previous bottom overlay.
2. Account Monitor includes account-truth execution history grouped by bot/day.
3. Orders is stabilized around account-truth sweeps rather than stream rows alone.
4. Orders favors durable broker/account identities: `order_ref`, broker `perm_id`, and broker `exec_id`; `order_id` is not treated as durable historical identity.
5. Completed-order quantity mapping preserves filled quantity and execution evidence when IBKR returns zeroed completed-order totals.
6. Timestamp authority wording has been corrected around `int64 ms UTC` storage/wire values and local display.

This PRD should not re-implement those changes. It should build the next layer: account-level triage and recovery workflows that use those cleaner account-truth and ledger surfaces.

## 2. Problem

The Bot Control page can tell an operator that a bot is not safe to submit, paused, stale, frozen, poisoned, unreconciled, or blocked. The broker pages can show account facts, broker orders, execution history, and partial reconciliation evidence. But the relationship between those surfaces is still weak.

When a bot is hung or cannot continue, the operator needs to answer three questions quickly:

1. **Why is this bot blocked right now?**
2. **Which account fact, order, execution, position, freeze, or receipt caused the block?**
3. **What corrective action can safely clear the block, and where do I perform it?**

Today that answer is scattered:

- Bot Control has `operator_surface.trader_guidance`, but its broker-page links are shallow.
- Account Monitor shows account truth, P&L, owner exposure, and execution history, but it is not a guided recovery surface for a blocked bot.
- Orders shows broker order and execution evidence, but rows do not consistently explain whether they are blocking a bot or how to resolve them.
- Reconciliation is still shaped primarily as "IBKR vs Engine" numeric comparison; it is not yet the operational recovery hub for stale/failed/adopted receipts, orphan adoption, poison reasons, and account freeze proof.
- Account freeze clearing is implemented at the artifact layer through `clear_account_freeze(...)`, but there is no operator flow that creates a backend-validated recovery proof or audited override from the UI.

The result is dangerous ambiguity. Operators can see pieces of evidence, but the product does not yet guide them from "bot blocked" to "account problem found" to "proof recorded" to "bot can continue."

## 3. Product Goal

Turn Account Monitor, Orders, and Reconciliation into **bot recovery lenses** for Bot Control.

Bot Control remains the primary control panel and the source of operator intent. The three broker pages become bidirectionally linked evidence and remediation lenses:

- Bot Control points the operator to the exact account/order/reconciliation evidence that blocks the bot.
- Each broker page shows when its rows are blocking or relevant to a specific bot.
- Corrective actions are backend-authored, guarded, auditable, and routed back to Bot Control after completion.

The target operator experience:

> "This bot cannot resume because account `DU...` is frozen after restart intensity. Open Account Monitor, inspect the freeze evidence, run account truth and reconciliation, record a recovery proof if clean, then return to Bot Control and resume."

or:

> "This bot cannot resume because broker order `learn-ai/JUNE-25/v1:...` is an owned active orphan. Open Reconciliation, adopt it into engine state, then verify the bot remains paused until the ambiguous exposure is resolved."

or:

> "This bot cannot submit because IBKR has an open order with no recognized namespace. Open Orders, inspect the foreign/unclaimed order, cancel through a guarded workflow or record an audited override, then rerun reconciliation."

## 4. Non-Goals

- Real-money live trading.
- Moving canonical math, ownership, reconciliation, or safety verdicts into Angular.
- Replacing Bot Control as the operator command surface.
- Replacing Account Truth.
- Replacing Activity/Audit as the per-bot evidence trail.
- Using Client Portal API in the live recovery path.
- Auto-classifying unstamped TWS hand-clicked orders as safe manual orders.
- Silently clearing account freezes.
- Clearing foreign or unknown exposure without broker evidence, reconciliation evidence, or an audited operator override.
- Implementing the long-lived AccountOwner daemon. This PRD must work with the current AccountOwner artifacts and process-local limitations.

## 5. Stop, Hung, and Cannot-Submit Taxonomy

The product should model "bot cannot safely submit" rather than only "bot crashed." A bot may be alive, stopped, paused, stale, blocked, frozen, poisoned, or simply waiting for market/session conditions.

Account for these states:

1. **Human stopped or paused the bot.** The recovery path is operator confirmation and resume/start when gates pass.
2. **Frontend/app crashed.** Visibility was lost, but the bot may still be running. Recovery starts by refreshing control-plane and broker evidence.
3. **Python API/control plane stale.** Commands may not reach the bot; actions should be degraded until lease/boot evidence is current.
4. **Live daemon crashed or restarted.** The bot process may be gone or bound to stale boot evidence. Recovery requires daemon health, process state, and broker reconciliation.
5. **Bot process alive but command loop stale.** The bot may be running but not listening to pause/stop/flatten/reconcile commands.
6. **Broker disconnected or degraded.** Broker facts cannot prove safety until connection, paper-only safety, and evidence freshness are restored.
7. **Broker probe stale or missing.** The broker may be connected, but the specific open order/execution/position/account evidence needed for safety is stale.
8. **Market data feed stalled.** The bot may run but should not make new decisions from stale bars or quotes.
9. **Market/session closed or halted.** This may be expected non-trading state, not a failure.
10. **Submit outcome uncertain.** A durable intent exists without a proven terminal broker result; recovery is reconciliation before any new submit.
11. **Reconciliation not available, stale, in progress, or failed.** The bot cannot safely submit until the receipt is fresh and clean/adopted.
12. **Owned orphan order or execution.** Broker proves our namespace but engine state does not fully own it; adoption may be required.
13. **Foreign or unclaimed order/execution/position.** Account is contaminated or not proven. Recovery may require cancel/flatten, adoption, baseline, or audited override.
14. **Duplicate active namespace.** More than one active owner claims the same bot order namespace.
15. **AccountOwner generation not accepting.** Phase is reconnecting, draining, frozen, unknown, or generation proof is missing.
16. **Account freeze.** Watchdog halt, unsafe flatten, restart intensity, or unresolved exposure wrote account-level freeze evidence.
17. **Activity publisher blind or degraded.** This is an observability problem. It may not block submit by itself, but it can block operator trust and recovery visibility.
18. **Risk/account constraints.** Buying power, margin, account restrictions, day-trade warnings, or account severity signals can block safe operation.
19. **Poisoned run.** A run-level unsafe sentinel exists; recovery is redeploy or explicit account-level recovery, not resume.
20. **Stopped durable desired state.** The bot is intentionally retired; restarting requires an explicit deploy/redeploy path.
21. **Control plane lost the process handle.** The bot may still be alive, but the data plane lost in-memory process or publisher registry state after a restart. Recovery is re-adoption or account-level proof, not automatic restart.
22. **Connected-account mismatch.** The broker session is connected to a different account than the bot/account triage target. Broker-sourced gates must be `unknown` or blocked, never pass.
23. **Clock or timestamp skew.** Broker observed time and local service time disagree enough to make freshness and TTL claims untrustworthy.
24. **Partial fill during flatten/cancel recovery.** A broker mutation may have partially succeeded while the UI/action outcome is ambiguous.
25. **Concurrent operators.** Two operators may attempt recovery on the same account. Mutation idempotency and append-only evidence must prevent double actions and stale clear decisions.

### 5.1 Account-Level Reconciliation Spine

Account-level reconciliation is the critical-path capability for this PRD. It answers:

> "Can this account be proven clean without relying on a currently running bot process?"

This is required for crashed/gone-process recovery and for account-freeze recovery proof. Runtime reconciliation is run-scoped and requires a live binding. Account-level reconciliation is account-scoped and can run when the bot process is gone.

Account-level reconciliation is **not** a new numerical or ownership engine. It is a durable, freshness-gated receipt over `account_truth`:

1. Run a broker/account sweep through the existing Account Truth path.
2. Preserve the existing `AccountTruthResponse.final_verdict`, `final_severity`, blockers, caveats, invariants, evidence gaps, owner rows, and source freshness.
3. Add broker-liveness proof, connected-account proof, generated time, expiry/TTL, and receipt identity.
4. Persist the receipt under account artifacts with enough evidence refs to replay the decision.
5. Expose the receipt to account triage, Account Monitor, Orders, Reconciliation, and the clear-freeze mutation.

The account-clean verdict has one home: Account Truth. Account-level reconciliation may reject a stale, account-mismatched, or broker-liveness-unproven Account Truth response, but it must not re-derive cleanliness independently from raw orders, positions, executions, or account values.

The receipt should fail closed when:

- the connected broker account does not match the requested account id;
- required broker liveness evidence is missing or stale;
- Account Truth returns `not_proven`;
- critical Account Truth invariants fail, including open orders, positions, and execution assignment;
- source evidence gaps make the receipt stale or incomplete;
- the receipt TTL has expired.

Run-scoped reconciliation remains authoritative for a specific run's WAL, intent adoption, and run poison state. Account-level reconciliation is the account-clean proof used when no live run can produce a runtime receipt or when an account freeze needs account-wide evidence.

## 6. Solution Overview

Introduce a backend-owned **Account Triage Projection** that acts as a thin compositor over existing authorities.

The projection must not become a parallel gate engine. It re-projects existing `GateResult` rows, each bot's existing `operator_surface`, Account Truth, account-level reconciliation receipts, account freeze artifacts, AccountOwner generation, mutation evidence, and account events. Its net-new value is rollup and routing: affected bots, focused evidence targets, return links, and backend-authored remediation routing.

The projection is the shared contract behind the bidirectional relationship:

- Bot Control uses it to link each blocker to the right broker page and focus target.
- Account Monitor uses it to render account-level gates, freezes, restart intensity, owner generation, exposure, and recovery proof actions.
- Orders uses it to annotate rows with bot blockers, recovery affordances, owner identity, and return links.
- Reconciliation uses it to render account-level receipts, runtime/cold-start receipts, stale/failed/adopted state, orphan adoption, poison reasons, and post-action proof requirements.

The projection must be authored in Python and tested with synthetic account truth, account-level reconciliation receipts, run-level reconciliation receipts, freeze artifacts, operator surface fixtures, and broker evidence. Angular is a renderer and router.

## 7. Account Triage Contract

Add a Python schema and service for an account-scoped recovery projection. The exact names can change during implementation, but the contract should carry these concepts.

### 7.1 Endpoint shapes

Preferred endpoints:

- `GET /api/accounts/{account_id}/triage`
- `GET /api/live-instances/{strategy_instance_id}/account-triage`

The account endpoint is primary. It fans out over durable account/instance evidence and emits one account-scoped projection. The instance endpoint is a bot-scoped filter over the same projection with return links to Bot Control, not a second computation path.

### 7.2 Core response fields

The response should include:

- `schema_version`
- `generated_at_ms`
- `account_id`
- `strategy_instance_id` when scoped to one bot
- `return_route` when invoked from Bot Control
- `summary_headline`
- `summary_detail`
- `overall_gate_result`
- `bot_submit_readiness`
- `account_reconciliation_receipt`
- `gate_rows`
- `affected_bots`
- `blocking_evidence_refs`
- `available_recovery_actions`
- `recent_recovery_events`
- `source_freshness`

All timestamps crossing boundaries remain `int64 ms UTC`.

Triage reads should not synchronously sweep IBKR on every Bot Control poll. The endpoint should reuse the latest account-level reconciliation/account-truth receipt when it is fresh enough, and expose freshness age plus remediation to refresh when it is stale.

### 7.3 Gate row model

Each account-triage row should be backend-authored and shaped like:

- `gate_id`
- `status`: reuse `GateResultStatus` (`pass`, `block`, `poison`, `freeze`, `unknown`, `not_applicable`)
- `scope`: `account`, `bot`, `order`, `execution`, `position`, `mutation`, `reconciliation`, `broker_connection`, `account_owner`, or `activity`
- `severity`: `ok`, `info`, `warning`, `critical`
- `title`
- `detail`
- `operator_next_step`
- `source`
- `evidence_at_ms`
- `affected_strategy_instance_ids`
- `affected_order_refs`
- `affected_perm_ids`
- `affected_exec_ids`
- `affected_con_ids`
- `evidence_refs`
- `primary_remediation`
- `secondary_links`

The row is the product primitive that makes bidirectional navigation possible.

Gate rows must be sourced from existing verdicts or receipts. If a row maps an Account Truth verdict, run reconciliation state, submit-readiness code, or `OperatorGate.status` into `GateResultStatus`, the mapping must be explicit and tested. Do not infer a passing triage row from raw broker facts in the compositor.

### 7.4 Remediation model

Remediation actions must be a closed backend-authored set, such as:

- `open_account_monitor`
- `open_orders`
- `open_reconciliation`
- `open_bot_control`
- `open_activity`
- `open_audit`
- `invoke_reconcile_instance`
- `refresh_account_truth`
- `renew_control_plane_lease`
- `cancel_known_order_by_perm_or_ref`
- `flatten_and_pause_bot`
- `emergency_flatten_account`
- `record_account_recovery_proof`
- `record_audited_override`
- `adopt_owned_orphan`
- `mark_poisoned`
- `redeploy`
- `wait`
- `external_manual_check`

Angular may render these actions, route them, and call the named endpoint, but it must not decide that a raw fact is safe to clear.

## 8. Page Responsibilities

### 8.1 Bot Control

Bot Control remains the command center.

Required changes:

1. Render account-triage links from `trader_guidance`, attention groups, and account-triage rows.
2. Replace shallow broker links with focus links:
   - `/broker/account-monitor?bot=<id>&account=<account_id>&gate=<gate_id>&focus=<focus>&return=/broker/bots/<id>`
   - `/broker/orders?bot=<id>&account=<account_id>&gate=<gate_id>&order_ref=<ref>&return=/broker/bots/<id>`
   - `/broker/reconciliation?bot=<id>&account=<account_id>&gate=<gate_id>&receipt=<id>&return=/broker/bots/<id>`
3. Show whether the block is account-scoped, bot-scoped, order-scoped, or evidence-scoped.
4. After a corrective action succeeds, refresh `LiveInstanceStatus` and account triage before enabling resume/start.
5. Keep primary trader guidance backend-authored.

### 8.2 Account Monitor

Account Monitor becomes the account safety and recovery board.

Required content:

1. Account truth summary and existing owner exposure.
2. Account freeze evidence, if present.
3. Restart-intensity gate, observed starts, threshold, active window, affected bots, and cooldown/recovery guidance.
4. AccountOwner generation and phase.
5. Current risk, positions, buying power, margin, net liquidation, and P&L evidence freshness.
6. Unknown positions and retired-owner exposure blockers.
7. Recovery proof workflow when backend says recovery proof is available.
8. Audited override workflow when backend says override is available.
9. Bot context chip when opened from Bot Control.
10. Return-to-bot link after recovery.

Account Monitor must not let an operator clear a freeze by clicking a frontend-only button. The UI submits a request to a guarded Python endpoint that creates `AccountRecoveryProof` or `AccountAuditedOverride` evidence and calls `clear_account_freeze(...)`.

### 8.3 Orders

Orders becomes the order and execution recovery lens.

Required content:

1. Open and completed orders grouped by durable identities from PR #761: `order_ref`, `perm_id`, and `exec_id`.
2. Row-level owner class: bot, app-minted manual, adopted manual, foreign/unclaimed, retired owner, unknown.
3. "Blocking bot X" indicator when a row contributes to a gate row.
4. For bot-owned rows, link to Bot Control and Activity/Audit.
5. For foreign/unclaimed live orders, fail closed and expose only guarded actions.
6. For known app/manual orders, allow cancel only through recovery-action guards that are account-scope checked, freeze-gated, and keyed by durable broker/account identity.
7. For owned orphan rows, link to Reconciliation adoption workflow rather than treating cancel as the default.
8. Preserve raw broker identifiers exactly in technical details.
9. Show evidence gaps honestly when completed-order or execution sweeps are unavailable.

Orders must keep manual submit secondary to recovery. This page should not become a broad trading ticket.

Cancel recovery must not be keyed only by raw `order_id`. A recovery cancel request should resolve by `perm_id` and/or `order_ref`, verify the connected account, verify ownership/classification, and refuse raw `order_id`-only cancellation during an active account freeze. If the only available identifier is session-local `order_id`, the UI should route to manual inspection or audited override rather than presenting a normal recovery cancel.

### 8.4 Reconciliation

Reconciliation becomes the operational recovery hub.

Required content:

1. Current reconciliation state: `NOT_AVAILABLE`, `IN_PROGRESS`, `CLEAN`, `ADOPTED`, `STALE`, or `FAILED`.
2. Account-level reconciliation receipt: Account Truth verdict, broker-liveness proof, connected-account proof, TTL, source freshness, final gate result, and evidence refs.
3. Run-level receipt freshness, WAL sequence, run id, namespace, broker observed time, failure reason, and adopted intent ids.
4. Account-level vs cold-start vs runtime reconciliation distinction.
5. Owned-orphan adoption detail.
6. Poison reasons: no order ref, unparseable order ref, unknown namespace, foreign perm id, corrupt sidecar/WAL, broker probe failure.
7. Unknown/foreign broker state that blocks account safety.
8. Mutation reconciliation for ambiguous pause/resume/stop/flatten/cancel outcomes.
9. Proof required before resume after uncertain submit or uncertain flatten.
10. Account freeze clear requirements when account-level reconciliation is part of recovery proof.
11. Links back to Orders rows, Account Monitor gates, Activity/Audit, and Bot Control.

The existing "IBKR vs Engine" numeric comparison can remain, but it should become a secondary section or tab. The primary purpose of this page is recovery and proof.

## 9. Recovery Workflows

### 9.1 Human stopped or paused bot

1. Bot Control shows who/when/why if evidence exists.
2. Account triage verifies no account-level blocker.
3. Resume/start remains disabled until backend gates pass.
4. Operator clicks Resume/Start in Bot Control.

### 9.2 Daemon or process crash

1. Bot Control shows host/process/control-plane state.
2. Account triage checks broker connection, account truth, account freeze, and the latest account-level reconciliation receipt.
3. If process is gone but broker has exposure, operator uses Account Monitor, Orders, and Reconciliation to recover account state.
4. Recovery does not require restarting the crashed bot just to obtain a runtime receipt.
5. Resume/start requires a fresh account-level reconciliation receipt, no account freeze, and the usual start/resume gates.

### 9.3 Submit outcome uncertain

1. Bot Control primary remediation invokes runtime reconciliation when live binding exists.
2. Reconciliation page shows the unresolved intent ids and broker facts.
3. If broker confirms an owned orphan, backend adoption evidence is written.
4. If broker contradicts the engine or foreign state appears, run is poisoned or account is frozen per backend rules.
5. Bot Control refreshes and only permits submit when `safe_to_submit` is restored.

### 9.4 Account freeze

1. Account Monitor shows freeze reason, source, recorded time, affected bots, and next step.
2. Operator refreshes account truth and account-level reconciliation.
3. Backend offers one of:
   - recovery proof path when the account-level reconciliation receipt proves account clean and final gate pass;
   - audited override path when explicit human approval is appropriate;
   - no clear action when evidence is insufficient.
4. Clear action records append-only evidence and calls `clear_account_freeze(...)`.
5. Bot Control remains blocked until status refresh sees no active freeze and all other gates pass.

### 9.5 Unknown open order

1. Bot Control links to Orders focused on the unknown row.
2. Orders shows foreign/unclaimed classification and exact broker identifiers.
3. Operator chooses a guarded backend-authored path:
   - cancel by durable `perm_id` and/or `order_ref` if account-scope, freeze, ownership, and idempotency guards pass;
   - adopt manual with audited evidence if this is a known hand action;
   - freeze/poison/reconcile if ownership remains unsafe.
4. Raw `order_id`-only cancel is refused during an active freeze and should not be presented as a normal recovery action.
5. Account-level reconciliation reruns after any corrective action.

### 9.6 Restart intensity freeze

1. Account Monitor shows observed start count, threshold, window, affected bots, and active freeze evidence.
2. Bot Control and Deploy should avoid encouraging repeated starts during the active window.
3. Recovery proof can clear the freeze only after account-level reconciliation proves broker/account state clean.
4. A clean recovery proof starts a new restart-intensity window without deleting durable history.
5. A countdown/wait or audited override may be more appropriate than proof-only clearing when restart intensity is purely behavioral and no exposure is present.

### 9.7 Account-Level Reconciliation

1. The operator or backend requests an account-level reconciliation for an account id.
2. The service verifies that the connected broker account matches the requested account id. Mismatch produces a non-passing receipt.
3. The service runs or reuses a fresh Account Truth sweep.
4. The service records broker-liveness proof, source freshness, TTL, and evidence refs.
5. The service persists a receipt whose clean/not-proven verdict is the Account Truth verdict plus freshness/account-scope/liveness gates.
6. Account triage consumes this receipt for account-clean state.
7. Clear-freeze recovery proof consumes this receipt for `reconciliation_result`.
8. Reconciliation UI surfaces this account receipt alongside run-level receipts.

This workflow is the exit from the crashed/gone-process loop: it proves the account without requiring the crashed bot to restart and produce a runtime reconciliation receipt.

## 10. Backend Implementation Decisions

1. **Thin compositor boundary.** Add an `account_triage` service in Python rather than scattering joins across Angular pages, but keep it thin: it re-projects existing verdicts and receipts rather than deriving new ones from raw facts.
2. **Single source of account-clean truth.** Account-level reconciliation receipts wrap `AccountTruthResponse`; they do not recompute account cleanliness. Account Truth owns clean vs not-proven.
3. **Compose from existing authorities.** Inputs include each bot's `LiveInstanceStatus.operator_surface`, `AccountTruthResponse`, account-level reconciliation receipts, run-level reconciliation receipts, account freeze artifacts, AccountOwner generation, broker activity health, incidents, mutation evidence, and account events.
4. **Account-scope invariant.** If the connected broker account does not match the triage account id, every broker-sourced gate is `unknown` or blocked, never pass.
5. **No frontend verdicts.** Angular cannot derive a gate row from raw account truth or raw orders. If a row needs product meaning, Python emits it.
6. **Reuse `GateResultStatus` with an explicit map.** Account triage should reuse the existing gate status vocabulary unless a separate PR updates the canonical contract and docs. Any translation from Account Truth, `OperatorGate.status`, reconciliation state, or submit-readiness code must be pinned in tests.
7. **Evidence refs must be concrete.** Each blocker should point at specific broker ids, artifact paths, run ids, receipt ids, event ids, or account event rows.
8. **Recovery actions are closed and guarded.** The frontend can render only known action kinds; unmapped actions render as unavailable with diagnostic detail.
9. **Cancel recovery is hardened before UI wiring.** Recovery cancel must be freeze-gated, account-scope checked, ownership checked, idempotent, and keyed by durable `perm_id` and/or `order_ref`. Raw `order_id`-only cancel is not a recovery primitive during freezes.
10. **Mutation idempotency is enforced.** Recovery mutations that can affect broker/account state must have enforced idempotency, not audit-only mutation ids.
11. **Clear-freeze endpoint is guarded.** Add a FastAPI endpoint that validates current account-level reconciliation, broker/account evidence, and final gate state before creating recovery proof or audited override evidence.
12. **Post-action refresh is mandatory.** Mutations return an acknowledgement, not a claim that the account is now clean. The UI must refetch account triage and bot status.
13. **Foreign state fails closed.** Unknown open orders, unknown current positions, duplicate active namespaces, and unassigned executions cannot be cosmetically hidden.
14. **IBKR evidence gaps degrade honestly.** Missing completed-order sweeps, position sweeps, executions, commissions, or liveness evidence cannot produce a clean account claim.
15. **Activity health remains observability unless paired with safety evidence.** Data-plane restarts can wipe in-memory publisher registry while bot processes continue. Triage must not escalate activity-publisher amnesia into a submit block by itself.
16. **Triage reads are freshness-aware.** Do not run expensive broker sweeps every 4-second Bot Control poll. Cache/reuse account-level receipts within TTL and expose stale actions when the receipt expires.

## 11. Frontend Implementation Decisions

1. **Deep links are first-class.** Bot id, account id, gate id, focus id, and return route travel through query params.
2. **Context chip appears on broker pages.** When opened from Bot Control, pages show "Blocking bot <id>" or "Reviewing bot <id>" with return navigation.
3. **Rows link both ways.** Bot-owned account/order/execution rows link back to Bot Control and Activity/Audit.
4. **Actions render from backend remediations.** The UI does not decide that a clear/cancel/adopt/flatten action is available.
5. **Raw identifiers stay exact.** `order_ref`, `perm_id`, `exec_id`, `con_id`, run id, intent id, receipt id, paths, hashes, refs, and URLs are preserved exactly.
6. **Backend codes in primary receipt/evidence UI use `receiptLabel`.** Preserve opaque audit tokens; do not pipe backend-authored trader prose.
7. **No nested recovery cards.** The recovery UI should be dense, operational, and scan-friendly.
8. **Reconciliation primary information architecture changes.** Recovery receipts and gates come first; numeric IBKR-vs-engine comparison moves below or into a secondary tab.
9. **Existing frontend verdict debt is folded back.** Any client-side uncertainty/attention derivation introduced before this PRD, including execution uncertainty codes and account-summary attention, should be migrated into backend-authored projection fields during this work or explicitly tracked as a blocker to the no-frontend-verdict invariant.

## 12. User Stories

1. As an operator, I want Bot Control to tell me which account fact blocks a bot, so that I do not search across three pages.
2. As an operator, I want Bot Control to deep-link to the exact Account Monitor, Orders, or Reconciliation row, so that recovery starts at the evidence.
3. As an operator, I want every broker page opened from Bot Control to show the bot context and return link, so that I do not lose my place.
4. As an operator, I want Account Monitor to show account freeze reason, source, and clear requirements, so that I know why all bots are blocked.
5. As an operator, I want restart intensity shown with threshold, count, window, and affected bots, so that I stop repeatedly starting into a freeze.
6. As an operator, I want Orders rows to say when they block a bot, so that unknown broker orders cannot hide in a ledger.
7. As an operator, I want foreign/unclaimed orders and positions to fail closed, so that bots cannot resume against unexplained exposure.
8. As an operator, I want owned orphan orders routed to adoption/reconciliation, so that I do not double-submit or cancel blindly.
9. As an operator, I want a safe account-freeze clear workflow, so that clean broker evidence can restore operation without manual file edits.
10. As an operator, I want audited override to be explicit, time-bounded, and evidence-backed, so that human exceptions remain traceable.
11. As a trader, I want Reconciliation to explain stale/failed/adopted receipts in plain language, so that I understand what to do next.
12. As a trader, I want raw IBKR identifiers available in technical detail, so that I can match TWS/Gateway and audit artifacts.
13. As an engineer, I want account triage computed in Python, so that the frontend never invents safety meaning.
14. As an engineer, I want fixtures for account freezes, unknown orders, owned orphans, stale receipts, and AccountOwner phases, so that recovery behavior is deterministic in CI.
15. As a reviewer, I want each implementation slice to update `docs/bot-lifecycle-account-owner-authority.md`, so that shipped authority stays synchronized.

## 13. Suggested Implementation Slices

### S0 - Account reconciliation receipt and recovery-action guards

Add the account-level reconciliation receipt and backend recovery-action guards before UI recovery work.

Scope:

- durable account-level reconciliation receipt wrapping Account Truth verdict, broker-liveness proof, connected-account proof, freshness, TTL, and evidence refs;
- account-clean final gate result derived from the Account Truth verdict plus liveness/account-scope/freshness gates;
- recovery cancel guard keyed by `perm_id` and/or `order_ref`, not raw `order_id`;
- freeze-gated cancel refusal during active account freezes unless an explicit backend recovery policy allows it;
- enforced idempotency for broker/account recovery mutations;
- explicit status translation table from Account Truth, `OperatorGate.status`, reconciliation state, and submit-readiness code into `GateResultStatus`.

Tests:

- clean account receipt from clean Account Truth;
- not-proven receipt from unknown open order, unknown position, unassigned execution, evidence gap, stale liveness, expired TTL, or connected-account mismatch;
- account-level receipt can be produced with no live bot binding;
- recovery proof can consume a clean account-level receipt;
- cancel guard refuses active freeze, connected-account mismatch, unknown ownership, raw `order_id`-only recovery cancel, and duplicate mutation id;
- cancel guard permits a known safe cancel by durable `perm_id` and/or `order_ref`.

### S1 - Thin account triage read model

Add Python schemas, service, and read endpoints for account triage. Compose from existing operator surfaces, account truth, account-level reconciliation receipts, freeze artifacts, AccountOwner generation, run-level reconciliation projection, broker activity health, and incidents. Do not re-derive account-clean or bot-safe verdicts.

Tests:

- clean account, running bot, safe to submit;
- account freeze;
- stale reconciliation;
- stale or missing account-level receipt;
- unknown open order;
- owned orphan;
- AccountOwner not accepting;
- missing broker evidence;
- connected-account mismatch;
- status translation table coverage.

### S2 - Bot Control deep links and account-triage rendering

Wire Bot Control to fetch the bot-scoped account triage projection. Render gate rows and focus links to Account Monitor, Orders, and Reconciliation.

Tests:

- blocked gate renders correct route and query params;
- backend-authored remediation renders without frontend-derived verdict;
- status refresh after reconcile/recovery action.

### S3 - Account Monitor recovery board

Add the account safety board, freeze evidence, restart-intensity panel, AccountOwner phase, read-only recovery requirements, and return-to-bot context. Mutation controls depend on S6.

Tests:

- active freeze renders no unsafe clear button;
- recovery proof action is hidden or disabled until S6 backend support exists;
- audited override action is hidden or disabled until S6 backend support exists;
- successful mutation, once S6 exists, triggers refetch rather than optimistic clean state.

### S4 - Orders recovery annotations

Annotate order/execution rows with account-triage blockers, owner context, bot links, guarded cancel/adopt routes, and focus behavior.

Depends on S0 cancel/recovery-action guards.

Tests:

- foreign open order blocks and offers only backend-authored actions;
- bot-owned order links to Bot Control and Activity/Audit;
- owned orphan routes to Reconciliation;
- raw `order_id`-only cancel is not rendered as recovery during a freeze;
- completed-order evidence gaps degrade honestly.

### S5 - Reconciliation recovery hub

Refactor the Reconciliation page around operational receipts: account-level reconciliation receipt, run-level current state, freshness, failure reason, adopted intents, poison reasons, mutation reconciliation, orphan adoption, and proof requirements. Move numeric IBKR-vs-engine comparison to a secondary section or tab.

Tests:

- stale/failed/not-available/adopted states render correct backend copy;
- account-level clean/not-proven/stale/mismatched-account receipt states render correct backend copy;
- owned orphan adoption path requires backend action;
- poison reasons are shown as evidence and not as clearable UI state;
- runtime reconcile action refetches bot status and account triage.

### S6 - Clear-freeze mutation endpoint

Add guarded FastAPI mutations for recovery proof and audited override. They should call the existing account artifact authority rather than rewriting freeze files directly.

Depends on S0 account-level reconciliation receipts and recovery-action guards.

Tests:

- recovery proof clears only with a fresh clean account-level reconciliation receipt and final gate pass;
- recovery proof fails with stale/missing/not-proven account-level reconciliation;
- audited override fails when stale or approved decision is `freeze`;
- audited override requires typed prior evidence refs rather than arbitrary junk;
- account events are appended;
- endpoint returns acknowledgement plus refetch guidance.

### S7 - End-to-end recovery scenarios

Add integration tests and, if practical, Playwright coverage over the most important flows:

- bot blocked by account freeze -> Account Monitor -> proof -> Bot Control;
- bot blocked by unknown open order -> Orders -> guarded action -> account-level reconciliation -> Bot Control;
- bot blocked by stale reconcile -> Reconciliation -> reconcile now -> Bot Control;
- bot blocked by missing/stale account-level receipt -> Reconciliation -> account reconcile -> Bot Control;
- bot blocked by AccountOwner reconnecting -> Account Monitor -> wait/refresh -> Bot Control.

## 14. Testing Strategy

### Backend

- Use pytest over pure projection services first.
- Use fake account truth, fake operator surface, fake artifacts, and fake broker evidence. CI must not require IBKR Gateway.
- Pin all recovery action availability to backend-emitted facts.
- Contract-test timestamps as `int64 ms UTC`.
- Exhaustiveness-test remediation action kinds.
- Assert no recovery proof can be created from stale, missing, mismatched-account, or contradictory account-level receipt evidence.
- Assert account triage does not re-derive Account Truth cleanliness from raw broker facts.
- Assert activity-health registry amnesia after data-plane restart does not become a submit block unless paired with a real safety blocker.

### Frontend

- Use Angular Testing Library and Vitest at existing service seams.
- Assert on rendered user behavior, route links, buttons, disabled reasons, and refetch behavior.
- Verify primary UI renders backend-authored prose and `receiptLabel` for raw code-like receipt values.
- Verify opaque identifiers remain exact.
- Add query-param focus tests for Account Monitor, Orders, and Reconciliation.
- Do not add frontend tests that assert client-side safety calculations.

### Manual/local validation

- Run local dev server after implementation and verify:
  - `/broker/bots/:id`
  - `/broker/account-monitor?bot=...`
  - `/broker/orders?bot=...`
  - `/broker/reconciliation?bot=...`
- Use screenshots or Playwright checks for focused row visibility, return links, and no overlapping recovery controls.
- IBKR live-paper smoke checks belong in runbooks or manual validation, not CI.

## 15. Documentation Updates Required Per Slice

Every implementing PR must update the relevant authority docs when shipped behavior changes:

- `docs/bot-lifecycle-account-owner-authority.md`
- `docs/runbooks/live-trade-reconciliation.md`
- `docs/architecture/ibkr-account-truth-cross-bot-validation-prd.md`, if Account Truth semantics change
- `docs/architecture/operator-notice-prd.md`, if notices/remediation kinds change

If the implementation introduces, retires, or moves math or engine authority paths, also update:

- `docs/math-sources-of-truth.md`
- `docs/architecture/engine-authority-map.md`

## 16. Open Questions for Review

1. Should Reconciliation be renamed in the UI to "Recovery" or "Account Recovery" while keeping the route stable?
2. What exact fields belong in the account-level reconciliation receipt hash/evidence refs?
3. What is the minimum evidence set required for `record_account_recovery_proof` after a freeze, beyond fresh account-level clean/pass?
4. Should audited override be available from Account Monitor only, or also from Orders/Reconciliation for focused foreign facts?
5. Should restart-intensity freezes show a countdown/wait action, or only a proof/override path when account exposure is clean?
6. Should owned-orphan adoption be run-scoped only until the AccountOwner daemon ships, or should the account-triage projection classify against all active registry namespaces now?
7. What is the operator identity source for `requested_by` and `approved_by` in local/dev mode?
8. Which PR #761 frontend-derived attention/uncertainty fields are folded into the backend in this initiative, and which are explicitly deferred?
9. What is the canonical translation table among Account Truth verdict/severity, `OperatorGate.status`, `GateResultStatus`, `ReconciliationState`, and `SubmitReadinessCode`?
10. How stale can an account-level reconciliation receipt be before Bot Control must refuse to treat it as recovery proof?
11. Should activity publisher degradation ever block resume/start, or remain warning-only unless broker/account evidence is missing too?
12. What idempotency key shape should recovery cancel, clear-freeze, and adoption mutations share?

## 17. Review Prompt for a Second Opinion

Use this prompt when asking another model or reviewer to challenge the PRD:

```text
You are reviewing a PRD for tim1016/learn-ai, a scientific trading research platform with strict backend-owned numerical and operational authority. The PRD is docs/architecture/bot-control-account-triage-reconciliation-prd.md.

Context:
- Implementation must happen after PR #761, "[codex] Broker control and account truth polish", is merged.
- PR #761 stabilizes Account Monitor execution history, Orders identity around order_ref/perm_id/exec_id, account-truth-backed order ledger sweeps, and the Bot Control inline workbench.
- The proposed PRD makes /broker/account-monitor, /broker/orders, and /broker/reconciliation into bidirectional recovery lenses for /broker/bots/:id.
- The key design is a Python-owned Account Triage Projection that is a thin compositor over existing authorities.
- Account-level reconciliation is a new durable receipt over the existing Account Truth verdict plus broker-liveness, connected-account proof, freshness, TTL, and evidence refs. It must not re-derive account cleanliness.
- S0 now gates the implementation: account-level reconciliation receipt, status translation table, freeze-gated cancel keyed by perm_id/order_ref, account-scope invariant, and enforced recovery mutation idempotency.
- Angular must render backend-authored verdicts/actions and must not derive account cleanliness, submit safety, ownership, recovery eligibility, or reconciliation pass/fail.

Please give a skeptical architecture/product review. Focus on:
1. Missing stop/hung/cannot-submit states for live-paper bots.
2. Whether the Account Triage Projection is the right boundary, or whether another service/API split would be safer.
3. Whether Account Monitor, Orders, and Reconciliation have the right responsibilities.
4. Whether the recovery workflows are auditable and fail closed.
5. Whether account-level reconciliation as a receipted Account Truth verdict is sufficient for crashed/gone-process and freeze recovery.
6. Whether the clear-freeze recovery proof and audited override design has holes.
7. Whether the implementation slices are independently shippable after PR #761 and S0.
8. Whether any proposed UI behavior risks Angular authoring safety meaning.
9. What tests or docs are missing before implementation starts.

Return:
- P0/P1/P2 concerns with file/section references.
- Open questions that must be answered before coding.
- Any alternative architecture you recommend.
- A short go/no-go recommendation.
```
