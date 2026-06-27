# PRD: Robust Bot Lifecycle, Account Ownership, and Gate Board

**Status:** Draft for review
**Created:** 2026-06-27
**Owner:** Inkant
**Primary objective:** Stop multi-bot lifecycle cascades by making broker-account ownership explicit, account-scoped, and visualizable.
**Inputs:** `docs/architecture/bot-lifecycle-gate-map.md`, `/Users/inkant/.codex/attachments/d5d3b0b8-5087-4c49-9f1f-822b05a7ac02/pasted-text.txt`

## Problem Statement

Bot Cockpit has many useful gates, but they are spread across start routing, runtime pre-flight, readiness, reconciliation, operator actions, broker order safety, and watchdog shutdown. That makes the system hard to reason about and leaves gaps where one bot's failed lifecycle can contaminate the shared IBKR account and trigger more failures.

The root cause is that a broker account is a shared mutable resource. IBKR does not enforce our per-bot ownership model, and it will not reject a stale process's `placeOrder` just because our control-plane lease changed. Today, the host daemon runs one OS subprocess per `strategy_instance_id`; each child has its own in-process submit lock. That is still multiple writers to one account, not a true single-writer design.

The most urgent live failure is watchdog shutdown ordering. Current lease-loss shutdown can proceed to disconnect/exit after an unproven flatten attempt. If flatten/cancel cannot be proven while the broker is still connected, the account can retain exposure while the run exits or poisons itself. That is the JUN26TSLA class and must be fixed before larger account-registry work.

## Current Facts

| Fact | Evidence | Consequence |
|---|---|---|
| Live runners are separate OS processes | `RunnerProcessManager` owns one subprocess per strategy instance and spawns `python -m app.engine.live.run start`. | A per-runner lock does not serialize account-level writes. |
| Submit lock is in-process | `LiveEngine._submit_lock = asyncio.Lock()` serializes submits inside one runner only. | Cross-process stale-writer windows remain. |
| Low-level IBKR call has one code site | `place_paper_order` calls `client.ib.placeOrder(...)`. | Good choke point, but not sufficient for R3 while multiple processes can call it. |
| Submit uncertainty handling mostly exists | `LivePortfolio.submit_pending_orders` records `PENDING_INTENT`, `ACK_FAILED_UNCERTAIN`, probes, adopts/retries/halts. | PROC.RESOLVE_SUBMIT is mostly built; terminal outcome must become account-scoped. |
| Start bypasses shared action evaluator | Start uses `_assert_start_allowed`; Resume/Pause/Stop use `operator_capability.py`. | Account freeze/preflight must be wired into Start explicitly. |
| Gate board does not exist | Gate map is documentation only. | Operator cannot see one authoritative table of gate status. |

## Decision

For any shared broker account, adopt **R3: AccountOwner single broker-writer authority**.

R3 means one account-scoped process/service owns the IBKR session and is the only component allowed to call broker `placeOrder`. Strategy runner processes emit durable intents to AccountOwner. AccountOwner serializes, reconciles, resolves uncertain submissions, and owns account-level freeze/baseline/recovery state.

R1, one broker sub-account per bot, remains a valid future simplification if IBKR paper/FA sub-accounts become available and operationally acceptable. The current code refuses multiple managed accounts, so this PRD targets the current shared-account reality.

R2, many independent runner writers plus per-instance reconciliation, is rejected as the long-term design.

## Goals

1. Make all lifecycle gates visible in one operator-readable gate board.
2. Ensure each visible gate row is produced by the same predicate used for enforcement.
3. Fix watchdog lease-loss ordering before any larger architecture migration.
4. Add account-scoped safety artifacts: append-only registry, baseline, unresolved exposure freeze.
5. Move ownership classification to account scope.
6. Introduce AccountOwner as the only writer to a shared broker account.
7. Prevent death-restart cascades with restart intensity and explicit account recovery workflows.

## Non-Goals

- Live-money enablement.
- Alpha strategy changes.
- Frontend visual redesign beyond rendering the gate board.
- Full FA/sub-account provisioning.
- Replacing existing mathematical/backtest engines.

## Gate Board Contract

The gate board is a backend-authored table. Angular renders it; Angular does not infer gate meaning.

| Field | Meaning |
|---|---|
| `gate_id` | Stable id such as `account.unresolved_exposure`, `start.poison_sentinel`, `submit.paper_safety`. |
| `scope` | `process`, `account`, `instance`, `run`, or `order`. |
| `phase` | `deploy`, `start`, `preflight`, `reconcile`, `activate`, `submit`, `action`, or `recovery`. |
| `status` | `pass`, `block`, `poison`, `freeze`, `unknown`, or `not_applicable`. |
| `source_of_truth` | Artifact, service, or broker observation that authored the result. |
| `enforcement_point` | Function/service that uses this same predicate to enforce. |
| `blocks` | Action or transition blocked by this gate. |
| `operator_next_step` | Reconnect, reconcile, flatten, redeploy, acknowledge baseline, override, etc. |
| `evidence` | Structured facts needed for debugging, with raw codes kept technical. |

Hard rule: every gate row must come from the enforcement predicate or a value object returned by it. Parallel descriptive projections are forbidden.

## Account Artifacts

| Artifact | Scope | Semantics |
|---|---|---|
| `instance_registry.jsonl` | Account | Append-only, write-ahead registry of every `strategy_instance_id`, namespace, lifecycle, and first/last run bound to this account. Written before first submit. Never reconstructed solely from run dirs. |
| `account_baseline.json` | Account | Explicit fleet reset: operator verified flat/no open orders, timestamp, listed instance ids, and residue class allowed. Never authorizes ignoring open orders. |
| `unresolved_exposure.flag` | Account | Freezes deploy/start/submit for the account when exposure or submit state is not provable. |
| `operator_override.jsonl` | Account | Audited override when broker is unreachable but operator manually confirms flat in TWS/Client Portal and acknowledges risk. |
| `account_events.jsonl` | Account | Restart intensity, AccountOwner lifecycle, freeze/unfreeze, reconnect drain, baseline, and override events. |

## Lifecycle Gates

| Gate | Required behavior |
|---|---|
| `GATE.PREFLIGHT` | Account-scoped, pre-spawn. Blocks deploy/start when unresolved exposure exists, account state is unowned/unprovable, restart intensity is exceeded, or baseline acknowledgement is required. |
| `GATE.RECONCILE` | Account-scoped under AccountOwner. Reconciles broker positions/orders/executions against the union of registry-known live/dead instance intents and lifecycle states. |
| `GATE.ACTIVATE` | Instance may enter ACTIVE only when desired state is RUNNING, no poison/freeze applies, reconcile is fresh, broker is connected, runtime config is complete, and AccountOwner accepts the instance. |
| `GATE.RESUME` | Resume uses the shared gate predicates and must recheck account freeze and reconcile freshness before ACTIVE. |
| `GATE.DEACTIVATE` | Pause/Stop remain durable safe writes. Flatten-and-pause writes PAUSED before flatten. |
| `GATE.POISON` | Run-level sink: this run is unsafe; account may remain usable if account state is proven safe. |
| `GATE.UNRESOLVED_EXPOSURE` | Account-level sink: no new deploy/start/submit until emergency flatten plus clean reconcile, or audited operator override. |

## Account-Scoped Classification

The classifier must answer against the whole account, not one bot's narrow namespace.

| Broker artifact class | Outcome |
|---|---|
| Owned by active/current instance and matches projection | Continue. |
| Owned by registry-known instance with lifecycle that expects it | Continue or adopt under that instance. |
| Owned by registry-known instance that is poisoned/dead and has unresolved exposure | Account freeze or recovery workflow, not ignore. |
| Completed unowned execution before explicit baseline, account flat, no open orders, instance listed | Ignore per baseline. |
| Unowned open order | Freeze/poison path; never baselineable. |
| Unowned live position | Freeze until flattened/reconciled or audited override. |
| Probe failed / unparseable / foreign broker id | Freeze or poison depending on whether account safety can be proven. |

## Watchdog Fix

This ships first because it addresses the confirmed live bug class.

Required order on lease loss:

1. Block new submits immediately.
2. Persist durable PAUSED.
3. While broker is still connected, attempt cancel/flatten.
4. Prove account/namespace result through broker lookup: positions flat and no relevant open orders.
5. If proof succeeds, persist safe incident outcome and allow run-level poison/exit as appropriate.
6. If proof is not available, persist unresolved incident now; when account artifacts exist, set `unresolved_exposure.flag`.
7. Only after proof or durable unresolved/quarantine evidence exists, disconnect broker and request engine exit.

Acceptance: a lease-loss fixture where flatten times out or broker proof is unavailable must not end as silent clean/poison with unproven exposure.

## AccountOwner

AccountOwner is the R3 broker-writer for one IBKR account.

Responsibilities:

- Own the IBKR session for the account.
- Accept durable submit intents from runner processes.
- Stamp or verify `order_ref` attribution.
- Serialize broker writes in one process.
- Run submit resolution on ambiguous outcomes.
- Own account-scoped reconciliation and freeze/baseline/override state.
- Drain reconnect replay before new submits.
- Publish gate board rows for account-level gates.

Runner processes may continue to run strategies and emit decisions, but they must not call `placeOrder` directly in shared-account mode.

## Post-Reconnect Ordering

After broker reconnect:

1. Stop accepting new submit intents.
2. Validate AccountOwner ownership/generation.
3. Drain broker-activity replay.
4. Reconcile account state against registry/intent union.
5. Publish gate board update.
6. Re-enable submit intents only if account gates pass.

This composes the existing reconnect-recovery halt with account reconciliation instead of letting independent brakes race each other.

## Recovery and Override

Automated clear path for `UNRESOLVED_EXPOSURE`:

1. Emergency flatten/cancel.
2. Broker probe succeeds.
3. Account classifier returns clean or baseline-authorized residue only.
4. Append unfreeze event.

Audited operator override path:

1. Broker remains unreachable or unprovable.
2. Operator confirms flat/no open orders in TWS or Client Portal.
3. Operator enters acknowledgement with account id, timestamp, reason, and evidence note.
4. System appends `operator_override.jsonl`.
5. Account may unfreeze, but next successful broker reconnect must run account reconciliation before new submit.

## User Stories

1. As an operator, I want a gate board that shows every deploy/start/resume/submit blocker, so I know exactly why the bot can or cannot trade.
2. As an operator, I want account freezes shown separately from run poisons, so I know whether redeploy is safe.
3. As an operator, I want a failed watchdog flatten to preserve broker proof or unresolved evidence before disconnect, so I do not lose the chance to know whether exposure remains.
4. As an engineer, I want gate rows emitted by enforcement predicates, so UI and backend behavior cannot drift.
5. As an engineer, I want registry writes before first submit, so broker residue is never falsely unowned because a run dir was missing or archived.
6. As an engineer, I want AccountOwner to be the only broker writer for a shared account, so stale runner processes cannot submit directly.
7. As an operator, I want an audited override for frozen accounts when IBKR is unreachable but I have manually verified flat state.

## Implementation Plan

| Slice | Work | Acceptance |
|---|---|---|
| P0 design lock | Document that current runtime is multi-process R2 and this PRD commits to R3 AccountOwner for shared accounts. | Gate map and PRD agree; no cross-process-lock-only plan remains. |
| P1 watchdog proof-before-disconnect | Reorder lease-loss shutdown and persist unresolved evidence before broker disconnect. | JUN26TSLA-style timeout/proof-fail fixture leaves unresolved incident/freeze-ready evidence. |
| P2 single-source gate board | Create gate result value objects from enforcement predicates and expose a backend gate board. | Start/action/readiness rows match actual enforcement in tests. |
| P3 account artifacts | Add append-only registry, baseline, unresolved exposure flag, and override log. | Start/deploy refuse when freeze exists; registry is written before first submit intent. |
| P4 account classifier | Refactor classification around account-level registry + union of intents. | Sibling-known live state is accounted; dead/poisoned sibling residue is not blindly ignored; unowned open order freezes. |
| P5 AccountOwner MVP | Add account-scoped writer process/service and route runner submit intents through it. | Runner process cannot call IBKR `placeOrder`; AccountOwner handles submit/reconcile/reconnect ordering. |
| P6 fleet governance | Add restart intensity, explicit fleet baseline workflow, and post-reconnect gate choreography. | Repeated failures freeze redeploy; baseline and reconnect drain are visible on gate board. |

## Tests

| Test | Assert |
|---|---|
| `test_current_runner_process_model_documented` | Host daemon starts one subprocess per instance; submit lock remains per-process until AccountOwner migration. |
| `test_watchdog_disconnect_after_proof_or_unresolved` | Disconnect is not called before proof or durable unresolved incident. |
| `test_gate_board_uses_enforcement_result` | A gate displayed as pass cannot block the corresponding mutation in the same fixture. |
| `test_registry_write_ahead_before_submit` | Submit intent is refused if registry entry is missing. |
| `test_unresolved_exposure_blocks_start` | Start and deploy are refused while `unresolved_exposure.flag` exists. |
| `test_operator_override_unfreezes_with_audit` | Override requires account id, reason, timestamp, and evidence note; next reconnect forces reconcile. |
| `test_account_classifier_dead_sibling_residue_freezes` | Registry-known but dead/poisoned residue is not ignored. |
| `test_account_owner_only_writer` | In shared-account mode, runner paths cannot reach `place_paper_order`; AccountOwner is the only broker writer. |
| `test_post_reconnect_ordering` | New submit is blocked until replay drain and account reconcile complete. |
| `test_restart_intensity_freeze` | More than configured failures inside window freezes auto redeploy. |

## Open Questions

1. AccountOwner process shape: host daemon child, separate FastAPI service, or daemon-owned thread/process?
2. Intent transport: filesystem queue under account artifacts, local HTTP, or another durable IPC?
3. Account artifact root and migration path for existing run dirs.
4. Restart intensity defaults: keep suggested `3 / 15 min` or tune for paper iteration speed?
5. Operator override evidence requirements: free-text note only, screenshot path, or structured checklist?

