# ADR 0010 — Operator-action contract: "Flatten and pause" composes durable intent + one-shot, Resume is a guarded write, Stop is instance retirement

**Status:** Proposed 2026-06-14. Vocabulary recorded in `CONTEXT.md` § "Operator-action contract". Grilling session: `grill-with-docs` 2026-06-14 against the vibe-coded-app remediation PRD (pruned 2026-07-04; git history). Load-bearing code claims (FLATTEN aliases STOP in `live_engine.py`; durable desired-state is the single intent knob per ADR 0004 / CONTEXT.md) verified before the session.
**Decision drivers:** VCR-0007 (a vibe-coded-app audit finding — pruned 2026-07-04; git history) found the bot control page's FLATTEN button labelled "Close all open positions immediately" but the runtime semantics today are *FLATTEN = STOP + close positions*: durable desired-state transitions to `STOPPED`, the bot refuses to restart without redeploy, and there is no FLATTEN-without-STOP primitive. The label is the highest-stakes affordance in the bot control page (the panic button). Independently, CONTEXT.md § "Operator intent — single knob" had already established that durable desired-state (`RUNNING` / `PAUSED` / `STOPPED`) is the operator's single intent knob and that one-shots (`FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED`) are reserved for true one-shot operations. The current FLATTEN fuses a one-shot with a durable-intent change, which violates the orthogonality CONTEXT.md established. ADR 0009 / `BROKER_SAFETY_VERDICT_TRANSITION_HALT` semantics (ADR 0011) introduce halt paths that themselves set `desired_state=PAUSED` and require operator action to leave; making the Resume action *guarded* (verdict-aware) is the seam where these halt contracts compose with operator intent.
**Related:** ADR 0004 (instance-addressed operator control plane — defines durable desired-state as the operator's single intent surface), ADR 0008 (durable submit / order identity — the SUBMIT_UNCERTAIN_HALT path sets `desired_state=PAUSED` and requires guarded Resume), ADR 0011 (broker safety verdict — defines the halt-on-transition path that also lands in `desired_state=PAUSED` and feeds the guarded Resume), `CONTEXT.md` § "Operator-action contract", `CONTEXT.md` § "Operator intent — single knob", the vibe-coded-app remediation PRD Phase 6 (pruned 2026-07-04; git history).

## Context

Today's runtime behavior (verified against the code at session time):

| Concern | Today |
|---|---|
| FLATTEN semantics | `FLATTEN` is documented to "alias to STOP" inside `live_engine.py`. Persists `STOPPED` to durable desired-state. Actual position flatten fires through `_shutdown_flatten`, which depends on the bar loop honoring `shutdown_event`. After FLATTEN, the bot will not restart without explicit operator redeploy or hand-edit of durable desired-state. |
| UI label | The bot control page FLATTEN button is labelled along the lines of "Close all open positions immediately" with no signal that the bot also stops. The operator under stress (the moment FLATTEN matters most) sees an action verb that doesn't disclose its full effect. |
| PAUSE / RESUME / STOP | Per CONTEXT.md § "Operator intent — single knob", PAUSE/RESUME/STOP are removed as first-class UI controls. Operator intent is conveyed by writing durable `desired_state` ∈ `{RUNNING, PAUSED, STOPPED}` via `/api/live-instances/{id}/desired-state`. The bot control page's "Pause," "Resume," and "Stop" buttons are now UI actions that drive that endpoint. |
| One-shots | CONTEXT.md § "One-shot command channel" reserves `FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED` (and possibly `DUMP_STATUS`) for true one-shot operations. The channel is orthogonal to durable desired-state. |
| Halt paths | ADR 0008's submit-uncertain halt and ADR 0011's broker-safety verdict transition halt both set durable `desired_state=PAUSED` and require operator action to leave. Resume from those halts must consult the current state of the world (verdict, ownership reconciliation, unresolved uncertain intents) before promoting to RUNNING — otherwise the operator can trivially re-enter the dangerous state with a button click. |

The audit grilling enumerated four candidate FLATTEN shapes:

1. **True FLATTEN keeps the bot running** with durable intent unchanged.
2. **`STOP_AND_FLATTEN` composite** (a second verb that fuses stop and flatten).
3. **FLATTEN closes positions AND sets `desired_state=PAUSED`** ("Flatten and pause") — the panic-button mental model.
4. **Keep current fused FLATTEN = STOP + close** with an honest rename ("Stop and close positions").

Shape (1) is unsafe: a bot that flattens but keeps running can re-enter on the next eligible bar — the opposite of what the panic-button mental model expects. Shape (2) is convenience-only; it duplicates primitives and trains operators to think of STOP as a normal operator button rather than a retirement path. Shape (4) cements the fused semantics in the data model and contradicts the desired-state / one-shot separation. Shape (3) — "Flatten and pause" — matches the panic-button mental model: *get flat now and don't take a new position until I say so.*

## Decision

### 1. UI affordances are five rows; underlying primitives are two

The bot control page's primary operator actions and their effects, as locked:

| UI Action | Durable Intent | One-shot Work | Process |
|---|---|---|---|
| Pause | `PAUSED` | none | alive |
| Resume | `RUNNING` (guarded — see Decision 3) | none | alive |
| Flatten and pause | `PAUSED` | cancel owned opens + liquidate | alive |
| Stop | `STOPPED` | graceful shutdown; optional flatten only if explicitly configured | exits |
| Mark poisoned | unchanged or blocked | write poison flag | depends on policy |

Underlying primitives:

- The **durable desired-state endpoint** (`/api/live-instances/{id}/desired-state`) writes `RUNNING` / `PAUSED` / `STOPPED`.
- The **one-shot command channel** dispatches `FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED` (and possibly `DUMP_STATUS`).

Every UI action above resolves to one or both primitives. There are no other operator primitives in v1.

### 2. "Flatten and pause" is composed at the endpoint; `FLATTEN_NOW` stays pure

`FLATTEN_NOW` MUST NOT mutate durable desired state. The "Flatten and pause" UI endpoint performs, in order:

1. write `desired_state = PAUSED`
2. enqueue one-shot `FLATTEN_NOW`

This preserves the desired-state / one-shot orthogonality. The one-shot is reusable from CLI / panic / scripts without hidden intent side effects. If the composition is needed in another context, the next consumer composes the same two primitives — the runtime contract is not duplicated.

If step 1 fails (durable write fails), the endpoint aborts before step 2 — no flatten without first registering the pause. If step 2 fails (queue down), the operator sees a clear "pause registered, flatten not enqueued, retry" response — the durable state is already PAUSED, so the bar loop will refuse new entries even without the flatten.

After "Flatten and pause" completes, the bot remains alive, durable intent is `PAUSED`, and the bar loop refuses new entries until the operator's intent changes. There is no FLATTEN-without-STOP primitive in v1 — if the panic-button mental model is the wrong shape for a future use case (scheduled square-up, intra-session de-risk-and-resume), add that primitive then.

### 3. Resume is a guarded write — never a way around the verdict or halt state

Three runtime contracts attach to Resume:

- ADR 0008 (`SUBMIT_UNCERTAIN_HALT`): Resume is allowed only if cold-start / runtime ownership reconciliation is clean **and** no unresolved uncertain intent remains in the WAL.
- ADR 0011 (broker safety verdict): Resume is allowed only if `broker_safety.final_verdict == "paper-only"`.
- This ADR: Resume is, mechanically, a write of `desired_state = RUNNING` — but the write is gated by the above contracts. The UI action's name and effect (RUNNING) are the same; what changes is that the endpoint checks the guards before promoting state.

If any guard fails, durable state stays at `PAUSED` and the API surfaces the failing reason (`broker_safety_not_paper_only`, `unresolved_uncertain_intent`, `reconciliation_not_clean`, etc.). The bot control page renders the failing reason next to the Resume button so the operator can act on it.

This makes "operator clicks Resume and bot returns to dangerous state" structurally impossible. The verdict, the reconciliation result, and the unresolved-intent ledger are all read-only inputs to the endpoint — the operator cannot bypass them by clicking the button.

### 4. STOP is heavy — instance retirement, not a normal operator button

`desired_state = STOPPED` is the instance-retirement path: process exits; resume requires redeploy or explicit operator action on durable state. STOP does **not** flatten by default — if positions need to close, the operator hits "Flatten and pause" first.

Rationale: STOP is the only durable state from which the system intentionally makes resumption heavy. If STOP were lightweight (one click, easy to undo), it would feel like a normal operator button and would be used as a panic flatten by operators who don't know the difference. Heavy STOP reserves the action for the situation where the operator has decided this *instance* is done — not just this position.

The optional "flatten on STOP" path is explicitly configured per instance (a deploy-time setting on `StrategyRegistration` or a `live_config` field, named TBD in implementation). It is *not* a runtime toggle — operators do not arrive at "flatten on STOP" by accident.

### 5. No `STOP_AND_FLATTEN` composite v1; no "flatten without changing intent" v1

`STOP_AND_FLATTEN` would be a convenience composite. It is rejected because:

- It duplicates the composition rule from Decision 2 at a second site.
- It re-introduces a button that performs both a durable-intent change and a one-shot — exactly the failure mode VCR-0007 surfaced.
- It encourages operators to think of STOP as a normal button (see Decision 4).

A "flatten without changing intent" primitive is rejected v1 because:

- It is the wrong shape for the panic-button mental model — the bar loop would re-enter on the next eligible signal.
- The future use case (scheduled square-up, intra-session de-risk-and-resume) is real but speculative; the primitive should be added when it has a named consumer.

Future revisions to this ADR may revisit either if a concrete use case appears.

## Consequences

**Positive:**

- The bot control page's panic button matches the operator's mental model. The runtime effect and the label both communicate "flatten and stand by." There is no hidden side effect.
- Desired-state and one-shot primitives stay orthogonal — the runtime contract that CONTEXT.md established is now uniformly honored at the bot control page. `FLATTEN_NOW` is reusable from CLI/panic/scripts.
- Resume is structurally safe: no button click bypasses the broker safety verdict, the ownership reconciliation, or the uncertain-intent ledger.
- STOP is reserved for instance retirement, which means operators are not trained to use it for de-risking. The "what does this button do?" surface area is small and consistent.

**Negative:**

- The bot control page gains an extra step for the operator who *did* want to stop the bot and flatten in one action. That operator now hits "Flatten and pause," waits for the liquidation to complete, then hits "Stop." The trade-off is intentional: the two-step is the same shape as the underlying primitives, and the one-shot can complete asynchronously while the operator decides whether STOP is the right next action.
- The Resume button's behavior is no longer "writes RUNNING" — it is "attempts to write RUNNING, may be refused." This requires careful UI copy so operators understand why a click might not transition state. The endpoint returns a structured reason; the bot control page renders it.
- Existing UI code that maps the FLATTEN button to "set desired_state=STOPPED + run shutdown-flatten" needs to be rewritten to the new composition. The PRD's Phase 6 owns this work.
- The `STOP` flatten-on-exit option is an explicitly named per-instance setting — adding it requires a small `live_config` / `StrategyRegistration` extension. Implementation lands when the use case is real.

**Non-consequences:**

- The durable-state values and semantics (`RUNNING` / `PAUSED` / `STOPPED`) are unchanged.
- The one-shot command channel verbs (`FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED`) are unchanged.
- The instance-addressed control plane (ADR 0004) is unchanged.
- Backend-compatible `PAUSE` / `RESUME` / `STOP` verbs on the one-shot channel (kept per CONTEXT.md for CLI / panic / older run-addressed paths) are not touched. The bot control page no longer dispatches them; CLI and panic paths may continue to.

---

## Amendment 2026-06-20 — PRD #616 reconciliation of contract with implementation

**Status:** Amended 2026-06-20 alongside the PRD #616 Bot Control redesign work. The original ADR named "five canonical actions" but the shipped capability surface and code carried only four. The shipped guard contract on Resume was also incomplete relative to Decision 3. This amendment records the resolution.

### A1. Five canonical actions on `operator_surface.actions`

The operator surface now ships exactly the five capabilities named in Decision 1:

```
operator_surface.actions = {
  resume:            ActionCapability,
  pause:             ActionCapability,
  stop:              ActionCapability,   # added — was missing pre-#616
  flatten_and_pause: ActionCapability,
  mark_poisoned:     ActionCapability,
}
```

`actions.stop` shares the shared capability evaluator and the same intent-state-pair rules. The bot control page's "Stop" affordance lives in the identity-strip overflow menu (PRD #617 §"Identity / control strip") and never appears inline with normal Resume/Pause/Flatten — that placement enforces Decision 4's "heavy STOP, not a normal button" rule by separation rather than by warning copy alone. Mark Poisoned likewise lives only on the Audit tab (PRD #617 §"Audit tab").

### A2. Canonical render-site rule for destructive actions

Each destructive action has **exactly one** canonical render site in the bot control page (PRD #617 §"Cutover criteria"):

- **Mark Poisoned** → Audit tab, typed-HALT confirmation. Never inline on a readiness gate.
- **Stop** → identity-strip overflow menu, retirement confirmation. Never inline on a readiness gate.
- **Flatten-and-pause** → identity strip primary button. Never inline on a readiness gate.

`OperatorGate.suggested_action` (PRD #616) authors only non-destructive actions inline via `invoke_capability`; destructive actions reach the operator only via `focus_action`, which is a navigation hint to the canonical render site, not an inline button (see ADR 0005 §Amendment 2026-06-20 for the projection layer's authority).

### A3. Resume guards reconciled with implementation

Decision 3 named three guards (broker safety verdict, ownership reconciliation, unresolved uncertain intent). Pre-PRD #616 the *capability projection* returned `actions.resume.enabled=true` unconditionally and the *mutation endpoint* did not re-evaluate the guards. PRD #616 reverses both:

- The shared `ResumeGuardState` resolver lives in `app/services/resume_guard_state.py`. It is consumed by the capability projection (`operator_surface.actions.resume / pause / stop`), the desired-state mutation endpoint (`POST /api/live-instances/{sid}/desired-state` re-runs the resolver immediately before the durable write), and the CLI `cmd_resume` (`app/engine/live/run.py`).
- The closed reason-code vocabulary is documented in `RESUME_REASON_CODES` and pinned by unit tests.
- The reason-priority order for the single-line tooltip is `STOPPED_REQUIRES_REDEPLOY → BROKER_SAFETY_UNSAFE → BROKER_SAFETY_UNKNOWN → UNRESOLVED_UNCERTAIN_INTENT → UNCERTAIN_INTENT_STATE_UNKNOWN → RECONCILIATION_*  → REDEPLOY_REQUIRED → ALREADY_RUNNING/PAUSED`. The structured response carries the full list.
- Reconciliation receipts surface as `RECONCILIATION_NOT_AVAILABLE` (honest, informational) until the writer is wired downstream — the resolver does not assume `NOT_AVAILABLE` blocks Resume; a future caller can opt in via `relevant_after_ms` at the mutation boundary.

### A4. CLI `--force` deleted

The legacy `--force` flag on `cmd_resume` was deleted. The bot control page cannot claim "guarded Resume is structurally safe across every entry point" while a CLI-only bypass shares the same resolver. Operators that need to clear a guard must resolve the underlying condition (re-confirm paper-only broker, reconcile uncertain intents, redeploy a poisoned run); each guard has a documented remediation path. The structural rule replaces the bypass — every other entry point shares the same resolver, so deleting `--force` was the only resolution consistent with the structural claim.

### A5. Intent-state-pair rules

The capability evaluator layers four intent-state rules above the artifact guards:

- `ALREADY_RUNNING` — Resume refused when current intent is RUNNING.
- `ALREADY_PAUSED` — Pause refused when current intent is PAUSED.
- `STOPPED_REQUIRES_REDEPLOY` — Resume / Pause refused when current intent is STOPPED. STOPPED is the retirement state; the revival path is Redeploy (Decision 4).
- `REDEPLOY_REQUIRED` — Resume / Pause / Stop refused when the run is poisoned. The poisoned binding is dead; revival is Redeploy.

These rules apply at the capability projection only; the artifact guards apply to the mutation endpoint and the CLI as well. The structural separation keeps the "the CLI does not consult the desired-state pair" rule honest — the CLI sets durable intent; the intent-state-pair rules are bot control-affordance presentation rules.


---

## Amendment 2026-06-22 — PRD #619-D mutation uncertainty + recovery

Status: shipping with PRs #637 (D1), #638 (D2), #639 (D3), #640 (D4) and this PR (D5). Scope is the mutation-uncertainty + recovery layer named in PRD #619 §6 619-D and rejects ADR-0008 changes — order-submit identity remains 0008's concern, not this one.

### B1. Durable `mutation_attempt` record (D1)

Every operator mutation (`start` / `stop` / `flatten` / `resume` / `pause`) writes a durable `MutationAttempt` artifact **before** the HTTP request leaves the data plane. The record lives at `<artifacts>/mutation_attempts/<attempt_id>.json` (flat layout per instance until volume requires an index), atomic via the canonical `atomic_write_pydantic_artifact` writer.

State machine (enforced by `transition_attempt`):

```
PREPARED → DISPATCHING → { RESPONSE_CONFIRMED | OUTCOME_UNKNOWN }
                                      ↓
       { EFFECT_CONFIRMED | EFFECT_NOT_OBSERVED | NOT_PROVABLE | EVIDENCE_CONFLICT }
```

`OUTCOME_UNKNOWN` is the conservative default: if the HTTP client cannot prove the request was not transmitted, the attempt classifies as `OUTCOME_UNKNOWN`, never as `RESPONSE_CONFIRMED`-with-some-status (PRD #619-C5 surfacing path; this amendment makes the record durable).

`mutation_attempt_id` is **audit-only** in D — the daemon does not yet enforce it as an idempotency key. Persisting it now means the C5 synchronous surfacing pass can be promoted to durable without a storage migration.

### B2. Action-conflict matrix (D2)

`operator_surface.actions` is now authored against the latest persisted `MutationAttempt` for the instance. The matrix engages whenever `dispatch_state != EFFECT_CONFIRMED` — including the three non-confirmed terminals (`EFFECT_NOT_OBSERVED`, `NOT_PROVABLE`, `EVIDENCE_CONFLICT`) which mean "we couldn't prove the prior mutation landed."

| Prior unresolved | Blocks | Reason code |
|---|---|---|
| Stop | Resume, Stop | `MUTATION_UNRESOLVED_STOP` |
| Flatten | Flatten | `MUTATION_UNRESOLVED_FLATTEN` |
| Resume | Resume | `MUTATION_UNRESOLVED_RESUME` |

`MUTATION_UNRESOLVED_START` is reserved in the closed vocabulary for the router-level `start_run` / Redeploy gate (own follow-up); it does not block any `evaluate_action` surface in v1.

Pause is **never** in the matrix — pause is goal-idempotent (always safe to stop new entries). Mark-poisoned is **never** in the matrix — it is incident recovery and must remain available when posture is degraded.

The matrix and POSTURE_DEMOTED guard ride **alongside** existing blockers (e.g. `NO_LIVE_BINDING`) — the operator sees every applicable reason code in `disabled_reasons[]`, never just one. Reason-code priority is unchanged (existing intent-state-pair codes sort first; matrix codes sort last alongside legacy live-binding codes).

### B3. Effect-based Reconcile action (D3)

`POST /api/live-instances/{sid}/reconcile-mutation` is the read-only effect inspector. **It never replays the original mutation.** The classifier `reconcile_mutation_effect(attempt, evidence)` is pure; it returns one of `EFFECT_CONFIRMED` / `EFFECT_NOT_OBSERVED` / `EVIDENCE_CONFLICT` / `NOT_PROVABLE` based on the action type and the assembled evidence (daemon process state, child `engine_runtime.json`, durable desired-state, broker owned-positions).

Per-action classification rules are encoded in `_reconcile_stop` / `_reconcile_start` / `_reconcile_resume` / `_reconcile_pause` / `_reconcile_flatten` and tested cell-by-cell. `daemon_reachable=False` short-circuits every action to `NOT_PROVABLE` — the daemon owns process identity, so a daemon outage means the read is fundamentally stale.

Reconcile boundaries:

- **404** when no `MutationAttempt` has been persisted for the instance.
- **409** when the attempt is `PREPARED` / `DISPATCHING` (in flight) or already terminal (one-shot per attempt; re-classification of stale terminals is a separate ADR).
- **200** + typed `ReconcileMutationResponse` otherwise. The router advances the attempt to the resulting terminal via `transition_attempt`, persists, and returns.

`EFFECT_NOT_OBSERVED` is **not** automatic permission to retry the mutation. The operator surface still gates the next action through the matrix until the prior attempt reaches `EFFECT_CONFIRMED` (the only matrix-disengaging state).

### B4. `broker_observation_consistency` divergence surface (D4)

`OperatorSurface.broker_observation_consistency` carries a backend-authored verdict comparing the child's broker observation (from `engine_runtime.broker.connected_account`) against the data plane's singleton snapshot (from `snapshot_data_plane_broker`). Four-way verdict: `CONSISTENT` / `CONFLICTING` / `UNKNOWN` / `NOT_COMPARABLE`. Mode mismatch (paper vs live) outranks account mismatch → `NOT_COMPARABLE`.

The bot control page renders the divergence card prominently on `CONFLICTING` but **never** overwrites the child's authoritative posture on `OperatorSurface.broker` — ADR-0011 makes the child observation authoritative for the bound instance; this amendment preserves that invariant.

### B5. Out of scope

This amendment intentionally does NOT:

- Touch ADR-0008 (`SUBMIT_UNCERTAIN_HALT`). The mutation-attempt contract sits at a different altitude — it governs control-plane mutations of operator intent, not broker-submit identity.
- Re-open the `--force` CLI bypass (Amendment A4 still holds).
- Add automatic mutation retry. Reconcile is read-only; the operator (or a future supervised retry primitive) decides whether to re-issue.
- Add daemon-enforced `mutation_attempt_id` idempotency. That is a future PRD; today the id is audit-only.
