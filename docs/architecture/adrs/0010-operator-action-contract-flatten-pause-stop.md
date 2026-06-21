# ADR 0010 — Operator-action contract: "Flatten and pause" composes durable intent + one-shot, Resume is a guarded write, Stop is instance retirement

**Status:** Proposed 2026-06-14. Vocabulary recorded in `CONTEXT.md` § "Operator-action contract". Grilling session: `grill-with-docs` 2026-06-14 against `docs/audits/vibe-coded-app-remediation-prd.md`. Load-bearing code claims (FLATTEN aliases STOP in `live_engine.py`; durable desired-state is the single intent knob per ADR 0004 / CONTEXT.md) verified before the session.
**Decision drivers:** VCR-0007 (audit `docs/audits/vibe-coded-app-research/findings/VCR-0007-flatten-aliases-stop-ui-claims-immediate-close.md`) found the cockpit's FLATTEN button labelled "Close all open positions immediately" but the runtime semantics today are *FLATTEN = STOP + close positions*: durable desired-state transitions to `STOPPED`, the bot refuses to restart without redeploy, and there is no FLATTEN-without-STOP primitive. The label is the highest-stakes affordance in the cockpit (the panic button). Independently, CONTEXT.md § "Operator intent — single knob" had already established that durable desired-state (`RUNNING` / `PAUSED` / `STOPPED`) is the operator's single intent knob and that one-shots (`FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED`) are reserved for true one-shot operations. The current FLATTEN fuses a one-shot with a durable-intent change, which violates the orthogonality CONTEXT.md established. ADR 0009 / `BROKER_SAFETY_VERDICT_TRANSITION_HALT` semantics (ADR 0011) introduce halt paths that themselves set `desired_state=PAUSED` and require operator action to leave; making the Resume action *guarded* (verdict-aware) is the seam where these halt contracts compose with operator intent.
**Related:** ADR 0004 (instance-addressed operator control plane — defines durable desired-state as the operator's single intent surface), ADR 0008 (durable submit / order identity — the SUBMIT_UNCERTAIN_HALT path sets `desired_state=PAUSED` and requires guarded Resume), ADR 0011 (broker safety verdict — defines the halt-on-transition path that also lands in `desired_state=PAUSED` and feeds the guarded Resume), `CONTEXT.md` § "Operator-action contract", `CONTEXT.md` § "Operator intent — single knob", `docs/audits/vibe-coded-app-remediation-prd.md` Phase 6.

## Context

Today's runtime behavior (verified against the code at session time):

| Concern | Today |
|---|---|
| FLATTEN semantics | `FLATTEN` is documented to "alias to STOP" inside `live_engine.py`. Persists `STOPPED` to durable desired-state. Actual position flatten fires through `_shutdown_flatten`, which depends on the bar loop honoring `shutdown_event`. After FLATTEN, the bot will not restart without explicit operator redeploy or hand-edit of durable desired-state. |
| UI label | The cockpit FLATTEN button is labelled along the lines of "Close all open positions immediately" with no signal that the bot also stops. The operator under stress (the moment FLATTEN matters most) sees an action verb that doesn't disclose its full effect. |
| PAUSE / RESUME / STOP | Per CONTEXT.md § "Operator intent — single knob", PAUSE/RESUME/STOP are removed as first-class UI controls. Operator intent is conveyed by writing durable `desired_state` ∈ `{RUNNING, PAUSED, STOPPED}` via `/api/live-instances/{id}/desired-state`. The cockpit's "Pause," "Resume," and "Stop" buttons are now UI actions that drive that endpoint. |
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

The cockpit's primary operator actions and their effects, as locked:

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

If any guard fails, durable state stays at `PAUSED` and the API surfaces the failing reason (`broker_safety_not_paper_only`, `unresolved_uncertain_intent`, `reconciliation_not_clean`, etc.). The cockpit renders the failing reason next to the Resume button so the operator can act on it.

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

- The cockpit's panic button matches the operator's mental model. The runtime effect and the label both communicate "flatten and stand by." There is no hidden side effect.
- Desired-state and one-shot primitives stay orthogonal — the runtime contract that CONTEXT.md established is now uniformly honored at the cockpit. `FLATTEN_NOW` is reusable from CLI/panic/scripts.
- Resume is structurally safe: no button click bypasses the broker safety verdict, the ownership reconciliation, or the uncertain-intent ledger.
- STOP is reserved for instance retirement, which means operators are not trained to use it for de-risking. The "what does this button do?" surface area is small and consistent.

**Negative:**

- The cockpit gains an extra step for the operator who *did* want to stop the bot and flatten in one action. That operator now hits "Flatten and pause," waits for the liquidation to complete, then hits "Stop." The trade-off is intentional: the two-step is the same shape as the underlying primitives, and the one-shot can complete asynchronously while the operator decides whether STOP is the right next action.
- The Resume button's behavior is no longer "writes RUNNING" — it is "attempts to write RUNNING, may be refused." This requires careful UI copy so operators understand why a click might not transition state. The endpoint returns a structured reason; the cockpit renders it.
- Existing UI code that maps the FLATTEN button to "set desired_state=STOPPED + run shutdown-flatten" needs to be rewritten to the new composition. The PRD's Phase 6 owns this work.
- The `STOP` flatten-on-exit option is an explicitly named per-instance setting — adding it requires a small `live_config` / `StrategyRegistration` extension. Implementation lands when the use case is real.

**Non-consequences:**

- The durable-state values and semantics (`RUNNING` / `PAUSED` / `STOPPED`) are unchanged.
- The one-shot command channel verbs (`FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED`) are unchanged.
- The instance-addressed control plane (ADR 0004) is unchanged.
- Backend-compatible `PAUSE` / `RESUME` / `STOP` verbs on the one-shot channel (kept per CONTEXT.md for CLI / panic / older run-addressed paths) are not touched. The cockpit no longer dispatches them; CLI and panic paths may continue to.

---

## Amendment 2026-06-20 — PRD #616 reconciliation of contract with implementation

**Status:** Amended 2026-06-20 alongside the PRD #616 cockpit-redesign work. The original ADR named "five canonical actions" but the shipped capability surface and code carried only four. The shipped guard contract on Resume was also incomplete relative to Decision 3. This amendment records the resolution.

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

`actions.stop` shares the shared capability evaluator and the same intent-state-pair rules. The cockpit's "Stop" affordance lives in the identity-strip overflow menu (PRD #617 §"Identity / control strip") and never appears inline with normal Resume/Pause/Flatten — that placement enforces Decision 4's "heavy STOP, not a normal button" rule by separation rather than by warning copy alone. Mark Poisoned likewise lives only on the Audit tab (PRD #617 §"Audit tab").

### A2. Canonical render-site rule for destructive actions

Each destructive action has **exactly one** canonical render site in the cockpit (PRD #617 §"Cutover criteria"):

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

The legacy `--force` flag on `cmd_resume` was deleted. The cockpit cannot claim "guarded Resume is structurally safe across every entry point" while a CLI-only bypass shares the same resolver. Operators that need to clear a guard must resolve the underlying condition (re-confirm paper-only broker, reconcile uncertain intents, redeploy a poisoned run); each guard has a documented remediation path. The structural rule replaces the bypass — every other entry point shares the same resolver, so deleting `--force` was the only resolution consistent with the structural claim.

### A5. Intent-state-pair rules

The capability evaluator layers four intent-state rules above the artifact guards:

- `ALREADY_RUNNING` — Resume refused when current intent is RUNNING.
- `ALREADY_PAUSED` — Pause refused when current intent is PAUSED.
- `STOPPED_REQUIRES_REDEPLOY` — Resume / Pause refused when current intent is STOPPED. STOPPED is the retirement state; the revival path is Redeploy (Decision 4).
- `REDEPLOY_REQUIRED` — Resume / Pause / Stop refused when the run is poisoned. The poisoned binding is dead; revival is Redeploy.

These rules apply at the capability projection only; the artifact guards apply to the mutation endpoint and the CLI as well. The structural separation keeps the "the CLI does not consult the desired-state pair" rule honest — the CLI sets durable intent; the intent-state-pair rules are cockpit-affordance presentation rules.

