# Bot control page — the Verdict Card (trader-first simplification)

**Date:** 2026-07-08
**Status:** Approved design, pre-implementation
**Route:** `/broker/bots/:id` (full replacement of the current page layout)
**Semantic ground truth:** PRD #974 rev 3.1 (daily bot lifecycle) + ADR-0026 (closed condition/cure enums)

## Problem

The current page presents ~40 interactive elements and ~30 blocks of prose for a bot whose
entire situation on the day of this audit was: off, account frozen (`flatten_timed_out`),
flat, session closed. The trader's decision space was one action; the page offered a 9-node
lifecycle chart (each node with Select/Receipts/Open), 9 readiness signal cards, 7 attention
prose articles, an event stream, deploy settings, a broker tail, and an incidents panel.
P&L appears nowhere prominent. The one needed action (clear the freeze) existed only as
instructional prose. A trader cannot operate this page without documentation; that is the
defect.

## Decisions locked during brainstorm (2026-07-08, user-approved)

1. **Clean slate.** Layout designed from zero. Prior layout PRDs are superseded where they
   conflict (see "Reconciliation with prior PRDs").
2. **State-adaptive page.** The page morphs by bot state; it shows only what is
   decision-relevant in that state.
3. **One permanent vital.** The only always-visible facts: the state word + one plain-language
   reason line. Everything else is state-conditional.
4. **Scoped "why?" drill-ins.** No global diagnostics wall. Evidence appears on demand,
   scoped to the claim being questioned.
5. **One big verb per state.** The state renders exactly one primary button that performs the
   state's exit. No disabled-button graveyards: an action that cannot run is not rendered;
   its absence is explained by the state itself.
6. **Architecture: the Verdict Card** (chosen over "Cockpit Strip + Stage" and
   "Trading Day Spine"). Centered single-column card; the state is the page. When the bot is
   healthy the verdict shrinks to a calm strip and the chart takes the page; when anything
   needs the trader, the verdict grows and the chart shrinks.

## State map (all mockups user-approved)

Display word = presence phase (#974), with one override: any open condition presents as
**SICK** (display precedence Sick > Ready > Off roster, per ADR-0025 pattern). "Ready" is
expressed by the verb, not a second word.

| State | Layout | Verb (one) | Also on screen |
|---|---|---|---|
| **SICK** (derived: ≥1 open condition) | Full card, red | Dominant condition's cure verb (closed cure set, ADR-0026), e.g. `Resolve exposure` → dialog offering flatten-vs-accept | Reason = dominant condition in plain language + `why?`; vitals row (Position, Today P&L, Session); thumbnail chart with incident flag |
| **ON DUTY** (healthy) | Verdict collapses to strip; chart owns page | `End day now` (calm, secondary prominence) | Vitals: Position, Today P&L, Trades, Orders left; 1-min chart with trade markers + P&L overlay + LIVE pill |
| **OFF DUTY** (offer valid) | Full card, green verb | `Start today's session` (sub-line: starts flat · offer good until <effective stop>) | Yesterday P&L, sizing preset, order cap, `change settings →`; 10-session attendance strip |
| **OFF DUTY** (no offer / off roster) | Full card, grey | None — reason states the next opportunity ("Roll call at next open", "Off roster — enable in ⋯"). Button Rule stays satisfied via the ambient exits in `⋯` (roster toggle, Retire); the primary exit for OFF DUTY is Start-when-offered | Same supporting facts, no verb |
| **CLOCKING OUT** (transient sub-phase) | Full card, amber, **no verb** | — | Live checklist (orders cancelled / flattened / receipt). Stall or failure lands OFF_DUTY with an open condition → page presents SICK with its cure verb. No eternal spinners. |
| **RETIRED** (terminal) | Full card, muted, read-only | `Create replacement` only if none exists | Honest retirement reason + `why?`; lifetime P&L, session count, lineage link; history browsable read-only |

Permanent chrome across all states: identity line (`<bot_id> · <symbol> · PAPER|LIVE`),
state word, reason line, and the `⋯` overflow.

## Deletion map (current page → fate)

Nothing silently lost: each element is deleted, absorbed into a state, or demoted to a drawer.

| Today's element | Fate |
|---|---|
| Posture chips (Broker proof / Submit / Exposure) | Absorbed: frozen-submit is the SICK state; exposure is an ON-DUTY vital; paper/live is one word in the identity line |
| "Bot Off" banner + Lifecycle controls + "Open runbook →" | Deleted; the state word does this job; runbook prose becomes drawer content |
| 9 readiness signal cards | Demoted into the why-drawer behind the state reason (failing/relevant ones first) |
| 7 attention-detail prose articles | Deleted; where a cure exists their remediation text becomes the verb's confirm-dialog copy, else drawer content |
| 9-node lifecycle chart (Select/Receipts/Open ×9) | Deleted from this page; its receipts survive scoped in why-drawers |
| Bot event stream panel | Off this page; the #928 pipeline remains a data source feeding drawers |
| "Current lifecycle focus" + Meaning pane | Deleted — the reason line is the meaning |
| Deploy-time settings pane (5 fields) | Compressed to OFF-DUTY settings line (`sizing · cap · change settings →`, which routes to redeploy) |
| Recent activity / Full audit trail tabs | Deleted; one "Full history" entry in ⋯ keeps audit access |
| Price & Trades chart + power tools | Survives as ON-DUTY centerpiece; range picker / replay / backfill shading (#968 scope) move behind chart-expand |
| Broker tail (8 category cards) | Demoted to the drawer behind broker-liveness claims + Full history |
| Incidents panel | Absorbed: SICK is the incident surface; history via Full history |

Net: ~40 interactive elements → 1 verb + 1 "why?" + 1 overflow (+ chart interactions on duty).

## Why-drawer contract

Exactly four trigger points, a closed set:

1. **State reason `why?`** → receipts behind the current verdict.
2. **Verb confirm dialog** → evidence for what the button is about to do (e.g. Resolve
   exposure shows the stranded-position receipt before flatten-vs-accept).
3. **Chart incident flags (⚑)** → that incident's receipt.
4. **`⋯` overflow** → Full history / audit trail escape hatch; ambient controls
   (roster toggle, Retire, change settings).

Content rules:

- Plain-language claim on top; receipts below rendered through the shared `receiptLabel`
  pipe (raw `reason_code` / `gate_id` / `source` never naked — existing hard rule); opaque
  audit tokens (ids, hashes, paths) preserved exactly.
- Timestamps via the shared timestamp display component (`local` for instants, `date-et`
  for date-anchored values), per temporal-rigor.
- **A drawer never introduces a new action** — at most it repeats the state's one verb.
- Empty drawer is honest per #974: "Not yet proven: <evidence> — [Prove now]". The word
  "Unknown" is banned.
- Multi-condition SICK: reason line shows the dominant condition (ADR-0025
  single-dominant-headline); the verb cures the dominant one; the drawer lists the others
  in cure order.

## Action model

- Verbs come from the closed cure/action set: `Start today's session`, `End day now`,
  condition cures per ADR-0026 (`resolve_exposure`, `clear_freeze`, `reconcile_now`,
  `prove_evidence`, `retire_replace`), `Create replacement`.
- Destructive or position-affecting verbs (flatten, accept-override, retire) get one
  confirm dialog carrying the relevant receipt; nothing else is interstitial.
- Ambient controls live only in `⋯`: roster toggle (non-retired), Retire (off duty),
  change settings (routes to redeploy), Full history.
- No CLI instructions ever rendered (Button Rule, #974).

## Data contract

The page consumes; it does not derive. Sources, with honest availability:

| Need | Source | Exists today? |
|---|---|---|
| Presence phase + reason + conditions + cure + offers | #974 single-writer evaluator projection (GET is pure, returns drift flag) | **No — lands with #974 slices 2–3.** This page is #974's visual layer and sequences behind those slices |
| Clean-exit checklist (clocking out) | `CleanExitReceipt` progress (#974 slice 3) | No — same dependency |
| Price chart + trade markers + LIVE pill | Existing chart snapshot endpoint + bar store + `is_streaming` (#968 chart PR) | Yes / in-flight |
| Position + Today P&L + Orders-left vitals | Broker tail projection (position/account P&L events) + order-cap counter | **Partial — needs a small vitals read model; must be resolved in planning, not invented in the component** |
| Attendance strip | Per-session receipts (#974 slice 4 day report) | No — strip ships greyed-empty until slice 4, honest-empty text |
| Receipts in drawers | Existing receipt/evidence surfaces (#928 pipeline, operator_surface) | Yes |

Interim behavior before #974 slices land: the page may ship behind the existing
`operator_surface` verdict with a reduced state set (Off / On / Sick-frozen), but no element
of the old layout returns. Timestamps `int64 ms UTC` end-to-end; session boundaries from the
canonical calendar (no hardcoded 09:30/16:00 — the "offer good until" line derives from
`effective_stop`).

## Edge and empty states

- **Never-deployed bot:** OFF DUTY variant, reason "Never run", verb routes to deploy form.
- **Backend unreachable:** the card itself goes honest-empty — state word `UNREACHABLE`,
  reason names the failing surface, no verb fabricated.
- **Evaluator drift flag set:** reason line gains "state may be stale — refreshing"; verbs
  disabled-by-omission until a fresh projection returns.
- **Session closed + on duty residue:** cannot occur per #974 (evaluator owns phase); if
  observed, treat as drift.

## Reconciliation with prior PRDs (explicit supersessions)

- **#951 (stream-primary):** layout superseded — no stream side panel, no receipts-in-nodes,
  no lower documentation section, no node chart. Its data plumbing (BotEventRow pipeline,
  verdict-now action mapping, receipt sourcing) is reused as drawer/verb feeds. The
  frontend row→action mapper seam survives as the verb/cure mapper.
- **#968 (workbench refinements):** chart scope (full history, backfill shading, replay,
  LIVE pill) survives behind chart-expand; broker-tail category summary survives inside the
  broker-liveness drawer; lifecycle-chart compaction and attention-dropdown fold-in are
  **mooted** (their subject is deleted).
- **#974 (daily lifecycle):** not superseded — this page is its UI. The Button Rule, closed
  vocabulary, sick-bay conditions, offers, and single-writer evaluator are consumed as-is.
- **#750 / #753:** already superseded by #951; now transitively superseded.

## Non-goals

- Fleet/roster home screen and evening day report (#974 slice 4 owns those).
- A separate diagnostics route (explicitly rejected — scoped drawers only).
- Mobile layout (the card happens to degrade well; not designed for here).
- Any change to backend enforcement, gates, or the evaluator itself.

## Platform rules that bind this build

Angular 21: standalone, OnPush, signals, `input()`/`output()`, native control flow,
`@for` with track; templates < 80 lines → child components per state card; AXE / WCAG AA
(state changes announced via live region; verb is a real button with accessible name;
drawers are dialogs with focus management). SCSS co-located. No `DatePipe` — shared
timestamp component only.

## Testing

- **Per-state DOM specs** (Angular Testing Library on the fake-services harness): for each
  state fixture assert the rendered state word, reason, the presence of exactly the one verb
  (and its absence where none is allowed), and the absence of deleted-surface artifacts.
- **Why-drawer contract specs:** drawer renders claim + piped receipt labels; never renders
  a second action; honest-empty text on no receipts.
- **Verb wiring specs:** each verb dispatches the existing command (start/end-day/cure) —
  mocked at DI level; confirm dialog appears for destructive verbs.
- **Backend contract tests** only if the vitals read model adds fields (httpx +
  ASGITransport per repo standard).
- Existing old-layout component specs are deleted with the layout, same PR.

## Implementation notes (2026-07-08 build)

Built and verified against the live page (`dep_val_smoke_002`). Type-check, project-scope
lint (clean for all touched files — remaining warnings are pre-existing `data-lab`/`lean-engine`
tech debt), and the full 270-test bot-control Vitest suite all pass, including 21 new/updated
specs (`verdict-card-model.spec.ts`, `verdict-card.component.spec.ts`, `why-drawer.component.spec.ts`,
rewritten `bot-control-page.component.spec.ts`, updated `bot-control-page.route-sidebar.spec.ts`).

Two refinements made during the build, both spec-consistent:

- **Crash recovery is a first-class verb.** When the start gate reports
  `CRASH_RECOVERY_REQUIRED`, the card surfaces "Record recovery evidence" as the one verb
  (opens the flat-account attestation dialog). This preserves a safety capability that the
  old notice/receipt banners carried — deleting those banners without this would have
  silently dropped it.
- **Retired suppresses remediations.** A retired bot shows only a lifecycle verb (Create
  replacement) or none — never a trader remediation as its primary verb — matching the
  read-only-record intent.
- **Self-targeting runbook verb opens the why-drawer.** Every sick bot's trader remediation
  is `open_runbook` → slug `watchdog-halt`, which the resolver maps to `/broker/bots/:id` —
  the page you're already on, so navigating is a no-op and the verb looked dead. Fixed: a new
  `evidence` verb kind (model) is chosen for any `open_runbook` remediation whose slug
  resolves to the instance's own page (`runbookOpensInstancePage()` in
  `operator-runbook-routes.ts`, covering `watchdog-halt`/`runtime-freshness`). The card
  labels it "View recovery details" and opens the why-drawer — the in-design home for
  this-bot recovery guidance — instead of navigating. Off-page runbooks (`broker-reconnect`,
  `broker-instance-operator-surface`) still navigate. When #974 wires a real account cure
  into `daily_lifecycle.primary_action`, that becomes the verb automatically (primary_action
  precedes remediation in `resolveVerb`).

Components: `lib/verdict-card-model.ts` (pure resolver), `verdict-card/verdict-card.component.*`
(presentation), `verdict-card/why-drawer.component.*` (scoped evidence dialog). The container
(`bot-control-page.component.ts`) kept all polling/dispatch/confirm plumbing and shrank from
875 to ~430 lines; its template dropped from 291 lines to ~55.

**Deferred (documented, not silently skipped):**

- The old child-component *files* (overview-tab, side-panel, node-inspector, tabs/*,
  overview-actions, trader-guidance-*, attention-dropdown, workbench-audit-panel) are no
  longer imported or rendered by the page but were left on disk with their passing specs, to
  keep this diff reviewable. Deleting them is a follow-up before the PR (their deletion is
  already in the deletion map above). The thermo-nuclear-code-quality-review gate must run
  before the first push and will flag them.
- The ON-DUTY body composes the existing self-loading `ActivityTabComponent` (chart + trades +
  broker tail) wholesale rather than a chart-only surface; the #968 "power tools behind
  chart-expand" refinement is not yet applied.
- The card template is ~110 lines (over the ~80 guideline); extracting a vitals or overflow
  sub-component is a candidate cleanup in the thermo pass.

## Open item for planning (not design)

- Exact shape/endpoint of the **vitals read model** (position, today P&L, orders-left):
  compose from existing broker-tail projections vs. a small dedicated endpoint. Decide in
  the implementation plan; the design only requires that the component consumes one typed
  surface and never sums broker events client-side.
