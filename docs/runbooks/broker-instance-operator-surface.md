# Broker Instance Operator Surface — Runbook

Status: shipping with #565 PR 13 (cleanup); IA revised 2026-06-17 via `grill-with-docs` (see "IA revision 2026-06-17" below).  
Audience: operators (traders) running paper / live bots from `/broker/instances/:id`.  
Engineer view: keep this in lockstep with the per-card disclosures and the Detective section tabs.

## What this page answers

The page is reorganized around **six** questions a trader actually asks during an incident, in decision priority order:

1. **What is it configured to do?** — strategy + order mode + daily cap + sizing rule (a 6th question added 2026-06-17; promoted above readiness because pre-trade verification asks this before anything else)
2. **What is it holding right now?** — current risk, positions, pending orders, daily cap
3. **Can it trade?** — readiness verdict, gates that are not passing
4. **Is it safe to let it trade?** — last session ended cleanly vs. fatal halt / poisoned
5. **What did it just do?** — chart, latest signal strip, recent trades
6. **What do I need to fix?** — recent incidents, audit & diagnostics

Anything that doesn't help answer one of those questions lives behind a disclosure or in the Detective section's Diagnostics tab.

The banner answers the at-a-glance form of questions 3 and 4 via pills (readiness pill + prior-run success/failure chip), which is why the full Can-It-Trade card is demoted to a collapsed default and only auto-expands when the verdict needs attention.

## Layout — top to bottom (revised 2026-06-17)

The layout is now ranked by the six trader questions above. Cards default to a one-line summary in steady state and auto-expand when the operator needs to act (see "Page-wide collapse rule" under Honest contracts).

| Rank | Section | Component | What it answers |
|------|---------|-----------|-----------------|
| — | Fleet header | `<app-fleet-header>` | Fleet-wide account state (PAPER pill, IBKR **connection** liveness, contamination verdict, account safety actions) |
| — | Tab strip (≥2 bots) | inline `<nav class="tab-strip">` | Switch between deployed bots; stable deployment order |
| **1** | **Sticky banner** | `<app-sticky-control-bar>` (extended) | Bot identity + intent pill (RUNNING/PAUSED/STOPPED) + process state pill (running/stopping/exited/unreachable) + safety verdict pill (ADR 0011: paper-only/unsafe/unknown) + prior-run success/failure chip + action toolbar (`p-toolbar`: Resume / Pause / Flatten-and-pause / kebab→Stop). Disabled actions render a `DISABLED_REASON_COPY` tooltip. |
| **2** | **Configuration card** | `<app-configuration-card>` (new — fuses today's Strategy Rules + Sizing) | Always-visible summary row: strategy + order mode + daily cap + sizing summary + Redeploy CTA. Expands when readiness has a *config-shaped failing gate*. When expanded, the card header pins a Current Risk summary chip (posture + pending count + $-at-risk). Sizing detail is a nested accordion; portfolio audit collapsed inside; per-trade audit embedded as a table. |
| **3** | **Current Risk card** | `<app-current-risk-card>` | Collapsed to a one-line posture summary when **Flat AND no pending orders**; expanded otherwise. Positions, pending orders, daily cap, sizing. |
| **4** | **Can-It-Trade card** | `<app-can-it-trade-card>` (renamed from Readiness card) | Collapsed `READY · N checks pass` with **green** border when READY. Auto-expanded with **amber** border on DEGRADED, **red** border on BLOCKED, **grey** border on UNKNOWN. Passive — never auto-pops the checklist modal. The banner's readiness pill deep-links into this card AND into the floating checklist modal. |
| **5** | **Detective section (tabbed)** | inline `<nav>` + tab panels | Tabs: **Activity** (Chart + Latest Signal strip + Trades table) / **Diagnostics** (Recent Incidents + Last Session detail + Audit accordion). Compact icon-button `View run log` lives in the Audit accordion header (no run_id on the button face; run_id appears inside the modal). Poison button lives in the **Diagnostics tab header** — forces the operator to switch to the evidence tab before quarantining. |
| **6** | **Floating Pre-Trade Checklist** | `<p-dialog>` non-modal at `position="bottomright"` | Minimized chat-bubble FAB by default ("Checklist · N fail" with failing-count badge). Operator-triggered only (no auto-pop). Expanded panel shows failing gates first; passed gates collapsed behind "Show N passed ▾". Minimize-only (never fully dismissible). |

## Honest contracts

- **Timestamps** — wire and storage are `int64 ms UTC` end-to-end. Display-side strings render in `America/New_York` for the operator. See `numerical-rigor.md` § Timestamp rigor.
- **Position posture** — Current Risk (PR9) filters zero-qty entries out before deciding `Flat / Long / Short / Mixed`. A residual stale entry can't flip the posture silently.
- **Daily cap** — the count is read verbatim from `readiness.gates` where `name === 'orders_cap'`. When the engine has not emitted a typed cap, the card says so honestly ("Daily cap status not reported by the engine") — no fabricated counter.
- **Incidents** — backend `IncidentCategory` enum + `parse_incidents` classifier (#565 PR 1, #566) is the single source of truth. The frontend `INCIDENT_COPY` map (PR6) is operator-language presentation only; unknown categories degrade to "Unknown error — see raw traceback."
- **Readiness** — gate-by-gate detail strings are surfaced verbatim. The Can-It-Trade card leads with the verdict and proportional count; the floating Pre-Trade Checklist renders the affordance-per-gate UX.
- **Page-wide collapse rule** (added 2026-06-17) — cards collapse to a one-line summary in steady state and **auto-expand when the operator needs to act**. The expansion trigger is *always a server-authored verdict* (readiness verdict, posture computed from server-side filtered positions, prior-run exit class) — never a frontend-derived heuristic. This is the same single-source-of-truth principle ADR 0011 applies to the broker safety verdict, generalized to the page's reactive layout. Implications: (a) a new card MUST identify its server-authored expand trigger before being added; (b) two clients viewing the same status payload MUST resolve to the same expanded/collapsed configuration; (c) "feels off, let me expand it ambient-style" is not a valid trigger — if a card has no verdict-driven expand condition, it doesn't belong in the page flow.
- **Disabled-action tooltip rule** (added 2026-06-17) — every disabled banner action renders an operator-language reason from a `DISABLED_REASON_COPY` map keyed by the structured reason code returned by the API (e.g. `broker_safety_not_paper_only` per ADR 0011, `unresolved_uncertain_intent` per ADR 0008, `reconciliation_not_clean` per ADR 0010 § Decision 3). The tooltip is verdict-level only; per-gate detail lives in the Can-It-Trade card. Unknown reason codes degrade to "*This action can't be taken right now — see Can-It-Trade card.*" The tooltip never invents a reason — same pattern as `INCIDENT_COPY`.
- **Banner operator-action contract** (added 2026-06-17) — the banner action toolbar contains exactly **Resume, Pause, Flatten-and-pause**, plus a kebab for **Stop**. Mark-poisoned lives in the Detective Diagnostics tab header (not the banner). All five affordances and their primitives are governed by ADR 0010 — the cosmetic Stop-into-kebab decision is the only deviation and does not revise the ADR (it is a UI layout call, not a contract change).

## What is deferred

The 13-PR sequence intentionally defers the following beyond #565:

- **Sticky bar destructive kebab** (User Stories #31 – #40) — FLATTEN, MARK_POISONED, Reset Paper Account dialogs in the sticky bar. The existing Advanced Actions card continues to drive those flows. Deferring keeps destructive control flow in one place during the operator-first refactor.
- **Sticky bar Restart & Update button** (User Stories #39 – #40) — conditional render based on platform-code freshness, which doesn't have a backend contract yet.
- **Per-gate affordances on the Readiness card** (User Stories #11 – #13) — button / nav-link / read-only note per gate. The existing Pre-Trade Checklist already renders these via the parent's `fixAction()` taxonomy; full extraction to the new card lands after the sticky bar takes ownership of Start / Pause / Stop.
- **Next-evaluation timestamp on the signal strip** (User Story #24) — needs a new backend contract field; today the strip is a no-new-math addition.
- **Per-resource transport staleness outside child runtime** (User Stories #41 – #44) — PRD #619-B now supplies child `runtime_freshness` from `engine_runtime.json`, including a `LAST-KNOWN` banner and backend-authored Resume / Flatten gating. PRD #619-C still owns typed data-plane↔daemon transport state (`RETRYING`, `UNREACHABLE`, auth/protocol errors) and browser poll-failure age.

## Post-merge cleanup (the actual #565 PR 13 sweep)

Once PRs 4 – 12 land, the following surfaces become reachable-but-redundant and should be removed in a follow-up sweep:

- The inline "Managed Positions" card in `broker-instances.component.html` is now redundant with the Current Risk card (PR9). Remove the inline section and the `brokerPositions()` helper.
- The inline "Latest Strategy Signal" card is now redundant with the Latest Signal strip (PR8). Remove the inline section.
- The static `Why It Stopped` heading was already removed by PR10's switch to `<app-last-session-card>`; double-check the inline last-exit-card block in the parent is gone end-to-end after PR10 merges.
- The legacy `bot-failures-table` folder was deleted by PR6; spot-check no stale imports or routes reference it after PR6 merges.

These are not done in this PR because PR 13 is independently branched from `master` and the redundancies only exist *after* the new components land. The sweep is mechanical and will be a small follow-up once the 12 cards are on `master`.

## IA revision 2026-06-17

A `grill-with-docs` session resolved a set of operator-feedback proposals against ADRs 0010, 0011 and this runbook. The locked outcome is reflected in the rewritten "What this page answers" and "Layout — top to bottom" sections above. The migration work it implies:

- **Delete the System Health panel.** Its three checks (broker connection, trading-engine state, bot intent) are now fully subsumed by (a) the fleet header's IBKR connection liveness pill, (b) the banner's process-state pill, and (c) the banner's intent pill. The deletion loses no information — but the banner's visual hierarchy must read all three pills at a glance for the substitution to be honest.
- **Promote operator actions into the banner.** `<app-broker-start-stop-card>` is removed in favor of a `p-toolbar` inside the sticky banner containing Resume, Pause, Flatten-and-pause, and a kebab containing Stop. Mark-poisoned moves into the Detective Diagnostics tab header. The "Jump to controls" affordance the sticky bar emits today is removed — the controls *are* the banner.
- **Fuse Strategy Rules + Sizing into a single `<app-configuration-card>`** (ranked 2). The merged card uses nested disclosures for sizing detail and portfolio audit; per-trade audit is embedded as a table. When expanded during a configure flow, the card header pins a Current Risk summary chip so the operator does not change rules while blind to held risk.
- **Promote Current Risk to rank 3**, collapsed to a one-line posture summary when **Flat AND no pending orders**.
- **Demote the Readiness card** (now Can-It-Trade) to rank 4 with verdict-coloured borders: green/READY-collapsed, amber/DEGRADED-expanded, red/BLOCKED-expanded, grey/UNKNOWN-expanded. Passive — never auto-pops the floating checklist.
- **Group chart + trades + audit + history into a tabbed Detective section** (rank 5). Activity tab contains chart + Latest Signal strip + Trades table; Diagnostics tab contains Recent Incidents + Last Session detail + Audit accordion. The `View run log` button moves into the Audit accordion header as a compact icon-button — the run_id is *not* displayed on the button face; it appears inside the modal that opens. The Audit accordion's white bottom-border is fixed to `var(--panel-border)`.
- **Move the Pre-Trade Checklist into a floating `p-dialog`** at `position="bottomright"` (rank 6). Default is a minimized chat-bubble FAB ("Checklist · N fail"); expanded panel shows failing gates first. Operator-triggered only — no auto-pop on BLOCKED. The banner readiness pill deep-links into both the Can-It-Trade card and the checklist modal.
- **Last Session full content moves into the Detective Diagnostics tab.** Only a small success/failure chip remains in the banner.

These changes are net-additive to the runbook's "five trader questions" framing — the framing is now six. The IA revision honours every contract in ADRs 0010 and 0011 unchanged; no ADR revisions came out of this grilling.

## PRD #607 cockpit revision (2026-06-21)

The Slices 1–8 contract below is amended with the following
**non-additive** changes; ``schema_version`` stays at ``1`` because the
revision lands before any external consumer ships against it.

- ``host_process.state`` enum is now ``RUNNING / STOPPING / EXITED /
  IDLE / WAITING_FOR_HOST / UNREACHABLE``.  ``IDLE`` means the host
  daemon is reachable but no subprocess is tracked for this instance.
  ``WAITING_FOR_HOST`` is derived: ``IDLE`` PLUS durable intent
  ``RUNNING`` — the operator has expressed intent and is waiting for
  the subprocess to be started outside the cockpit.  Distinct from
  ``STARTING`` (which the cockpit does NOT emit — it cannot start
  anything; ADR-0003 / ADR-0007).
- ``broker`` carries **two independent enums**:
  - ``safety_verdict``: ``PAPER_ONLY / UNSAFE / UNKNOWN`` (ADR-0011).
  - ``connection``: ``CONNECTED / DISCONNECTED / UNKNOWN`` (broker
    session liveness).
  They MUST be read separately.  A paper-only account whose IBKR
  session has dropped is ``PAPER_ONLY`` + ``DISCONNECTED``; composing
  them collapses two operator-relevant facts.
- ``trading_session`` block added:
  ``{ phase, permits_strategy_activity, next_transition_ms, timezone,
  as_of_ms }``.  ``phase`` is one of ``PRE / RTH / POST / CLOSED /
  UNKNOWN``.  The server owns boundaries (per-strategy session policy
  or RTH default); Angular only advances and formats the visible
  HH:MM:SS string from its local wall clock.  Hard-coding session
  hours in Angular is forbidden.

### Sticky-header behavior

The cockpit page renders a single ``<header class="cockpit-sticky">``
wrapping {bot tab-strip + sticky-control-bar + attention strip}.  The
wrapper owns ``position: sticky; top: 0``; inner children must not
redeclare sticky.  Fixed via a Playwright assertion that scrolls the
page and compares the wrapper's bounding rect before/after.

### Eight legacy surfaces removed in this revision

The legacy hero, ``panel-toolbar`` View Run Log row, ``system-health``
card, ``behavior-card``, ``strategy-state`` card,
``broker-card`` (Managed Positions), ``advanced-card``, and
``<app-broker-start-stop-card>`` reference are deleted from the
broker-instances template.  Their information is subsumed by the
sticky banner + host-process notice + verdict-bordered cards.

## PRD #607 / Slices 1–8 — `operator_surface` projection (2026-06-20)

The `/api/live-instances/{id}/status` response gained an `operator_surface`
field that is the single source of truth for operational verdicts,
risk posture, structured daily-cap usage, action-plan consumption,
broker safety verdict, prior-run classification, host-process state,
and per-action capability + reason codes.  The Frontend renders these
fields; it does NOT derive verdicts from raw status fields.

Shape (`schema_version: 1`):

```ts
operator_surface: {
  schema_version: 1
  host_process:     { state, notice, copyable_command }
  prior_run:        { classification }
  broker:           { safety_verdict }
  configuration:    { verdict, reason_codes }
  current_risk:     { posture, pending_order_count, verdict, unrealized_pnl }
  daily_order_cap:  { used, limit }
  action_plan:      { consumption, anomaly_verdict }
  actions:          { resume, pause, flatten_and_pause, mark_poisoned }
  runtime_freshness:{
    posture_demoted,
    stale_reason_codes,
    command_loop,
    broker,
    bar_loop,
    control_plane
  }
}
```

### Four authority layers

1. **Server domain eligibility + verdicts** — Python authors every
   verdict in `operator_surface`.  Mutation endpoints
   (`/flatten-and-pause`, `/commands{MARK_POISONED}`) re-evaluate
   eligibility via the same Python capability evaluator and reject
   with `409 Conflict` + `disabled_reason_code` when denied.  A stale
   status snapshot must not be exploitable. Runtime posture demotion
   blocks Resume and Flatten-and-pause; durable Pause / Stop remain
   available as fail-safe intents.
2. **Angular transient request state** — `busyVerb` /
   `requestInFlight` lives only in Angular.  Keycaps disable when
   `requestInFlight === true` regardless of server capability so a
   double-click cannot fire two requests.  The reason-code vocabulary
   deliberately excludes `BUSY_VERB_IN_FLIGHT`.
3. **Angular presentation + operator-controlled expansion** —
   verdict-glow class application, collapse animation, atmospheric
   polish, and the single-boolean operator override on READY cards.
   On attention verdicts the collapse toggle is absent from the DOM
   (Option A).
4. **Host-process lifecycle is outside the cockpit's authority**
   (ADR-0003 + ADR-0007).  The host runner is operator-owned.  The
   cockpit writes durable intent (Resume / Pause are always available
   and gate the next host start) and actuates on bound runs (when
   `actions.<verb>.effect === 'LIVE_ACTUATION'`).  It does NOT expose
   Start / Stop / process-control affordances — the legacy
   `<app-broker-start-stop-card>` is the cautionary tale (PRD
   #607 Slice 8 superseded it with `<app-host-process-notice>`;
   the orphaned directory was deleted by the 2026-06-22 audit
   P3-007 after the route-table / import-graph proof of
   non-reference).  When the
   daemon is idle, the cockpit surfaces the server-authored
   `host_process.notice` and (only if server-authored) a copyable
   safe command.  REDEPLOY is a separate surface for creating a new
   run configuration; it is NOT a restart path.

### Reason-code vocabulary (closed, updated 2026-06-22)

The full closed set, source of truth on the server:

- `PythonDataService/app/services/operator_capability.py` →
  `REASON_CODES` — action-conflict-matrix codes
  (`MUTATION_UNRESOLVED_START/STOP/FLATTEN/RESUME`), the durable
  mutation transport code (`OUTCOME_UNKNOWN`), and the live-binding
  / live-effect codes (`NO_LIVE_BINDING`, `NO_OWNED_POSITIONS`,
  `ALREADY_POISONED`, `ALREADY_STOPPED`, `POSTURE_DEMOTED`).
- `PythonDataService/app/services/resume_guard_state.py` →
  `RESUME_REASON_CODES` — Resume / Pause / Stop intent-state codes
  (`ALREADY_RUNNING`, `ALREADY_PAUSED`, `STOPPED_REQUIRES_REDEPLOY`,
  `REDEPLOY_REQUIRED`), the broker safety identity gate
  (`BROKER_SAFETY_UNSAFE`, `BROKER_SAFETY_UNKNOWN`), the
  submission-capability gate
  (`SUBMISSION_CAPABILITY_BLOCKED`, `SUBMISSION_CAPABILITY_UNKNOWN`),
  the reconciliation-receipt gate (`RECONCILIATION_*`), and the
  uncertain-intent gate
  (`UNRESOLVED_UNCERTAIN_INTENT`, `UNCERTAIN_INTENT_STATE_UNKNOWN`).

Deliberately removed (pre-PRD #616 vocabulary): `SAFETY_BLOCK_HALT`,
`RECONCILE_NOT_WIRED`, `BUSY_VERB_IN_FLIGHT`, `NOT_RUNNING`.

**Frontend copy map.** The cockpit's operator-language lookup lives
at
`Frontend/src/app/components/broker/cockpit-v2/lib/disabled-reason-copy.ts`
(landed by the 2026-06-22 audit's P2-002 fix). Adding a new code is
a typed addition to the closed Python enum + an entry in the
TypeScript `OperatorReasonCode` union + an operator-language string
in `OPERATOR_REASON_COPY`. The Vitest parity test
`disabled-reason-copy.spec.ts` fails on any drift between the
cockpit map and the Python source-of-truth set. Unknown codes are
rendered as ``Unrecognized reason code: <code>`` so a gap is
visibly diagnosable rather than silent — the operator can still
read the raw token and search the runbook.

Two Frontend-only codes live alongside the server set and are
clearly prefixed `LOCAL_*` so they cannot pretend to be server
authority: `LOCAL_TRANSPORT_STALE` (control-plane transport is not
CONNECTED — the cockpit refuses local dispatch fail-closed) and
`LOCAL_REQUEST_IN_FLIGHT` (a previous request is still pending).

### Open shortcomings carried forward

- **`order_mode` field** — not yet declarative.  Cockpit does not
  surface it; a future ADR + multi-stack PRD adds it.
- **`action_plan.anomaly_verdict`** — server returns `READY` while no
  detector exists.  PRD #593 Slice 4 wires a real detector; the
  cockpit consumes the same field, no Frontend change.
- **`broker.safety_verdict === 'DEGRADED'`** — currently unreachable
  through the live readiness gate (pass/fail only).  Surfaces when a
  richer `BrokerConnectionState` channel lands on the wire; the
  router-side mapping helper is the only thing that grows.
- **`host_process.copyable_command`** — `null` in the first iteration.

## Quick visual audit before deploy

After merging PRs 4 – 12, walk the page top-to-bottom on a paper bot:

1. PAPER chip visible in the page utility row AND `SAFETY · PAPER_ONLY` indicator visible in the identity strip (both consume the same server-authored `operator_surface.broker.safety_verdict`; mismatch is structurally blocked by the 2026-06-22 audit's P1-001 fix)
2. Bot identity + state pill + readiness pill visible
3. Posture chip on Current Risk matches expected positions (Flat for a brand-new bot)
4. Last Session shows thin stub on a clean prior exit, full card with `Re-deploy (fresh run_id)` on a dirty one
5. Readiness card shows the calm strip when verdict is READY
6. Strategy Rules card renders the four primary rows; `Show advanced ▾` reveals the broker address + hydration mode + contract path
7. Latest Signal strip below the chart shows the engine's last decision
8. Recent Incidents renders operator-language copy (not raw `Error 1100`)

If any of those land wrong, the corresponding PR in the series is the place to look first.

## PRD #619-D mutation uncertainty + recovery (2026-06-22)

### Reconcile procedure (no cockpit button yet)

**There is no Reconcile button in the cockpit-v2 UI today** — the visible `RECONCILE · NOT WIRED` hazard banner is the honest statement of that gap. The 2026-06-22 audit found the cockpit's earlier tooltip copy directed operators to "Use Reconcile on the Audit tab," which would have sent them looking for a control that does not exist. The corrected tooltip points to this runbook section.

The reconcile endpoint is server-authored (`POST /api/live-instances/{strategy_instance_id}/reconcile-mutation`, defined in `PythonDataService/app/routers/live_instances.py`); the cockpit just hasn't wired a button. Until it does, the operator procedure is:

```bash
# Inspect the most recent mutation_attempt for the instance
curl -s http://localhost:8000/api/live-instances/<id>/status | jq '.operator_surface.actions'

# Call the Reconcile endpoint to classify the prior attempt
curl -s -X POST http://localhost:8000/api/live-instances/<id>/reconcile-mutation | jq
```

The response carries the `MutationAttempt.dispatch_state` advanced to one of the four terminals below (`EFFECT_CONFIRMED` / `EFFECT_NOT_OBSERVED` / `EVIDENCE_CONFLICT` / `NOT_PROVABLE`). When the attempt becomes `EFFECT_CONFIRMED`, the action-conflict matrix disengages and the cockpit's Resume / Stop / Flatten button re-enables on the next status poll.

Wiring this into the cockpit UI is a follow-up. The action-conflict-matrix tooltips were corrected in this audit; the button itself is the deferred work.

### Incident vocabulary

This section pins the operator-facing copy for every new reason / state code introduced by 619-D. The cockpit renders operator-language strings via the shared `disabled-reason-copy.ts` map (2026-06-22 audit P2-002); the runbook entry below carries the deep procedural detail.

#### `OUTCOME_UNKNOWN` (PRD #619-C5, durable in 619-D1)

**What it means.** A mutation request (Deploy / Start / Stop / Flatten / Resume / Pause) was sent to the host daemon, but the response did not arrive intact. The daemon may or may not have observed the request.

**What to do.** Do not blindly retry. Reconcile the attempt (see "Reconcile procedure" above). The endpoint inspects daemon state, the child's `engine_runtime.json`, and broker positions, then advances the attempt to one of:

- `EFFECT_CONFIRMED` — the intended effect is observable; the mutation did land.
- `EFFECT_NOT_OBSERVED` — no evidence of the effect; the mutation likely did not land, but Reconcile is **not** permission to retry — read the next-action guidance below.
- `EVIDENCE_CONFLICT` — facts contradict (e.g. process running but no daemon binding); investigate before acting.
- `NOT_PROVABLE` — insufficient evidence (daemon unreachable); retry Reconcile after the daemon recovers.

#### `MUTATION_UNRESOLVED_STOP` / `MUTATION_UNRESOLVED_FLATTEN` / `MUTATION_UNRESOLVED_RESUME` (PRD #619-D2)

**What it means.** The previous mutation of the named action type is in an unresolved state (not yet `EFFECT_CONFIRMED`). The current action is blocked because retrying without confirmation could double-act on the same intent (re-stop a process that just stopped, re-flatten positions that were already closed, etc.).

`MUTATION_UNRESOLVED_START` is reserved in the vocabulary but does not block any operator-surface action in v1 — Start mutations are gated at the router level (`start_run` / Redeploy).

**What to do.** Reconcile the attempt (see "Reconcile procedure" above). The matrix disengages when the prior attempt reaches `EFFECT_CONFIRMED`. If Reconcile classifies as `EFFECT_NOT_OBSERVED`, you must decide whether to re-issue the mutation; the system does **not** auto-retry, and the matrix stays engaged until the next action explicitly advances state.

#### `EFFECT_CONFIRMED` / `EFFECT_NOT_OBSERVED` / `EVIDENCE_CONFLICT` / `NOT_PROVABLE` (PRD #619-D3)

These are Reconcile outcomes, not standalone reason codes. They appear on the Reconcile response and on the durable `MutationAttempt.dispatch_state`. The Reconcile button surfaces them; the cockpit's action-conflict matrix reads them.

#### `ACCOUNTS_MATCH` / `ACCOUNTS_DIVERGE` / `CHILD_OBSERVATION_MISSING` / `DATA_PLANE_OBSERVATION_MISSING` / `DATA_PLANE_DISCONNECTED` / `CONFIGURED_MODES_DIVERGE` (PRD #619-D4)

**Where shown.** Backend-authored on `OperatorSurface.broker_observation_consistency.reason_codes`. The cockpit renders the divergence card on `CONFLICTING` prominently — but the card never overwrites the child's authoritative posture on the broker hero.

| Code | What it means | What to do |
|---|---|---|
| `ACCOUNTS_MATCH` | Both observations agree on the same account. Card colour-coded as informational. | Nothing — this is the healthy state. |
| `ACCOUNTS_DIVERGE` | Child and data plane report different connected accounts. | Check the host runner's IBKR client config and the data plane's `IBKR_HOST` / `IBKR_PORT` settings. One is connected to the wrong account. |
| `CHILD_OBSERVATION_MISSING` | The child has not yet published an `engine_runtime.json` for the bound instance. | Wait one to two seconds (steady-state publisher cadence is 1Hz). If it persists, the child may be paused / failed; check the host-process card. |
| `DATA_PLANE_OBSERVATION_MISSING` | The data plane singleton is unavailable (broker disabled, lifespan tearing down) or reports an empty account string. | If `IBKR_BROKER_ENABLED=false` in the data plane, this is expected. Otherwise check the data plane broker singleton's connection state. |
| `DATA_PLANE_DISCONNECTED` | The data plane singleton is configured but not currently connected to IBKR. | Restore the data plane's IBKR session before treating the divergence verdict as authoritative. |
| `CONFIGURED_MODES_DIVERGE` | Child runs in `paper` mode but data plane is `live` (or vice versa). Comparison is suppressed (`NOT_COMPARABLE`) — comparing accounts would mislead. | Check the deployment's intended mode; one of the two layers is mis-configured. |

#### `ORPHANED_CONTROL_PLANE` / `EXITED_UNMANAGED` (PRD #619-B, runbook updated 2026-06-22)

**`ORPHANED_CONTROL_PLANE`.** A child process is running with a sidecar that names an older daemon `boot_id` than the live daemon's. The daemon refuses to issue new Start commands for the instance until the orphan is resolved.

**`EXITED_UNMANAGED`.** A sidecar refers to a process the daemon cannot verify is alive (no live PID + sidecar is stale).

**What to do.** For `ORPHANED_CONTROL_PLANE`: verify the prior process is no longer trading (broker positions, last bar timestamp), then delete the sidecar to allow a new Start. For `EXITED_UNMANAGED`: the prior process has died; delete the sidecar to re-deploy.
