# Bot Control Dashboard — Per-Bot Detail Page Redesign (PRD)

**Surface:** `broker/bots/:id` → `bot-control-page.component` (in-place reshape; no new parallel route).
**Builds on:** PRD #718 (`docs/bot-lifecycle-workbench-redesign.md`) and proposed ADR 0017. Honesty rules from ADR 0013 (judgment vs evidence), ADR 0014 (backend-rendered narratives), ADR 0016 (trader-authored activity) remain binding.
**Design source:** the "Bot Control Dashboard" mock (claude.ai design), reconciled with #718 in a grilling session on 2026-06-30.
**Data plane:** existing Python FastAPI REST via `LiveRunsService` (the `/api/live-instances/*` and `/api/lifecycle-projection/*` endpoints). **No GraphQL** is introduced for this surface.
**Implementation-snapshot DoD:** update the canonical `docs/bot-lifecycle-account-owner-authority.md` once shipped.
**Status:** ready-for-agent.

---

## Problem Statement

As an operator supervising a live (paper) bot, I open the bot's detail page to answer one question: **can this bot safely place or manage its next trade — and if not, what exact field or proof needs action?** The page already carries the right data, but it renders as a long **scrolling document**: posture facts, the control actions, trader guidance, the lifecycle chart, the node inspector, recent activity, and the audit trail are stacked vertically. To correlate "what is wrong" with "what I can do about it" I scroll up and down and hold state in my head. The three posture facts a trader reads first — **broker proof**, **submit capability**, **exposure** — plus the **broker connection** are not pinned where I can glance at them; guidance is a separate strip that competes with the toolbar; and the control actions, the evidence for the selected step, and the audit are spread down the page. I want a single-screen control surface where the decision-relevant facts and controls are always visible and the "why / how the system knows" detail is one glance or click away.

## Solution

Reshape the existing per-bot page (`bot-control-page.component` and its children) into a **fixed single-screen control dashboard** that never scrolls as a document. Regions, top to bottom:

1. **Header strip** — bot identity (monospace slug + ticker) on the left; three operator-facing posture pills (**Broker proof**, **Submit**, **Exposure**) rendered as human labels; a right-aligned **broker connection** pill (ticker + connection state — Connected / Disconnected / Unknown — piped from `broker.connection`). No last-contact age is shown: the operator-surface broker block exposes no such timestamp, and the UI must not invent one.
2. **Controls toolbar** — the existing three action buckets **Run** (Start / Resume / Pause), **Recover** (Flatten & pause / Stop / Fresh run), **Danger** (Mark poisoned); plus an **Attention** badge that opens a dropdown, and a dynamic **next-step CTA** whose label is the backend-authored remediation.
3. **Slim degraded banner** (dismissible, session-only) — surfaces the **control-plane** state (e.g. DEGRADED, last contact) when it is not healthy.
4. **Two-column body that fills the remaining height** — **left**: the existing branching **lifecycle chart** (nodes + edges + expandable subgraphs), restyled to the mock's compact-card aesthetic; **right**: the **node inspector** for the selected step (Meaning → Evidence rows → collapsible Technical diagnostics).
5. **Bottom bar** — two tabs, **Recent activity** and **Full audit trail**, each opening an overlay panel; the richer execution content (broker-activity timeline, orders, signal strip, trade chart) and provenance are preserved inside those overlays.

The page keeps the governing principle from #718: **decision → explanation → provenance** (top = facts that affect the next decision; inspector = *why*; overlays/diagnostics = *how the system knows*). The cross-cutting honesty rule is preserved: **no frontend-derived verdicts or chips** — the backend authors state and reason prose; Angular renders it verbatim, with concept tooltips keyed on stable ids as the only frontend-authored copy.

## User Stories

1. As an operator, I want the bot's identity (strategy slug + ticker) pinned in the header, so that I always know which bot I am acting on.
2. As an operator, I want the header identity, ticker symbols, run ids, timestamps, hashes, and paths rendered in monospace, so that true identifiers are visually distinct from prose.
3. As a trader, I want a **Broker proof** pill in the header showing a human label (e.g. "Paper Only"), so that I can confirm at a glance whether trading against this account is allowed.
4. As a trader, I want a **Submit** pill showing whether the bot can submit an order (e.g. "Cannot submit"), so that I know if the order path is open before I expect fills.
5. As a trader, I want an **Exposure** pill (e.g. "Flat / Long / Short"), so that I know the bot's current market position without opening the orders table.
6. As an operator, I want a **broker connection** pill (ticker + connection state from `broker.connection`: Connected / Disconnected / Unknown), so that I can tell whether the broker session is up independently of the safety verdict. (No last-contact age — the operator-surface broker block does not expose one.)
7. As an operator, I want posture pills to read **"Unknown" (muted)** when the backend does not assert a value, so that the UI never infers a posture I cannot trust.
8. As an operator, I want the **Run** controls (Start, Resume, Pause) grouped together, so that routine lifecycle control is one scannable block.
9. As an operator, I want the **Recover** controls (Flatten & pause, Stop, Fresh run) grouped together, so that recovery actions are distinct from routine run control.
10. As an operator, I want the **Danger** control (Mark poisoned) visually separated and styled as destructive, so that I do not confuse it with routine actions.
11. As an operator, I want each control's **enabled/disabled** state to come from the backend capability, so that a control I cannot use is visibly closed with a reason.
12. As an operator, I want a disabled control to show its **backend reason** (headline + detail + reason code) on hover, so that I understand why it is closed.
13. As an operator, I want clicking a disabled control to **select its target lifecycle node**, so that I can see the upstream gate that is blocking it.
14. As an operator, I want an in-flight action to show a **busy/spinner** state on its control, so that I do not double-fire.
15. As an operator, I want an **Attention** badge with a count, so that I can see how many things need my attention without reading the whole page.
16. As an operator, I want the Attention dropdown to open a list of **headline + detail** items, so that I can triage what needs action.
17. As an operator, I want the Attention dropdown header to also carry the **situation headline, the "Why" explanation, and the risk line**, so that the guidance that used to live in a separate strip is consolidated in one place.
18. As an operator, I want each Attention item's dot color to reflect its **severity** (info / warning / critical), so that critical items stand out.
19. As an operator, I want a **next-step CTA** whose label is the backend-authored remediation (e.g. "Reconcile now", "Open runbook", "Redeploy"), so that the single most important next action is always one click away and never a hard-coded guess.
20. As an operator, I want a **slim degraded banner** when the control plane is unhealthy, showing its state and backend-authored notice with a link to broker status, so that I know actions may apply to durable desired state only.
21. As an operator, I want to **dismiss** the degraded banner for the session, but have it **re-appear** on a state change or reload, so that I can clear it temporarily without permanently hiding a safety-relevant notice.
22. As an operator, I want the **lifecycle chart** to remain a branching node+edge graph with expandable subgraphs, so that I keep the full lifecycle structure (per PRD #718 "keep chart"), not a flattened list.
23. As an operator, I want the lifecycle chart restyled to compact cards with lane chips, status chips, technical labels, and a "Blocking step" marker, so that it matches the dashboard aesthetic while carrying the same data.
24. As an operator, I want node colors to carry **global lifecycle status** (passed / active / blocked / poison / freeze / unknown / inactive), so that I can read overall health from the chart.
25. As an operator, I want the **currently-focused node** (backend `primary_node_id`) selected by default, so that the inspector opens on the step that matters now.
26. As an operator, I want to **click any node** to load it into the inspector, so that I can investigate any step on demand.
27. As an operator, I want the inspector to show the selected step's **label, lane, status, and technical label**, so that I know exactly which step I am reading.
28. As an operator, I want the inspector's **Meaning** line in backend-authored prose, so that I understand what the step means without decoding raw codes.
29. As an operator, I want the inspector's **Evidence rows** (label + tone-colored message), so that I can read the read-only proofs behind the step's status.
30. As an operator, I want a collapsible **Technical diagnostics** section with raw source / gate id / reason code / timestamps, so that the advanced provenance is available but not in my way by default.
31. As an operator, I want the **deploy** node's inspector to show "Change for next run" settings with a "Change via redeploy" affordance, so that I can adjust the next run without mutating the running bot.
32. As an operator, I want a **Recent activity** overlay from the bottom bar, so that I can review the latest lifecycle/broker events without leaving the page.
33. As an operator, I want the Recent-activity overlay to preserve the richer execution content (broker-activity timeline, orders today, latest signal, trade chart) in an internally-scrolling region, so that nothing I rely on today is lost in the redesign.
34. As an operator, I want a **Full audit trail** overlay carrying lifecycle audit events plus provenance and runtime configuration, so that I can audit what happened and how the run was created.
35. As an operator, I want each activity/audit row to show **time · tag · text** with the tag color-coded by tone, so that I can scan the log quickly.
36. As an operator, I want **Mark poisoned** to require a confirmation step, so that I cannot mark a run dead by a single misclick.
37. As an operator, I want **Flatten & pause** to require a confirmation step, so that I cannot close positions by a single misclick.
38. As an operator, I want **Stop, Pause, Resume, and Fresh run** to remain direct one-click actions, so that routine control keeps the mock's direct feel.
39. As an operator, I want **Fresh run** to inherit a confirmation **only if** it would implicitly poison/abandon an active run or cancel pending broker state, so that confirmations track real destructiveness rather than blanket friction.
40. As an operator on a wide desktop (≥1280px), I want the fixed single-screen two-column layout, so that I get the glanceable control surface the design intends.
41. As an operator on a narrower viewport (<1280px), I want the columns to stack to one column and the page to scroll, with no horizontal clipping, so that the page degrades gracefully instead of cutting off content.
42. As an operator, I want each pane (chart, inspector, overlay) to scroll **internally** only as an overflow safety valve, so that the page itself stays put while a busy pane absorbs its own overflow.
43. As an operator, I want the whole surface to keep polling live status (~4s) and stream broker activity, so that what I see stays current without manual refresh.
44. As a keyboard / assistive-tech user, I want the toolbar, Attention dropdown, confirmations, and overlays to be fully operable and AXE-clean (WCAG AA), so that the control surface is accessible.

## Implementation Decisions

- **In-place reshape, not a new component.** Modify `bot-control-page.component` and its existing children (overview tab, overview actions, node inspector, node receipts pane, audit panel, and the trader-guidance surface). The data wiring through `LiveRunsService` is reused as-is; no route changes, no GraphQL.
- **Fixed single-screen shell.** The page root becomes a flex column: header (~48px) · controls toolbar (~44px) · degraded banner (~30px, conditional) · two-column body (fills remaining height) · bottom bar (~40px). The document does not scroll; panes scroll internally only on overflow. Use responsive **min-heights**, not a hard 1440×900 lock.
- **Responsive floor.** Two-column at **≥1280px**; below that, stack to one column and permit **page scroll** (the only sanctioned page scroll). No horizontal clipping at any width.
- **Color = house tokens.** Restyle using the existing `_tokens.scss` variables; the mock's palette maps onto house tokens (accent → `--accent`, green → `--bull`, red → `--bear`, amber → `--warn`, backgrounds → `--bg-canvas`/`--bg-surface`). No one-off mock hexes.
- **Left pane keeps the branching lifecycle chart.** The mock's linear rail-timeline is a depiction only; the branching node+edge graph with expandable subgraphs is retained per PRD #718 and only restyled. This delta from the mock is recorded here and in the authority doc.
- **Posture badges are backend-authored, human-labelled.** Display rule (encodes a decision):

  ```
  displayValue = backendLabel ?? receiptLabel.transform(rawCode)
  ```

  Broker proof ← broker `safety_verdict`; Submit ← `submit_readiness` (`label` / `can_submit`); Exposure ← `current_risk.posture`; connection pill ← broker `connection`. Raw enum codes (e.g. `PAPER_ONLY`, `FLAT`) are **never** rendered as monospace tokens in the header — they pass through the `receiptLabel` pipe to prose. Monospace is reserved for true identifiers (slug, ticker, run ids, timestamps, hashes, paths) and technical diagnostic rows. When the backend does not assert a value, the pill reads a muted **"Unknown"**; Angular never infers posture from broker safety, readonly flags, action effects, or host state.
- **Guidance relocates into the Attention affordance.** The standalone guidance strip is removed. The Attention dropdown carries: a header block (situation headline + "Why" explanation + risk line) followed by the attention items. The next-step CTA label is driven by the backend's primary remediation, not hard-coded.
- **Node selection is explanatory only, never an eligibility gate.** Eligibility comes solely from the backend action capability. Selecting or clicking a node updates the inspector and highlights the node; clicking a disabled action selects that action's target node and shows its reason + receipts.
- **Bottom bar = two overlays, richness preserved.** "Recent activity" hosts the reused activity content (broker-activity timeline + orders + signal + trade chart) in an internally-scrolling overlay; "Full audit trail" hosts lifecycle audit events + provenance + runtime config. Nothing currently shown on the page is dropped — it is reorganized.
- **Confirmations.** Mark poisoned and Flatten & pause open a confirmation step before firing. This confirm is a **frontend-only gate**: on confirm, Flatten & pause calls the atomic `/flatten-and-pause` service method, which takes **no** `confirm` flag. (The `confirm:true` requirement belongs to the *separate* `/emergency-flatten` endpoint, which this flow does not use — do not "fix" the frontend into that endpoint's shape.) Stop / Pause / Resume / Fresh run fire directly. Fresh run gains a confirmation only if implementation confirms it implicitly poisons/abandons the active run or cancels pending broker state.
- **Delta from PRD #718 to record:** (a) mock shows **3** posture pills (broker proof / submit / exposure); #718 lists **4** (adds Execution posture). This PRD ships the mock's 3; an Execution pill is a cheap future addition since the backend field exists. (b) Mark poisoned is a **visible Danger button** here vs. the `⋯` overflow in #718 — still confirmation-gated.
- **Live updates.** Retain the ~4s status poll and the existing broker-activity SSE for the activity overlay. No data-plane re-architecture.

## Testing Decisions

- **A good test asserts external behavior, not implementation.** Render the component with a **faked `LiveRunsService`** provided at the DI level, feed canned `LiveInstanceStatus` / lifecycle-timeline fixtures, and assert on **rendered output** (visible labels, pills, enabled/disabled controls, dialog presence, overlay contents) — never on private signals.
- **Reuse existing seams — do not add new ones.** Component-level specs already exist for this surface and are the highest practical seam: `bot-control-page.component.spec.ts`, `overview-tab.component.spec.ts`, `overview-actions.component.spec.ts`, `trader-guidance-pane.component.spec.ts`. Extend these (Angular Testing Library `render()` + `screen`, Vitest) rather than introducing lower-level harnesses.
- **Modules under test:** the reshaped page shell (region presence + responsive stacking behavior), the header posture badges (backend-label-else-piped-prose rule, and the muted "Unknown" path), the controls toolbar (bucket grouping, backend-driven enable/disable, disabled→reason→node-select, busy state), the Attention affordance (count, folded Why/risk header, severity dots, dynamic CTA label), the degraded banner (conditional render + session-only dismiss + re-appear on state change), the inspector (selected-vs-primary node, Meaning/Evidence/diagnostics, deploy "change for next run"), the confirmation gating (poison + flatten prompt; stop/pause/resume/fresh-run direct), and the two bottom overlays (open/close + preserved content).
- **Fixtures:** canned `LiveInstanceStatus` variants covering the honest-uncertainty states (broker unreachable, reconciliation blocked, unknown posture) and a healthy state, mirroring the mock's sample data. Prefer small deterministic fixtures over live data.
- **Accessibility:** assert AXE-clean on the toolbar, Attention dropdown, confirmation dialog, and overlays; verify keyboard operability and accessible names for every interactive control.
- **Regression:** any behavior that exists today (control actions firing the correct mutations, receipts rendering through `receiptLabel`, provenance copy-to-clipboard) keeps a passing test after the reshape.

## Out of Scope

- **Backend changes.** All data and mutations already exist in the Python service; this is a frontend reshape. No new endpoints, schemas, or GraphQL.
- **The fleet console** (`broker/instances`, cockpit-v2). This PRD touches the **per-bot** workbench only.
- **In-place config mutation.** Settings remain a "change for next run" (redeploy) flow per #718 decision D1; live config editing is a future ADR.
- **The mock's linear rail-timeline** as a literal left-pane layout — deliberately not adopted; the branching chart is retained.
- **A fourth "Execution posture" header pill** — noted as a cheap follow-up, not shipped in this slice.
- **New polling/streaming architecture** — the existing ~4s poll + SSE are reused.

## Further Notes

- This redesign is the visual realization of PRD #718 (`docs/bot-lifecycle-workbench-redesign.md`) and its proposed **ADR 0017**; it does not reopen those decisions except for the two recorded deltas (3-vs-4 pills; Mark-poisoned placement).
- **Honesty rule is binding:** no frontend-derived verdicts or chips. The only frontend-authored copy is concept tooltips keyed on stable ids.
- **Delivery:** implement on this branch, run project-scope lint (`eslint Frontend/src`) + Vitest, launch the app and capture screenshots of the running page, then stop and hand back the diff + screenshots. No PR is opened until the owner reviews and runs the thermo-nuclear code-quality review.
- **DoD:** on ship, update the canonical `docs/bot-lifecycle-account-owner-authority.md` to record the reshaped surface and the two deltas from #718.
