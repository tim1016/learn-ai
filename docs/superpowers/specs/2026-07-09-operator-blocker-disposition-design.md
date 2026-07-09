# Operator-blocker disposition contract — deploy + bot-control guidance redesign

**Date:** 2026-07-09
**Status:** Design approved; awaiting spec review → implementation plan
**Surfaces:** `Frontend/src/app/components/broker/broker-deploy-form/`, `Frontend/src/app/components/broker/bot-control/`, `PythonDataService/app/services/operator_surface.py`
**Related:** ADR-0013 (operator-surface judgment vs evidence), ADR-0025 (single-dominant headline), ADR-0026 (daily bot lifecycle), PRD #974, PRD #616/#617/#619-A (operator surface), `docs/runbooks/broker-instance-operator-surface.md`

---

## 1. Problem

Deploying bots and getting them to a running state does not guide the operator honestly. Three concrete failures (the operator's own ranking):

1. **Blocks that don't block.** The deploy form lets you deploy — and start — into a broker that is `hard_down`, an account that is frozen, or a launcher daemon that is down. Broker connectivity is shown only as an amber "fact" in `deploy-readiness.ts`; it does not block. The bot is created, then silently fails to trade.
2. **Scattered recovery.** When a deployed bot won't run, the fix often lives on another page (engine control, account monitor) or in a support ticket, and several conditions dead-end with no in-UI action at all (`prove_evidence`, `resolve_exposure`).
3. **No honest dead-end.** When nothing can recover a bot (poisoned run, retired instance), the UI keeps implying hope instead of plainly saying "this can't be recovered — Replace or Remove is your only move."

The operator did **not** rank "silent not-running" (the verdict card + why-drawer already explain *why*), so this design keeps that machinery and does not redesign it.

### Root cause

There is no single authoritative answer to *"what is blocking this bot, and what is my move"* that both screens consume. Today:

- The deploy surface re-derives readiness in TypeScript (`deploy-readiness.ts`, ~628 lines) with its own `DeployBlocker` shape, blind to the operator surface.
- The control surface re-derives verdicts in TypeScript (`verdict-card-model.ts`, 223 lines) from the operator surface.
- The backend emits raw reason-codes both sides reinterpret independently.

That drift **is** why broker-down blocks one path but not another, and why cures dead-end. The fix is to name the missing concept and make it the single source of truth.

## 2. Key insight — the disposition is already implicit

The four dispositions this design formalizes already exist, un-unified, in the backend:

- `operator_surface.py:738` `_GATE_ACTION_TABLE` already sorts every gate into either an action (`_INVOKE_RESUME`, `_REDEPLOY`, `reconcile_instance`) or an "unavailable reason" that is literally a disposition: `WAIT_FOR_CONDITION`, `REQUIRES_OUT_OF_BAND_RESOLUTION`, `NO_INLINE_REMEDIATION`.
- `HostProcessStartCapability` already carries closed reason codes (`STOPPED_REQUIRES_REDEPLOY`, `ACCOUNT_FROZEN`, `HOST_SERVICE_OFFLINE`, `CRASH_RECOVERY_REQUIRED`, …).

What is missing: a **closed disposition enum**, a **single blocker atom** carrying it, a **`terminal` disposition**, and a **deploy surface that consumes the same list** instead of re-deriving.

This design therefore **extends** the existing operator-surface projection (ADR-0013's backend-authored-semantics model); it is not a rewrite of it.

## 3. The contract (approved keystone)

A single backend-authored value type is the atom both screens render.

```
disposition ∈ { fix_here | fix_elsewhere | wait | terminal }   # closed enum
```

| disposition | meaning | rendered as |
|---|---|---|
| `fix_here` | an in-UI action clears it | filled button **on the bot's own screen** |
| `fix_elsewhere` | real fix, but lives off-screen | deep link + one plain instruction |
| `wait` | transient / self-heals | muted "waiting for X", **no button** |
| `terminal` | nothing recovers *this* bot | loud dead-end card: **Replace / Remove** only |

```python
class OperatorMove(BaseModel):           # backend-authored
    label: str                           # trader prose, e.g. "Connect the broker"
    action: OperatorAction               # canonical SUPERSET union (see below)
    target: str | None                   # route + fragment / runbook slug / endpoint id

class OperatorBlocker(BaseModel):        # backend-authored, closed
    id: str                              # e.g. "broker_disconnected", "account_frozen"
    severity: Literal["blocking", "warning"]
    disposition: Disposition
    headline: str                        # trader prose, backend-authored (never TS-composed)
    detail: str | None
    primary_move: OperatorMove | None    # None only for `wait` and terminal-remove-only cases
    secondary_moves: list[OperatorMove]  # e.g. terminal offers both Replace and Remove
    applies_to: Literal["deploy", "run", "both"]
```

**`OperatorAction` is a canonical superset, not a rename of an existing union.** Today the two action unions in `schemas/live_runs.py` differ: `GateSuggestedAction` (line 1703) is `InvokeCapabilityAction | FocusAction | RedeployAction | OpenRunbookAction` (**no** `invoke_endpoint`), while `TraderPrimaryRemediation` **includes** `InvokeEndpointAction`. We define `OperatorAction` as the superset — the existing members plus `InvokeEndpointAction` and the new `RetireReplaceAction` / `RemoveAction` / `ConfirmInFormAction` — and keep `GateSuggestedAction` / `TraderPrimaryRemediation` as narrower subsets where those seams already constrain the choices. No parallel taxonomy; one superset, existing subsets preserved.

**severity ≠ disposition** (orthogonal). A `wait` blocker is still `blocking` at deploy time — you genuinely cannot run yet — it just renders muted with no fake button.

### One author, two consumers

- New `author_operator_blockers(...)` in the operator-surface layer consolidates today's scattered sources — `HostProcessStartCapability` reason codes, the `_GATE_ACTION_TABLE` rows, account/freeze/fleet conditions — into `OperatorSurface.blockers: list[OperatorBlocker]`.
- The **same** author backs a new deploy-preflight response, so the deploy screen stops re-deriving and renders the identical list.

### Invariants (mapped to the three pains)

1. *Blocks that don't block* → **deploy is refused iff `blockers` contains any `severity == "blocking"`.** Broker-down becomes one blocker row; it now blocks by construction on both surfaces.
2. *Scattered recovery* → every `fix_here` blocker renders its button on the bot's screen; `fix_elsewhere` renders the link. **No blocker can exist without a rendered move.**
3. *No honest dead-end* → `terminal` is first-class with a mandatory dead-end rendering; a bot cannot be "stuck with a hopeful button."

**Keystone test:** a blocker with no valid `(disposition, move)` pairing fails the backend test suite. This is what structurally prevents a future `prove_evidence`-style dead-end.

## 4. Deploy surface

**Backend — new preflight, same author.** `GET …/deploy-preflight?strategy_key&account_id&instance_id` runs `author_operator_blockers(...)` over the *environment/precondition* gates (daemon reachable, broker connected, account proven/unfrozen, fleet clean, strategy validated, instance-not-already-running) and returns:

```
{ ready: bool, blockers: OperatorBlocker[] }        # ready === blockers.every(b => b.severity !== "blocking")
```

**Frontend — `broker-deploy-form` stops deriving, starts rendering.** Clean split of responsibility:

- **Form completeness** (missing fields, sizing preset, action-plan legs) stays client-side — it is about *the form*.
- **Preconditions** move entirely to backend blockers. `deployBlocker()` and the hand-rolled `DeployReadinessFact[]` (~half of `deploy-readiness.ts`) are deleted.

**One button.** `[Deploy & run]`, disabled unless `formComplete() && preflight.ready`. The deploy-only / deploy-without-start path is **removed** (per the approved "one action, hard-blocked" decision). When disabled:

- The button caption names the single top blocker: *"Can't deploy — broker disconnected."*
- Directly under it, that blocker's rendered move: `fix_elsewhere` → "Connect the broker →" (the missing link today); `fix_here` → the actual button.
- The old amber "facts" strip becomes a compact list of the remaining blockers grouped by disposition — the operator sees *everything* between them and a running bot, each with its move.

Identity/exposure coherence confirmations survive as `fix_here` blockers whose move is "confirm & deploy" (in-form), so nothing regresses.

## 5. Control surface

The just-built verdict card (`2026-07-08-bot-control-verdict-card-design.md`) is kept. Three changes wire it to the contract:

1. **The verb comes from the blocker, not from TS.** `verdict-card-model.ts` stops re-deriving the primary move; it renders `blockers[0].primary_move`. `fix_here` → filled button on the card (Reconcile / Clear freeze / Renew lease / Record recovery evidence). `fix_elsewhere` → link + one sentence. `wait` → muted "Waiting for market open" with **no** button.
2. **Honest terminal card.** When `blockers[0].disposition === "terminal"`, the card switches to a distinct dead-end treatment: a plain state word ("Can't recover"), the backend sentence explaining *why* it is unrecoverable, and **only** the terminal moves — **Replace** (retire + fresh deploy, lineage kept) and **Remove** (soft-delete). No hopeful lifecycle button is shown.
3. **Every scattered action comes home.** `why-drawer` stays evidence-only but now lists all blockers grouped by disposition. The two current dead-ends (`prove_evidence`, `resolve_exposure`) get real dispositions in the catalog — never "a button that does nothing."

**Consistency guarantee:** because both screens read `OperatorBlocker[]` from the same author, "broker disconnected" reads and acts identically whether you are deploying or staring at a stuck bot. Drift is structurally impossible.

## 6. Scenario catalog

Every failure mode found in the code, pinned to disposition + move. **★ = broken/missing today.**

Slice 1 deploy-preflight consumes the data-plane `IbkrClient` connection state
only (`connected|soft_lost|subscriptions_stale|degraded_data_farm|disconnected`;
`None` means the broker snapshot is unavailable). Broader cockpit states such as
`hard_down`, `disabled`, `reconnecting`, and `recovering` are authored by the
broker-health monitor overlay and remain catalog concepts for bot-control/run
surfaces, not deploy-preflight inputs.

### `fix_elsewhere` — real fix, off this screen (link + one sentence)

| id | applies | sev | primary_move | note |
|---|---|---|---|---|
| `daemon_down` (`HOST_SERVICE_OFFLINE`) | both | blocking | "Start the engine on this machine →" | ★ deploy shows no link today |
| `broker_disconnected` (`hard_down`/`disconnected`/`disabled`) | both | blocking | "Connect the broker →" | ★ doesn't block deploy today |
| `strategy_not_validated` | deploy | blocking | "Open Strategy Validation →" | already links |
| `fleet_contaminated` (`policy_blocks_starts`) | both | blocking | "Clear fleet state →" (account monitor) | ★ no link today (`REQUIRES_OUT_OF_BAND_RESOLUTION`) |
| `instance_already_running` (`ALREADY_RUNNING`) | deploy | blocking | "Go to the running bot →" | |
| `daemon_incompatible_contract` | run | blocking | "Rebuild & redeploy the daemon →" (runbook) | infra |
| `orphaned_socket` / `registry_amnesia` | run | blocking | "Restart the launcher →" (runbook) | ★ no clear guidance today |
| `account_exposure_unresolved` (`resolve_exposure`) | both | blocking | "Flatten exposure (Account Monitor) →" | routed, **not** a ticket — Account Monitor already has flatten + audited override (`flattenExposureFromDialog` → `emergencyFlattenAccount`, `broker-account-monitor.component.ts:459`) |
| `broker_evidence_unproven` (`prove_evidence`) | run | blocking | "Refresh broker evidence (Account Monitor) →" | ★ demoted from `fix_here` — **no one-call endpoint exists** (`InvokeEndpointAction.endpoint` is `reconcile_instance` only). Honest link, not a fake button. Do **not** reuse the crash-recovery override — that is `CRASH_RECOVERY_REQUIRED`-specific, not generic broker-evidence proof |

### `fix_here` — in-UI button, on the bot's own screen

| id | applies | sev | primary_move | note |
|---|---|---|---|---|
| `account_frozen_clearable` (`ACCOUNT_FROZEN`, flat) | both | blocking | [Clear freeze] | already wired |
| `account_not_proven` / `evidence_stale` (`reconcile_now`) | both | blocking | [Run account reconcile] | the devv2 case |
| `crash_recovery_required` (`CRASH_RECOVERY_REQUIRED`) | run | blocking | [Record recovery evidence] | attestation dialog |
| `halt_active` (`halt_clear`) | run | blocking | [Resume] | |
| `positions_inconsistent` (`positions_self_consistent`) | run | blocking | [Flatten & pause] | |
| `identity_coherence_unconfirmed` | deploy | blocking | [Confirm identity & deploy] | in-form |
| `exposure_coherence_unconfirmed` | deploy | blocking | [Confirm exposure & deploy] | in-form |
| `waiting_for_host` (`WAITING_FOR_HOST`/`IDLE`) | run | warning | [Start] | roll-call / start offer |

### `wait` — transient, muted, no button (button stays disabled but honest)

| id | applies | sev | shows |
|---|---|---|---|
| `broker_reconnecting` (`reconnecting`/`recovering`/`soft_lost`) | both | blocking | "Waiting for broker to reconnect…" |
| `broker_data_farm_degraded` (`degraded_data_farm`) | both | **blocking** | "Waiting for IBKR data farm to recover — don't submit until healthy" — backend already marks this `severity="critical"` / "do not submit until healthy" (`operator_broker_projection.py:110`) |
| `broker_subscriptions_stale` (`subscriptions_stale`) | both | blocking (deploy) / warning (run) | "Resubscribe required — waiting for streams" — backend `severity="warning"`; on the single-button deploy path we treat resubscribe-required as `wait`-blocking (don't start into it) |
| `session_closed` / `calendar` / `warmup` / `instrument_surface` (`WAIT_FOR_CONDITION`) | run | blocking | "Waiting for market open / warmup…" |
| `daily_cap_reached` (`daily_order_cap`) | run | warning | "Order cap hit — resets next session" |
| `roll_call_pending` | run | warning | "Run roll call for a fresh start offer" |

### `terminal` — nothing recovers *this* bot; Replace / Remove only ★ *(disposition doesn't exist today)*

| id | applies | sev | moves | note |
|---|---|---|---|---|
| `run_poisoned` (`STOPPED_REQUIRES_REDEPLOY`, `poison_sentinel`) | run | blocking | [Replace] (retire + fresh deploy) · [Remove] | redeploy is the only path |
| `retired` (phase `RETIRED`) | run | blocking | [Remove] (soft-delete) · [Replace] | read-only today; now honest |
| `exited_unrecoverable` (redeploy-required exits) | run | blocking | [Replace] · [Remove] | |

**Terminal moves map to existing endpoints** (no new mutations): **Replace** → `POST /api/live-instances/{id}/retire-and-replace` (`live_instances.py:2758`; requires `confirm_account_flat` attestation, else 409 `ACCOUNT_FLAT_ATTESTATION_REQUIRED`). **Remove** → `DELETE /api/live-instances/{id}` (soft-delete via `deleteBot`, `live-runs.service.ts:238`) — **not** `POST …/delete`.

**Not terminal:** `spec_signature` / `indicator_state_hydration` are redeploy-fixable but not dead → `fix_here`-via-Replace (Replace offered, bot not declared unrecoverable). `crash_recovery_required` is `fix_here` (attestation), not terminal.

## 7. Slicing (each slice = one PR, each closes a pain)

### Slice 1 — Deploy preflight + hard block → kills *blocks that don't block*

- **Backend:** `Disposition` enum; `OperatorBlocker` / `OperatorMove` / `OperatorAction` types; `author_operator_blockers()` for the deploy preconditions; `GET …/deploy-preflight`.
- **Backend tests:** every blocker has a valid `(disposition, move)` pairing; broker-down / daemon-down / frozen-account each produce a `blocking` blocker; `ready` is false iff any blocking blocker.
- **Frontend:** deploy form renders `blockers`; single `[Deploy & run]` disabled unless `formComplete && ready`; delete `deployBlocker()` + hand-rolled facts; add the missing engine/broker links; remove the deploy-only path.
- **DoD:** cannot deploy into a down broker / down daemon / frozen account; each shows its move.

### Slice 2 — Control-surface dispositions + honest terminal → kills *no honest dead-end* + most of *scattered recovery*

- **Backend:** extend `author_operator_blockers()` over run-time gates (consolidate `start_capability` codes + `_GATE_ACTION_TABLE` + account conditions into `OperatorSurface.blockers`); add `terminal` (poisoned, retired, exited-unrecoverable).
- **Frontend:** `verdict-card-model.ts` renders `blockers[0].primary_move`; terminal card treatment (Replace/Remove, no hopeful button); why-drawer lists blockers by disposition.
- **DoD:** poisoned & retired render the dead-end; no blocker renders without a move; `fix_here` actions appear on the card.

### Slice 3 — Residual cures + anti-drift + docs → closes the tail of *scattered recovery*

- Wire the residual ★ rows: `fleet_contaminated` clear path; `orphaned_socket` / `registry_amnesia` launcher-restart guidance. (`prove_evidence` and `resolve_exposure` are already resolved as routed `fix_elsewhere` in Slice 2; a first-class `prove_evidence` endpoint remains deferred.)
- **Parity test:** a shared blocker id (`broker_disconnected`) renders identically on deploy-preflight and run surfaces — the structural anti-drift guarantee.
- Update `docs/runbooks/broker-instance-operator-surface.md`; add an ADR recording the disposition taxonomy.

## 8. Non-goals

- Redesigning the verdict card / why-drawer visuals (kept from the 2026-07-08 design).
- "Silent not-running" explanation work (operator did not rank it; the why-drawer already covers it).
- New recovery *mechanics* beyond wiring existing capabilities/endpoints to blockers (Slice 3 only wires what already exists or is a thin endpoint).
- Real-time liveness semantics — halts remain owned by the live feed per `temporal-rigor.md`; blockers render the projected state.

## 9. Decisions (resolved during spec review 2026-07-09)

1. **`broker_evidence_unproven` (`prove_evidence`) → `fix_elsewhere` (RESOLVED).** No one-call endpoint exists (`InvokeEndpointAction.endpoint` is `reconcile_instance` only, `live_runs.py:1681`). Demote to an honest routed link (Account Monitor / refresh broker evidence). Do **not** reuse the crash-recovery override — it is `CRASH_RECOVERY_REQUIRED`-specific, not generic broker-evidence proof. A real `fix_here` endpoint is deferred (out of scope for Slices 1–2).
2. **`OperatorAction` = canonical superset (RESOLVED).** The two existing unions differ — `GateSuggestedAction` (`live_runs.py:1703`) excludes `invoke_endpoint`; `TraderPrimaryRemediation` includes it. Define `OperatorAction` as the superset (existing members + `InvokeEndpointAction` + new `RetireReplaceAction`/`RemoveAction`/`ConfirmInFormAction`) and keep `GateSuggestedAction` / `TraderPrimaryRemediation` as narrower subsets. No fresh parallel taxonomy, no lossy rename of `GateSuggestedAction`.
3. **Blocker ordering / dominance (RESOLVED, default kept).** `blockers[0]` drives the single verb; reuse the existing blockage-ladder / notice-placement ordering (ADR-0025 single-dominant-headline) rather than a new sort.
4. **Terminal semantics (RESOLVED).** `Replace` → `POST /api/live-instances/{id}/retire-and-replace` (lineage kept, `confirm_account_flat` attestation, `live_instances.py:2758`). `Remove` → `DELETE /api/live-instances/{id}` (soft-delete / hide-from-catalog via `deleteBot`, `live-runs.service.ts:238`). `resolve_exposure` is a routed `fix_elsewhere` (Account Monitor flatten + audited override, `broker-account-monitor.component.ts:459`), not a ticket dead-end.

No blocking questions remain for Slice 1 execution.
