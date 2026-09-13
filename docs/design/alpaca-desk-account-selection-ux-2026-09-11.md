# Alpaca desk account-selection UX

Status: implementation plan

## Outcome

The Alpaca desk must distinguish configuration state from broker reachability.
When no account is effective, the page guides the operator through selecting a
verified account configuration instead of rendering account-data failures and
empty Trader/Operator panels. When an account is effective, the existing desk
remains the operational surface and broker read failures keep their current,
honest meaning.

The server owns the activation-state classification and all operator-facing
guidance. The browser renders that projection and opens the existing
configuration review flow; it does not infer whether a profile is safe,
whether a restart is required, or whether Live is armed.

## Interaction model

1. The desk reads the configuration projection and the existing account
   snapshot. A successful account snapshot remains the authority for whether
   account-scoped controls can render; this preserves the generation-zero
   environment-bootstrap compatibility path, which has no effective profile.
2. Without an effective account, the desk shows a lifecycle strip for active
   account, selected configuration, and worker handover.
3. Only non-archived, complete revisions with an explicitly approved account
   pin are offered as account choices. This read never resolves or probes a
   credential and never re-verifies an account; credential availability remains
   part of the Configuration review and Apply refusal flow.
4. Choosing an account changes only the local radio selection. The primary
   action opens the configuration page with that exact profile/revision
   preselected for review. Stage and Apply remain explicit controls on the
   configuration page.
5. Staging never switches the running worker. Apply remains a separate,
   deliberate action and a controlled restart remains the only handover to the
   new effective selection.
6. Selecting or applying a Live profile never arms Live trading and never
   retargets an existing strategy instance.
7. With an effective selection, the existing account summary, safety surfaces,
   Trader/Operator lenses, order entry, and Deploy action render normally. A
   different staged revision or pending restart appears as a compact change
   banner without replacing the connected desk.
8. “Active” is installation-scoped, never global. V1 has one worker and one
   effective account per installation. Concurrent Paper and Live trading uses
   isolated installations/workers with separate ports, Clerk volumes,
   credentials, and custody. Two browser sessions against one backend do not
   create two workers. A future cross-installation view may observe both, but
   this selector must not imply that it reassigns bots across installations.

## Future improvement: one desk, multiple clerks

A later version may let one frontend operate Paper and Live at the same time by
addressing multiple isolated clerks or execution contexts behind one desk. The
frontend would show both account lanes concurrently and every bot/run command
would carry an explicit target context so the backend can route it to the
correct clerk, credential boundary, broker endpoint, and custody state.

This is a presentation and orchestration unification, not a relaxation of the
safety boundary. Each clerk must retain its own effective configuration,
selection generation, idempotency and stale-write fences, Live arming policy,
audit trail, and failure state. A Paper failure must not disable or retarget the
Live clerk, and no action may infer its destination from whichever account is
currently selected in the browser. The current single-clerk contract is kept
deliberately explicit so it can become one lane in that future multi-clerk
model without changing the meaning of “effective.”

**Status 2026-09-12:** this future is now accepted as ADR 0062 (the broker
clerk fleet control plane; PRD `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md`).
The fleet spine — opaque clerk identities, broker-qualified account-assignment
fencing, and the provider-adapter boundary — landed as `PythonDataService/app/broker/fleet/`.
Nothing in this desk changes yet: the explicit `{broker}/{clerk_id}` routes and
the frontend cutover are the PRD's Phases 3–4, and this document's
single-clerk contract remains the desk's authority until then.

## Backend interface

Add a protected read beneath the existing broker-configuration router:

`GET /api/brokers/alpaca/configuration/desk-state`

The response is a single deep interface for the desk:

- `activation_state`: `no_selection`, `staged_not_applied`,
  `apply_requested_restart_required`, or `effective_selection`.
- backend-authored `headline`, `detail`, `selection_label`, and `consequence`.
- ordered lifecycle items describing active account, selected configuration,
  and worker state.
- a backend-authored primary action descriptor whose typed kind is mapped to
  frontend-owned navigation; the server does not author browser URLs.
- exact staged and effective choice projections when present.
- selectable account choices carrying the exact profile ID, revision, account
  ID, nickname/display label, endpoint mode, status copy, and a backend-authored
  action label/kind.

The projection never returns credential slot identifiers or secret material.
It is derived only from durable configuration records and never contacts the
broker. Broker transport reachability is an orthogonal account-read outcome; a
historical effective acknowledgement is not represented as current process
liveness or connectivity.

## Frontend composition

Create a presentation component for the inactive-account experience. It owns
only accessible choice interaction and emits exact selection/action intents.
The existing desk remains the container and owns reads, fenced staging, reload,
navigation, and refusal rendering.

When the account snapshot is unavailable and `activation_state !==
effective_selection`:

- render the backend headline, detail, lifecycle, choices, consequence, and
  action labels;
- hide account summary, hold/custody surfaces, Trader/Operator tabs and panels,
  manual-order entry, and the Deploy action;
- keep Configuration/Manage accounts available;
- use a real fieldset/radio group for the ephemeral choice, preserve focus, and
  describe the action's consequence through `aria-describedby`.

When the account snapshot succeeds, render the existing desk even if the
profiles database has no effective reference; that is the explicit
generation-zero environment-bootstrap compatibility case. When an effective
selection exists but the broker account read fails, retain the specific
connectivity failure instead of falling back to the configuration empty state.
A different staged or apply-requested revision appears as a compact
pending-change banner without hiding the still-connected account desk.

## Test seams

### FastAPI

Exercise the protected HTTP response for all four durable states. Pin literal
copy and choice eligibility at the public response, including exclusion of
archived, incomplete, unpinned, and unavailable-credential profiles. Assert no
credential identifiers or secret-like values cross the response.

### Angular

Exercise rendered behavior through the component and desk:

- inactive state hides all account-scoped controls;
- backend-authored copy is rendered unchanged;
- choosing a row changes no durable state by itself;
- the primary action navigates with the exact selected profile/revision without
  writing configuration state;
- effective state preserves the existing Trader/Operator and Deploy behavior;
- radio group, labels, action disabled state, keyboard interaction, status, and
  alert semantics are accessible.

## Deliberate exclusions

- No worker restart button or process-control endpoint.
- No Live arming/disarming controls.
- No typed account-ID entry and no credential form.
- No multi-clerk orchestration or simultaneous multi-account custody in this
  change; the future direction is documented above.
- No retargeting of existing strategy-instance bindings.
- No attempt to infer current worker liveness from the historical effective
  acknowledgement.
