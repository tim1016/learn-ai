# PRD — Bot Control Panel: stream-primary overhaul

Event stream in the side panel with inline actions · node-scoped receipts inside the chart nodes · lower documentation section.

- **Tracker:** issue #951 (`ready-for-agent`).
- **Surface:** `broker/bots/:id` (bot control page), the Fresh run / deploy form, and Recent Incidents.
- **Builds on:** PRD #928 / ADR-0024 (Bot event stream — remains the authority for the stream's data pipeline, contracts unchanged), PR #950 (fresh-run-only surfacing), PRD #718 (Bot Lifecycle Workbench), the shipped Bot Control Dashboard (issue #750).
- **Supersedes:** the layout prescriptions of the Bot Control Dashboard redesign and of `bot-control-inspector-receipts-prd.md` (#753/#754). Their non-layout facts and verified sourcing decisions carry forward; see the banner in the inspector-receipts PRD.
- **Evidence:** `docs/audits/bot-control-panel-redeploy-observations.md` (2026-07-07 redeploy session, four bots).
- **Data plane:** Python REST/SSE via the existing live-runs service layer. No GraphQL.
- **Honesty rules (binding):** ADR-0013 (judgment vs evidence), ADR-0014 (backend-authored narratives), ADR-0024 (one source, two projections; surface disposal — replace, don't add), ADR-0001 (files canonical).
- **Implementation-snapshot DoD:** every PR that implements a slice of this PRD updates `docs/bot-lifecycle-account-owner-authority.md` in the same PR.
- **Status:** ready-for-agent.

---

## Problem Statement

The bot control panel buries its only live, trustworthy surface — the Bot event stream — inside a workbench tab, while the page's prime real estate goes to a large lifecycle flow-chart and side panels that present reference material as if it were live actionable state. The operator pays for this daily:

- **Submit-time dead ends.** During the 2026-07-07 redeploy session, three bots in a row (`dep_val_smoke_002`, `DIagVal6`, `deiagAPPL6`) showed "Ready to deploy" in the Fresh run form, accepted a full action plan, and then failed at submit with "durably STOPPED. Resume the bot to clear the stop latch." The durable STOPPED latch was knowable before submit — the form just never asked.
- **False readiness.** The deploy form reported "Ready to deploy" while the action plan's ON ENTER / ON EXIT legs were still empty. Nothing validates leg presence today, frontend or backend.
- **Invisible safety halts.** `Valgate-Jul6` accepted a start, died within seconds, and landed on Recovery lane · Poisoned with Prior Halt Trigger = Cold Start Divergence — yet Recent Incidents said "No recent incidents." A poisoned safety halt is invisible exactly where incidents are supposed to live, because boot-time reconciliation refusal never mints an OperatorIncident.
- **Scattered actionability.** Recovery guidance is split across an attention dropdown, a node inspector, disabled-button tooltips, and a guidance timeline. When a bot is blocked, the operator hunts across surfaces to find which one holds the action.
- **Clutter.** The page does not fit the view; the flow-chart dominates; the same global guidance blocks repeat regardless of context. One confirmed copy defect: the blocked-deploy alert renders "Deploy — blockeddeiagAPPL6…" with the space between the category and the bot-scoped detail missing.

## Solution

Reorganize the page around a strict division of labor — **nodes show values, the stream shows actions, the lower section explains**:

1. The **Bot event stream** (already built: spine events, gate-walk drill-in, terminal errors, SSE + backfill) is promoted out of the activity tab into the **side panel**, always visible next to the flow-chart. It is the live, actionable surface of the page.
2. Stream rows gain **inline actions**: when a row's condition has an operator action available right now (durable STOPPED latch → Resume, poison/safety halt → recovery path, blocked Deploy & start → Deploy only), the action renders on the row itself — live when the current verdict allows it, greyed with the backend's reason when it does not.
3. **Node-scoped receipts render inside the flow-chart nodes.** The node is the inspector now: compact value at rest, full receipts on expand. The separate node-inspector pane is deleted (surface disposal: replace, don't add). The chart itself becomes visibly thinner and more compact.
4. The displaced non-node-scoped side-panel content (trader guidance proof lines, change-for-next-run, advanced evidence, audit trail) moves to a **lower documentation section** in normal page flow — plain scrolled reference material, no tabs, no overlays, no CTAs, no urgency styling.
5. The deploy form **pre-detects the durable STOPPED latch** and empty action-plan legs before submit, and a **safety-halt → incident bridge** makes poisoned halts (including Cold Start Divergence) first-class Recent Incidents with forensic detail.

This supersedes the layout portions of the Bot Control Dashboard PRD and the inspector-receipts PRD, and amends ADR-0024's surface hierarchy. The event-stream data contract (BotEventRow, GateStep, TerminalError, identity ladder, classifier seam) is unchanged and remains authoritative.

## User Stories

1. As a bot operator, I want the Bot event stream visible in the side panel at all times, so that the live narrative of what my bot is doing is never hidden behind a tab.
2. As a bot operator, I want the lifecycle flow-chart to be thinner and more compact, so that the chart, the stream, and the page controls all fit in one view without scrolling the page shell.
3. As a bot operator, I want a stream row whose condition is resolvable right now to carry the resolving action inline (e.g., a durable-STOPPED block row carries Resume), so that I act where I read instead of hunting for the right button elsewhere.
4. As a bot operator, I want an inline row action that is currently unavailable to render greyed with the backend's exact reason, so that I know why I cannot act and what would unblock it.
5. As a bot operator, I want a poison/safety-halt row to offer the recovery path (Fresh run) inline, so that post-run recovery has one obvious next step.
6. As a bot operator, I want destructive or irreversible actions launched from a stream row (Mark poisoned, Flatten and pause) to keep their typed confirmation flow, so that inline convenience never weakens the safety model.
7. As a bot operator, I want a resolved condition to appear in the stream as its resolving event, so that the history shows both the problem and the fix in one narrative.
8. As a bot operator, I want each lifecycle node to display its own receipt values inside the node, so that the node's shape and its numbers are one thing and I never cross-reference a separate pane.
9. As a bot operator, I want to expand a node in place to see its full receipts (headline, detail, source, evidence time), so that drill-down does not navigate me away or open an overlay.
10. As a bot operator, I want nodes with no emitted events to say "not emitted yet" honestly inside the node, so that empty never masquerades as healthy or borrows global data.
11. As a bot operator, I want the trader guidance, proof lines, change-for-next-run, and advanced evidence content to live in a lower documentation section in normal page flow, so that reference material reads as reference material and never competes with live surfaces for urgency.
12. As a bot operator, I want the documentation section to carry no CTAs and no live-state claims, so that when I see a button anywhere on this page I know it is real and current.
13. As a bot operator, I want the Fresh run form to detect the durable STOPPED latch the moment I check "Start trading immediately," so that I learn about the latch before filling in an action plan, not at submit.
14. As a bot operator, I want the form's primary CTA to switch to "Deploy only" (with copy explaining that Resume must clear the latch first) when the latch is present, so that the path I am offered is one that can actually succeed.
15. As a bot operator, I want the deploy readiness verdict to fail while ON ENTER / ON EXIT action-plan legs are empty, so that "Ready to deploy" is never a lie.
16. As a bot operator, I want a Cold Start Divergence (or any poisoned safety halt) to appear in Recent Incidents with its forensic facts — halt trigger, source flag, evidence time, audit artifact reference — so that a bot that died for safety reasons is impossible to miss.
17. As a bot operator, I want a bot that dies after an accepted start to explain why on the page, with a pointer to the exact audit artifact, so that "it just went to Bot Off" never happens silently again.
18. As a bot operator, I want the safety-halt incident to be deduplicated by its incident key, so that one halt is one visible story, not a repeated alarm.
19. As a bot operator, I want the blocked-deploy alert copy fixed ("Deploy — blocked deiagAPPL6…", with the space), so that error text reads as authored prose, not string concatenation.
20. As a bot operator, I want raw backend identifiers (reason codes, gate ids, halt triggers) rendered through the shared receipt-label pipe everywhere on the redesigned page, so that I read trader language while opaque audit tokens stay exact.
21. As a bot operator, I want every timestamp on the redesigned surfaces rendered through the shared timestamp display component, so that time display is consistent and date-anchored values never drift a day.
22. As a bot operator, I want the top action toolbar (Resume / Pause / Stop / Flatten / Fresh run / Mark poisoned) to remain available for commands that have no triggering event (pausing a healthy bot), so that the stream's inline actions are shortcuts, not the only door.
23. As a keyboard or screen-reader user, I want inline row actions and expandable nodes to be reachable and labeled (WCAG AA, AXE-clean), so that the redesign does not trade accessibility for density.
24. As a trader reviewing a bot after the fact, I want the stream's backfill to show past terminal rows with their actions greyed (verdict-now says the moment has passed), so that history is preserved without advertising stale affordances.
25. As a developer or agent maintaining the panel, I want the row→action mapping to be a closed, exhaustiveness-tested set, so that a new terminal code cannot silently ship without a decision about its inline action.
26. As a developer or agent maintaining the panel, I want the node-inspector pane, the trader-guidance timeline, and other superseded surfaces deleted (not hidden), so that the codebase matches the surface-disposal rule: one verdict surface, one stream, everything else a projection or gone.

## Implementation Decisions

- **Division of labor (locked):** nodes show values, the stream shows actions, the lower documentation section explains. No surface does another surface's job.
- **Stream promotion, not stream construction.** The existing Bot event stream component (SSE live tail + paginated backfill, row expansion to gate-walk and terminal-error forensics) moves from the activity tab into the persistent side panel. The BotEventRow / BotEventRaw / GateStep / TerminalError contracts are unchanged.
- **Inline actions are a frontend join to verdict-now.** A stream row carries only its condition identity (terminal-error code, gate id, event type). A small pure mapper — a closed affordance map from condition identity to operator command — is joined at render time against the operator surface's actions block (enabled / disabled_reason_code / gate_results) to decide whether the action renders live or greyed-with-reason. No BotEventRow contract change; the stream stays purely historical ("one source, two projections" per ADR-0024 — the stream never re-derives the verdict). Unmapped terminal codes render no action and fail the exhaustiveness snapshot test.
- **Inline actions invoke the same operator commands that exist today** (resume, pause, stop, flatten-and-pause, mark-poisoned, navigate-to-Fresh-run, deploy-only). No new backend command paths. Typed confirmation flows are reused for destructive commands.
- **Top toolbar survives as the verdict-now command surface.** Locked decision "actionability lives in the stream" is interpreted as: one mechanism (operator-surface verdict), two placements (toolbar for operator-initiated commands with no triggering event; stream rows for condition-scoped shortcuts). Both render from the same actions block; neither invents availability.
- **In-node receipts.** Each lifecycle node renders its node-scoped receipts inside the node: at rest, a compact primary value plus status; on expand (click), the node grows in place to show full receipts (headline, detail, source, gate id, evidence time). The node-inspector pane is deleted. The inspector-receipts PRD's north star (node + receipt = two views of one backend receipt) is realized by collapsing the two views into one; its verified sourcing facts remain binding — halt trigger/at/detail come from the instance last-exit record (only when the poison flag is present), never from the prior-run classification; nodes without event emitters render honest-empty.
- **Chart rendering: extend the existing custom HTML/CSS chart.** Nodes are already HTML elements and can host receipt content directly. The chart is restyled thinner/more compact. The unused flow-chart library dependency currently declared in the frontend package manifest is removed. No new dependency.
- **Lower documentation section.** A plain page-flow section (not tabs, not overlays) houses the displaced content: trader guidance proof lines, change-for-next-run, advanced evidence, and the audit trail. Styled as documentation: no buttons, no CTAs, no severity coloring. Raw codes go through the receipt-label pipe; backend-authored prose is rendered verbatim.
- **Surface disposal executed in the same change:** the node-inspector pane is deleted; the trader-guidance "recent activity" timeline is deleted (redundant with the promoted stream); the attention dropdown's remediation CTA is retired — critical conditions surface as stream terminal rows plus incidents, and its explanatory prose moves to the documentation section. The runtime banner (incident headline) is retained as a projection per ADR-0024 §9.
- **Deploy form pre-detection.** The Fresh run form reads the already-exposed verdict-now state (the durable-STOPPED latch surfaces today as a resume-gate reason code on the operator surface) when "Start trading immediately" is checked: Deploy & start is disabled and the primary CTA switches to Deploy only, with copy explaining that Resume clears the latch first. The backend submit-time gate is unchanged and remains authoritative.
- **Empty-legs readiness gate.** The deployment readiness verdict gains a backend check: an action plan with empty ON ENTER or ON EXIT legs yields a blocked-before-submit verdict with a dedicated reason code. The frontend's blocked-reason computation mirrors it. Backend owns the verdict; the frontend never claims readiness the backend would refuse.
- **Safety-halt → incident bridge.** A poisoned safety halt — including Cold Start Divergence minted at boot-time reconciliation refusal, which today writes only the poison flag — mints an OperatorIncident through the existing terminal-incident path, deduplicated by incident key (instance + run + halt trigger), carrying forensic facts: halt trigger, source flag, evidence time (int64 ms UTC), and a reference to the audit artifact. The Recent Incidents surface then shows it with no frontend special-casing.
- **Copy defect.** The blocked-operation alert's title/detail join is fixed so category and bot-scoped detail are separated by a space; error copy follows the repo's error-authoring guidance (required core + remediation only when the system truly knows the fix).
- **ADR-0024 is amended in place** (inline-actions decision; §9 disposal table updated for the new side-panel placement, the deleted inspector pane, and the deleted guidance timeline). No new parallel ADR. The layout portions of the Bot Control Dashboard PRD and the inspector-receipts PRD are marked superseded by this PRD.
- **Definition of done for implementing PRs:** the bot-lifecycle account-owner authority document is updated in the same PR, per the existing rule.
- All temporal values remain int64 ms UTC end-to-end; display only through the shared timestamp component.

## Testing Decisions

- A good test asserts external behavior — endpoint response contracts and rendered operator-visible output — never internal signals, private state, or implementation order.
- **Backend contract seam (primary emphasis).** Async HTTP endpoint tests against the FastAPI app (the repo's existing async-client + ASGI-transport pattern, as used by the operator-surface, bot-events, and incidents router tests):
  - Deployment readiness verdict returns blocked-before-submit with the new reason code when ON ENTER / ON EXIT legs are empty, and clears when legs are present (regression test written failing-first — this is the observed defect).
  - Operator surface exposes the durable-STOPPED latch reason code pre-submit (regression pin on existing behavior the deploy form now depends on).
  - Boot-time poisoned safety halt (Cold Start Divergence path) mints exactly one deduplicated incident carrying halt trigger, evidence time, and artifact reference, and the incidents endpoint returns it (failing-first — this is the observed "No recent incidents" defect).
  - Bot-events rows carry the condition-identity fields (terminal code, gate id, event type) the frontend join requires — contract pin, no new fields expected.
- **Unit seam (new, small).** The row-condition → action affordance mapper: pure function, exhaustiveness snapshot over the closed terminal-code and event-type sets (mirrors ADR-0024's classifier exhaustiveness pattern); unmapped members fail the build, not the operator.
- **Frontend rendered-DOM seam (lighter).** Component specs on the existing DI-level fake-service harness for the bot-control page, asserting what the operator sees: stream renders in the side panel; a terminal row shows its action enabled or greyed-with-reason according to the faked verdict; a node renders its receipt values and expands in place; honest-empty node copy; deploy form disables Deploy & start and switches the CTA when the faked latch is present; the documentation section contains no interactive controls. Prior art: the existing bot-control page and child-component specs with the fake live-runs service provided at DI.
- Every defect observed in the 2026-07-07 audit ships with a regression test that fails before the fix (empty-legs readiness, missing incident, copy join).

## Out of Scope

- Building an authoritative per-bot PnL source (Monitor-node behavior unchanged: trade-activity summary plus explicit "P&L not yet available").
- New backend command paths or changes to the gates themselves (gates are the safety model and untouchable; only duplicate visualizations are disposed).
- Out-of-band push / notification delivery for stream events (explicitly deferred by the bot-event-stream PRD pending a delivery-channel decision).
- Node-click cross-filtering of the stream (clicking a node expands the node; filtering the stream by gate id is a possible follow-up, not this PRD).
- Fleet list (/broker/bots) redesign beyond what PR #950 already shipped for fresh-run-only surfacing.
- Adopting a third-party flow-chart library (decided against; the unused declared one is removed).
- Backfilling incidents for runs that halted before the bridge ships.
- Live-money trading concerns.

## Further Notes

- Evidence base: the 2026-07-07 bot-control redeploy observations audit (four bots: `dep_val_smoke_002`, `DIagVal6`, `deiagAPPL6`, `Valgate-Jul6`; one prior success, `Bars-July-6`, proving the UI path can get a bot trading). The audit file is committed alongside this PRD.
- Builds directly on PR #950 (fresh-run-only state surfaced in table and detail actions) — reuse, don't redo.
- Relationship to existing plans: the bot-event-stream PRD (#928) remains the authority for the stream's data pipeline and its remaining slices (exhaustiveness CI gate, end-to-end round trip); this PRD changes where the stream lives and what its rows can do. The Bot Control Dashboard PRD (#750) and inspector-receipts PRD (#753/#754) are superseded in their layout prescriptions; their non-layout facts and decisions carry forward as noted above.
- The stream is the page's only live surface by design; if a future condition needs an operator affordance, the path is: give it a terminal/spine event with a condition identity, then map it — never add a new panel.
