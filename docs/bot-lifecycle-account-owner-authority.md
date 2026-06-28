# Bot Lifecycle and Account Ownership - Authority

> **Canonical implementation snapshot** for Bot Cockpit live-paper lifecycle
> gates, account-scoped artifacts, broker submit ownership, and operator
> affordances.
>
> This document describes what ships in code today. It is not a PRD, design
> proposal, or target architecture. When this page disagrees with code, code
> wins and this page must be updated in the same PR.
>
> **Supporting design intent:** `docs/architecture/bot-lifecycle-account-owner-prd.md`
> and `docs/architecture/bot-lifecycle-gate-map.md`. Those docs may describe
> future R3 behavior that is not implemented yet.
>
> **Owner:** the engineer editing `PythonDataService/app/engine/live/*`,
> `PythonDataService/app/routers/live_instances.py`, or
> `PythonDataService/app/services/operator_surface.py`.
>
> **Last reviewed:** 2026-06-28 (account artifact root, account registry,
> unresolved exposure freeze, account classifier V1, AccountOwner submit lane
> foundation, GateResult projection, and restart-intensity freeze).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. Current architecture](#2-current-architecture)
- [3. Shipped account artifacts](#3-shipped-account-artifacts)
- [4. GateResult contract](#4-gateresult-contract)
- [5. Account registry and restart intensity](#5-account-registry-and-restart-intensity)
- [6. Account freeze enforcement](#6-account-freeze-enforcement)
- [7. Account classifier V1](#7-account-classifier-v1)
- [8. AccountOwner submit lane V1](#8-accountowner-submit-lane-v1)
- [9. Operator surface and readiness](#9-operator-surface-and-readiness)
- [10. What does not ship today](#10-what-does-not-ship-today)
- [11. Code cross-reference](#11-code-cross-reference)

---

## 1. Scope and authority

This document answers: **what actually owns live-paper bot lifecycle,
account-level safety artifacts, broker submit gating, and operator-visible
gate rows today?**

In scope:

- Account-scoped artifact files under `artifacts/accounts/<account_id>/`.
- Account registry and restart-intensity freeze behavior.
- Active account-freeze gates for deploy, start, resume, readiness, and submit.
- Account classifier V1 over broker evidence, registry rows, durable intents,
  optional baseline evidence, and optional audited override evidence.
- AccountOwner submit-lane foundation as an in-process component.
- GateResult rows added to readiness, operator surface actions, and Start
  affordance projections.

Out of scope:

- Live-money trading. The live runtime remains paper-only.
- Alpha strategy behavior, backtest math, and indicator correctness.
- Final R3 AccountOwner daemon process, IPC queue, and production single-writer
  broker ownership. Those are still migration targets.

Authority precedence for this domain:

1. Code.
2. This document.
3. `docs/architecture/bot-lifecycle-account-owner-prd.md` and
   `docs/architecture/bot-lifecycle-gate-map.md`.
4. Model memory or stale handoff notes.

---

## 2. Current architecture

Current production remains **R2 multi-runner runtime with account-scoped guard
rails**.

The host daemon still spawns one OS subprocess per `strategy_instance_id`.
Each runner may still construct its own broker client, run cold-start
reconciliation, process bars, and submit through `LivePortfolio`. This is not
the final R3 single account-writer architecture.

What shipped in this slice is the account authority foundation:

```text
Bot Cockpit / FastAPI
  -> host daemon deploy/start
  -> runner process
  -> LiveEngine
  -> LivePortfolio
  -> broker adapter / optional AccountOwner submitter

Account-scoped side channel:
  artifacts/accounts/<account_id>/
    instance_registry.jsonl
    unresolved_exposure.flag
    owner_generation.json
    account_events.jsonl
```

The account side channel is durable and enforcement-backed, but it does not yet
replace the multi-process runner topology.

---

## 3. Shipped account artifacts

All paths below are rooted at the live artifacts parent, then
`accounts/<account_id>/`. Account ids are validated by
`account_artifacts_root(...)`.

| Artifact | File | Authority |
|---|---|---|
| Account freeze | `unresolved_exposure.flag` | `AccountFreezeEvidence`; active while `cleared_at_ms is None`. |
| Account event log | `account_events.jsonl` | Append-only audit stream for freeze, registry, owner, recovery, override, submit, and restart-intensity events. |
| Instance registry | `instance_registry.jsonl` | Append-only `AccountInstanceBinding` rows for account, strategy instance, run id, namespace, lifecycle state, timestamp, and source. |
| Owner generation | `owner_generation.json` | `AccountOwnerGeneration` with generation, phase, timestamp, and source. |

Models live in `PythonDataService/app/engine/live/account_artifacts.py`.
Writes are fsync-backed through the same file-lock helpers used by live state
sidecars.

### Freeze clearing

`clear_account_freeze(...)` accepts exactly one of:

- `AccountRecoveryProof`: broker-backed proof with clean reconciliation and a
  final passing `GateResult`;
- `AccountAuditedOverride`: fresh operator override with approval metadata,
  prior evidence, and next reconciliation step.

Successful clearing leaves the freeze file in place as cleared evidence and
appends an account event. A stale override or an override whose approved
decision is `freeze` cannot clear the account.

---

## 4. GateResult contract

`GateResult` lives in `PythonDataService/app/schemas/live_runs.py`.

```text
gate_id: str
status: pass | block | poison | freeze | unknown | not_applicable
source: str
operator_reason: str
operator_next_step: str | None
evidence_at_ms: int64 ms UTC
```

The status vocabulary is account/lifecycle oriented. Existing readiness gates
still expose legacy `status` values (`pass`, `fail`, `unknown`) for backward
compatibility; `fail` normalizes to `block` when projected into a `GateResult`.

Current producers:

| Producer | Gate ids / area |
|---|---|
| `AccountFreezeEvidence.to_gate_result()` | `account.unresolved_exposure` |
| `evaluate_account_instance_binding(...)` | `account.instance_registry` |
| `evaluate_restart_intensity(...)` | `account.restart_intensity` |
| `AccountClassifierDecision.to_gate_result()` | `account.classifier` |
| `AccountOwner.reconnect_gate_result()` | `account_owner.reconnect` |
| `build_live_readiness(...)` / `build_start_readiness(...)` | Readiness rows with embedded `gate_result` |
| `operator_surface.py` | Start/action/operator readiness projections |

Important limitation: a full account-level gate board is not shipped yet.
GateResult rows are attached to existing surfaces.

---

## 5. Account registry and restart intensity

### Registry binding

`AccountInstanceBinding` records:

- `account_id`
- `strategy_instance_id`
- `run_id`
- `bot_order_namespace`
- `lifecycle_state` (`DEPLOYED`, `ACTIVE`, `RETIRED`)
- `recorded_at_ms`
- `source`

`bot_order_namespace_for_instance(strategy_instance_id)` returns
`learn-ai/<strategy_instance_id>/v1`.

`evaluate_account_instance_binding(...)` folds the registry by latest
`strategy_instance_id`, then verifies that the current account, run id, active
state, and namespace match. Duplicate active namespace ownership blocks with
`ACCOUNT_REGISTRY_DUPLICATE_NAMESPACE`.

Current registry writers:

| Source | State | Where |
|---|---|---|
| Host deploy | `DEPLOYED` | `RunnerProcessManager.deploy(...)` |
| Host start | `ACTIVE` | `RunnerProcessManager.start(...)` |
| Direct runner start | `ACTIVE` | `run.py cmd_start(...)` |

The registry is append-only. It is not yet owned by a long-lived AccountOwner.

### Restart intensity

`evaluate_restart_intensity(...)` reads account events, counts
`account_instance_binding_recorded` events whose `lifecycle_state` is `ACTIVE`,
and freezes the account when the count reaches the policy threshold inside the
active window.

Default `RestartIntensityPolicy`:

| Field | Default |
|---|---|
| `threshold` | `3` |
| `window_ms` | `300000` |
| `scope` | `account` |
| `source` | `account_restart_intensity` |

When breached, the evaluator emits `account.restart_intensity` with
`status=freeze`, appends `account_restart_intensity_breached`, and writes
`unresolved_exposure.flag` if no active freeze already exists.

---

## 6. Account freeze enforcement

An active `unresolved_exposure.flag` blocks:

| Boundary | Enforcement |
|---|---|
| Deploy | `routers/live_instances.py::deploy_instance` checks the broker account before forwarding to the daemon. |
| Start precheck | `routers/live_instances.py::_assert_start_allowed` checks the run's account before forwarding Start. |
| Host start | `RunnerProcessManager.start` writes/evaluates the account registry and rejects if an account freeze exists. |
| Direct runner start | `run.py cmd_start` writes/evaluates the account registry and exits before engine construction if frozen. |
| Resume | `set_instance_desired_state` rejects `resume` while account freeze evidence is active. |
| Submit | `LivePortfolio.submit_pending_orders` checks `account_freeze_provider` before any broker call or AccountOwner handoff. |
| Operator surface | `operator_surface.py` disables Start and Resume affordances and attaches the freeze `GateResult`. |

This is defense in depth. The same artifact is the durable source, but each
boundary still maps it locally to HTTP, CLI, or operator-surface shape.

---

## 7. Account classifier V1

`classify_account(...)` lives in
`PythonDataService/app/engine/live/account_classifier.py`.

Inputs:

- `AccountBrokerEvidence`: broker status plus optional `BrokerSnapshot`.
- `AccountInstanceBinding` registry rows.
- `AccountDurableIntent` rows.
- Optional `AccountBaselineEvidence`.
- Optional `AccountOperatorOverride`.
- `now_ms`.

Outputs:

`AccountClassifierDecision` with `outcome`, `reason`, account id, optional
affected instance/run/namespace, affected order refs, baseline id, override id,
and decision timestamp. `to_gate_result()` projects the decision to
`gate_id=account.classifier`.

Decision map:

| Outcome | Gate status | Meaning |
|---|---|---|
| `continue` | `pass` | Broker evidence matches active registry and durable intent evidence, or no exposure needs action. |
| `ignore_baseline` | `pass` | Completed unknown historical execution is covered by baseline cutoff. |
| `adopt` | `block` | Broker evidence belongs to a registered namespace but lacks durable intent evidence. |
| `retry` | `unknown` | Broker evidence is retryably unavailable. |
| `freeze` | `freeze` | Broker state is unprovable or registry/override evidence is inconsistent. |
| `poison_run` | `poison` | Exposure has no order ref, an unparseable order ref, or an unknown namespace not covered by baseline. |
| `unknown` | `freeze` | Broker state is unknown and cannot silently continue. |

Fresh audited overrides can authorize `continue` for selected unavailable
broker states. Stale, account-mismatched, or contradicted overrides become
freeze decisions.

---

## 8. AccountOwner submit lane V1

`AccountOwner` lives in `PythonDataService/app/engine/live/account_owner.py`.

What ships:

- `AccountOwnerSubmitIntent` typed intake object with trace id, account id,
  strategy instance id, run id, namespace, intent id, order ref, intent kind,
  order spec, owner generation, and `created_at_ms`.
- An in-process `AccountOwner` with an `asyncio.Lock` to serialize submit calls
  inside that instance.
- Intake gates for account mismatch, active freeze, registry mismatch, owner
  generation mismatch, account classifier non-pass result, and order-ref/spec
  mismatch.
- Account event writes for prepared, accepted, rejected, uncertain, reconnect
  phase, and reconnect drain evidence.
- `handle_reconnect(...)` phase transitions through `reconnecting`,
  `draining`, `accepting`, or `frozen`.
- Optional runner wiring through `LiveEngine(account_owner_submitter=...)` and
  `LivePortfolio(account_owner_submitter=...)`.

What does not ship:

- A long-lived AccountOwner daemon process.
- IPC intake from runners to a shared account owner.
- Production single-writer enforcement across all runners.
- AccountOwner-owned reuse of the full `IntentWal` submit state machine for
  uncertain broker acks.

In default production wiring, runners still use the legacy direct-submit path
unless `account_owner_submitter` is provided.

---

## 9. Operator surface and readiness

`build_live_readiness(...)` can include the account instance registry gate in
the engine-authored readiness vector. `build_start_readiness(...)` now adds
embedded GateResult rows to backend-derived start readiness.

`operator_surface.py` now projects:

- Start capability `gate_results`, including account freeze.
- Action capability `gate_results`, including account-freeze blocking for
  Resume.
- Readiness gates as `OperatorGate` rows with canonical `gate_result`.

The cockpit should render these server-authored rows. It should not infer gate
meaning from enum names or compose remediation text on its own.

---

## 10. What does not ship today

- R3 AccountOwner as a durable account-scoped daemon.
- IPC intent queue from runner processes to AccountOwner.
- One broker session owned by AccountOwner for a shared account.
- Account-level gate board as a standalone backend surface.
- Account baseline file model beyond the classifier's optional
  `AccountBaselineEvidence` input.
- Automatic watchdog failure-to-flatten promotion into the account freeze
  artifact. Watchdog evidence emits GateResult rows, but account freeze is a
  separate artifact.
- Retirement compaction or canonical deduplication of account registry rows.

---

## 11. Code cross-reference

| Concern | File |
|---|---|
| Account artifacts, freeze, registry, restart intensity | `PythonDataService/app/engine/live/account_artifacts.py` |
| Account classifier V1 | `PythonDataService/app/engine/live/account_classifier.py` |
| AccountOwner submit lane V1 | `PythonDataService/app/engine/live/account_owner.py` |
| GateResult schema and operator DTOs | `PythonDataService/app/schemas/live_runs.py` |
| Host daemon deploy/start registry writes | `PythonDataService/app/engine/live/host_daemon.py` |
| Runner direct-start registry writes | `PythonDataService/app/engine/live/run.py` |
| Live engine registry readiness gate wiring | `PythonDataService/app/engine/live/live_engine.py` |
| Portfolio submit freeze/registry/AccountOwner hooks | `PythonDataService/app/engine/live/live_portfolio.py` |
| Readiness GateResult embedding | `PythonDataService/app/engine/live/readiness.py` |
| Public deploy/start/resume account-freeze checks | `PythonDataService/app/routers/live_instances.py` |
| Operator surface GateResult projections | `PythonDataService/app/services/operator_surface.py` |
| Existing submit state machine | `PythonDataService/app/engine/live/submit_state_machine.py` |
| Supporting design intent | `docs/architecture/bot-lifecycle-account-owner-prd.md` |
| Gate map design context | `docs/architecture/bot-lifecycle-gate-map.md` |
