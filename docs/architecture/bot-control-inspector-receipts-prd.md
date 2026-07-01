# PRD — Bot Control Dashboard: node-scoped trader-friendly receipts

- **Surface:** `broker/bots/:id` → `bot-control-page.component`, specifically the right-pane node inspector.
- **Builds on:** PRD #718 (Bot Lifecycle Workbench), PR #751 (Bot Control Dashboard redesign), and the existing `operator_surface` / `lifecycle_chart` / `LifecycleChartReceipt` contracts.
- **Design source:** Trader review of the shipped dashboard, cross-checked against the live backend code paths (Codex).
- **Data plane:** Python REST via `LiveRunsService`. No GraphQL.
- **Honesty rules (binding):** ADR-0013 (operator surface: judgment vs evidence), ADR-0014 (backend-authored, backend-rendered narratives), ADR-0001 (files canonical; Postgres is a rebuildable projection).
- **Implementation-snapshot DoD:** Every PR that implements a slice of this PRD updates `docs/bot-lifecycle-account-owner-authority.md` in the same PR.
- **Status:** ready-for-agent.

---

## Problem Statement

I am a trader looking at one bot on the Bot Control Dashboard. On the left is the lifecycle flowchart — Pre-flight gates, Account safety, Reconcile, Activate, Monitor, Submit order, Broker activity, Recovery. When I click a gate, I expect the right-hand panel to explain **that gate**, backed by the bot's real numbers, in language I understand.

Instead, no matter which gate I click, the right panel shows the same three global blocks — a "read-only proof" list, a "change for next run" block, and a "technical diagnostics" drawer. These are about the bot as a whole, not about the gate I selected. When I click **Pre-flight gates**, the fields have nothing to do with pre-flight. When I click **Account safety**, I still can't see the account number, whether the account is clean against Interactive Brokers, or whether my position is flat. When I click **Reconcile** and it says "blocked", nothing tells me how it gets unblocked.

I also don't understand some of the wording. "Ack" and "act" are engine words, not trader words. And some gates are purely internal machinery — I have no button to press on them — but the panel doesn't say that, so I can't tell whether I'm supposed to act or whether the system is just working through a step.

I need the right side to be the **exact numerical receipt for the node on the left, in trader language** — and when a gate genuinely can't prove something yet, I need it to say so plainly rather than show me an irrelevant or fabricated value.

## Solution

The node inspector becomes a **faithful, node-scoped view of the selected node's backend receipts**, rendered in trader-friendly prose that the **backend** authors. The left flowchart node and the right inspector become two views of one thing: the node is the shape, the inspector is that node's exact numbers explained.

Concretely:

- The inspector shows **only the selected node's** meaning, next step, and receipts. The three global blocks (read-only proof lines, change-for-next-run, technical diagnostics) leave the per-node inspector and move **below the fold** into the audit/advanced area, where whole-bot evidence belongs.
- Each gate's receipts are **authored numbers with a trader-language headline**. The number, unit, source, gate id, and timestamp stay as the auditable payload; a backend-authored `headline`/`detail` explains what the number means for the trader. The frontend renders that prose verbatim and formats only the raw code-like tokens through the shared `receiptLabel` pipe — it never invents meaning.
- Where a node **cannot prove something from its own backend-scoped receipt** — a signal event that isn't emitted yet, a per-bot P&L source that isn't authoritative, a broker snapshot that isn't folded — the inspector says so honestly ("not emitted yet", "not available yet"). It never borrows nearby global data to fill the gap.
- Where a gate is **internal machinery the trader has no role in**, the inspector states that plainly ("internal gate — no operator action needed; it can still block the bar"), driven by a backend-authored per-node actionability signal, and still shows the gate's receipts so the trader can see what it is checking.
- Trader-facing language is cleaned up: "ack" becomes "acknowledgment"; engine/enum spelling never leaks into trader copy.

This keeps the dashboard trader-friendly without becoming fictional: every number the trader sees is a real backend receipt, and every gap is labelled as a gap.

## User Stories

1. As a trader, I want the right panel to describe the exact gate I clicked in the flowchart, so that the left and right sides always agree.
2. As a trader, I want each gate to show only its own numbers, so that I am never shown another gate's evidence by accident.
3. As a trader, I want the global "read-only proof" list to move out of the per-gate panel and into a below-the-fold audit area, so that the gate panel stays focused.
4. As a trader, I want the "change for next run" settings to appear only on the deploy/configuration gate where they belong, not on every gate.
5. As a trader, I want each receipt shown as a plain sentence I can read, so that I don't have to decode `label is value unit` strings.
6. As a trader, I want the raw number, unit, source, gate id, and timestamp still available (as an audit detail) behind the plain sentence, so that I can verify the receipt.
7. As an auditor, I want the trader sentence to be authored by the backend, so that the meaning of a number is never invented by the frontend.
8. As a trader, on **Pre-flight → bar configuration**, I want to see the bot's intended instrument/bar plan (symbol, instrument surface, action plan, and the committed run config), so that I can confirm the bot is set up to trade what I expect.
9. As a trader, I want bar resolution / session / warmup shown on pre-flight **only when the backend can truthfully source them** (from config or strategy metadata), so that the panel never guesses.
10. As a trader, on **Pre-flight → poison sentinel**, I want a short explanation of what a poison sentinel is, plus whether it is armed and what specific condition would poison the bot, so that I understand the safety trip-wire.
11. As a trader, on **Pre-flight → prior delay / hold**, I want to see when the bot was last halted and its latest reconcile time, so that I understand why it is being held before it runs.
12. As a trader, on **Account safety**, I want to see the account number I'm trading, so that I know exactly which account is live.
13. As a trader, on **Account safety**, I want a clear "account is clean / not clean against Interactive Brokers" reconciliation summary, labelled as an account/fleet-level fact, so that I trust the account isn't contaminated.
14. As a trader, on **Account safety**, I want a deep link to the full account snapshot in the Account Monitor, so that I can inspect balances and positions when I need the detail.
15. As a trader, on **Account safety → broker safety**, I want to see the paper/live safety verdict and that this is a paper trading setup, so that I know I'm not about to send live orders by accident.
16. As a trader, on **Account safety → broker connection**, I want to see whether the broker is connected or disconnected, so that I know the bot can actually reach the broker.
17. As a trader, on **Account safety → current risk**, I want to see my current position posture (flat / long / short / mixed) and any pending order count, so that I know whether the bot currently has exposure.
18. As a trader, on **Reconcile → receipt state**, I want to see the cold-start receipt state (clean / adopted / stale / failed / in-progress / not available), so that I know whether broker and engine agree.
19. As a trader, when reconcile is blocked, I want to see the specific reason and how it gets unblocked, so that I know what to do next.
20. As a trader, on **Reconcile**, I want to see the adopted-intent count, last reconcile time, WAL sequence, and broker-observed time as receipts, so that I can see the evidence behind the verdict.
21. As a trader, on **Reconcile → broker snapshot**, I want the positions/intents the broker reported (when the backend folds them), and an honest "not folded yet" when it doesn't, so that I'm never shown a fake snapshot.
22. As a trader, on **Reconcile → continue or block**, I want the backend's continue/block verdict kept as the gate, so that I understand the AND-gate that lets the bot proceed.
23. As a trader, on **Activate → desired state**, I want to see the durable desired state (running / paused / stopped), when it was set, by whom, and why, so that I understand the bot's intended behaviour.
24. As a trader, on **Activate → resume capability**, I want to see whether resume is available and the specific reasons it is or isn't, so that I know if I can bring the bot back.
25. As a trader, on **Activate → command loop**, I want to see whether the command loop is fresh or stale and why, so that I trust the bot is listening to commands.
26. As a trader, on **Monitor**, I want a summary of recent trading activity (orders today, last fill), so that I can see what the bot has been doing.
27. As a trader, on **Monitor**, I want P&L shown only when it is authoritative, and an explicit "P&L not yet available" otherwise, so that I never mistake a placeholder for a real number.
28. As a trader, on **Submit order → strategy signal**, when no signal evidence is emitted yet, I want the panel to say "no signal evidence emitted yet" rather than show a blank or borrowed value.
29. As a trader, on **Submit order → intent / write-ledger (WAL)**, I want to see the durable order-intent receipt (intent id, sequence, timestamp) when one exists, so that I can trace the order.
30. As a trader, on **Submit order → broker submission**, I want to see the submit-boundary receipt (order ref, order id, perm id, timestamp), so that I can trace what reached the broker.
31. As a trader, on **Submit order → acknowledgment or reconcile**, I want the uncertain-outcome handling receipt in trader language, so that I understand what happens when an order's fate is unknown.
32. As a trader, on **Broker activity → activity publisher**, I want to see the publisher health (ready / starting / degraded / unavailable) and how recently it emitted, so that I trust the activity feed.
33. As a trader, on **Broker activity → owner generation**, I want to see the account-owner phase and generation, so that I understand which run owns the account.
34. As a trader, on **Broker activity → broker acknowledgment**, I want the latest broker execution evidence (when folded) and an honest "no direct evidence emitted yet" when not, so that I'm never shown fabricated fills.
35. As a trader, I want "ack" and "act" replaced with "acknowledgment" wherever they face me, so that the labels read like trader language.
36. As a trader, on any gate that is internal machinery I can't act on, I want the panel to say "internal gate — no operator action needed" while still showing its receipts, so that I know it's the system's job, not mine.
37. As a trader, I want a short concept tooltip on unfamiliar terms (poison sentinel, owner generation, WAL), so that I can learn what a concept means separately from what is true right now.
38. As a trader, I want the concept tooltip (what a term means) to be clearly distinct from the receipt (what is true now), so that I don't confuse definitions with the bot's current state.
39. As a screen-reader user, I want the inspector's node-scoped sections, honest-empty states, and audit detail to be reachable and labelled, so that the panel passes accessibility checks.
40. As a trader, I want whole-bot evidence (proof lines, advanced diagnostics) still available in the audit/advanced area below the fold, so that nothing I relied on is lost — it's just relocated.
41. As an operator, I want the relocation to preserve the existing bottom-bar activity and audit overlays, so that the richer history views keep working.
42. As an implementing agent, I want each node's receipt sourcing documented, so that I author receipts from real backend fields and mark honest gaps where a field doesn't exist.

## Implementation Decisions

**ID1 — The node inspector becomes purely node-scoped.** The selected-node inspector renders only: the node header (label, lane, status), the node's meaning / why / next step, and the node's receipts. The three currently-global blocks — read-only proof lines (`operator_surface.trader_guidance.proof_lines`), change-for-next-run redeploy settings, and the technical-diagnostics drawer (`operator_surface.trader_guidance.advanced_evidence`) — are removed from the per-node inspector and relocated below the fold into the existing audit/advanced surface (the workbench audit panel). Change-for-next-run, if retained inline anywhere, is scoped to the deploy/configuration node only.

**ID2 — The backend authors trader-friendly receipt prose.** `LifecycleChartReceipt` gains two backend-authored fields: `headline` (a single trader-language sentence stating what the number means) and `detail` (optional secondary trader context). The existing `label`, `value`, `unit`, `source`, `gate_id`, `ts_ms`, `ts_ms_resolved` remain the auditable payload and are unchanged. The frontend renders `headline` verbatim as the primary line and keeps the raw `label is value unit` line as the audit detail, with code-like tokens formatted through the shared `receiptLabel` pipe and opaque tokens (ids, refs, hashes, paths, urls) preserved exactly. The frontend never composes or classifies receipt meaning (ADR-0013 §1, ADR-0014 §1). The frontend TS `LifecycleChartReceipt` interface mirrors the two new nullable fields.

**ID3 — Author node-scoped receipts for the currently-empty sub-nodes** from data already on `operator_surface` and adjacent status fields, honoring these verified sourcing facts:
- **Pre-flight / configuration:** `OperatorSurfaceConfiguration` carries only `verdict` + `reason_codes`. The instrument/bar plan is sourced from sibling fields — the run's symbol, the action-plan projection, the instrument-surface plan (policy/explicit), and the committed run configuration provenance. Bar resolution / session / warmup are not typed on the operator surface (they live only in the run's live-config); surface them only if truthfully sourced, never inferred by the frontend.
- **Poison / prior hold:** the specific halt trigger, halt timestamp, and halt detail come from the instance's last-exit record (populated only when a poison flag is present), plus the readiness gate's poison-sentinel state. The operator surface's prior-run field exposes only a coarse classification, so last-exit is the receipt source. Last reconcile time comes from the reconciliation projection's `last_reconcile_ms`.
- **Account safety:** split the concepts. Paper/live safety comes from the broker safety verdict. Broker up/down comes from the broker connection state. Position posture and pending-order count come from the current-risk projection. Account identity/cleanliness comes from the fleet account summary (an account/fleet-level fact) and/or broker-observation-consistency — when shown on a single bot's node it must be labelled as account/fleet-level, and it is folded by the backend, not joined client-side.
- **Reconcile:** state, adopted-intent count, WAL sequence, broker-observed time, and failure reason already exist as receipts; add a remediation receipt/next-step when blocked. A fuller broker snapshot (positions/intents) is not on the operator surface today and requires a backend fold (S3).
- **Activate:** resume capability sources from the resume action capability's `disabled_reason_code` / `disabled_reasons` / `gate_results`; desired-state sources from the desired-state view's `state`, `updated_at_ms`, `updated_by`, `reason`, `path_status`; command-loop from runtime freshness.
- **Broker activity:** publisher health sources from broker-activity-health's `state` and `facts` (registered/running, latest row sequence, seconds-since-last-row). Owner generation sources from the account-owner phase/generation. The latest broker execution row is not folded into the operator surface today and requires a backend fold (S3).

**ID4 — Honest emptiness is a first-class state.** A node that cannot prove something from its own backend-scoped receipt renders an explicit "not emitted yet" / "not available yet" state — never a fabricated zero and never borrowed global data. Two specific verified gaps must render honestly: the **strategy signal** node and the **broker acknowledgment** node have no direct event emitters today (only intent-WAL, submit-order, place-order, and ack-or-reconcile are event-backed), and **per-bot P&L is not authoritatively populated** (see ID8). A new backend-authored per-node `operator_actionability` signal (proposed values: operator-actionable / system-only / no-action-needed) drives a standard inspector banner for internal-only gates ("internal gate — no operator action needed; it can still block the bar"). There is no general per-node actionability signal today, so this field is net-new backend work.

**ID5 — Cross-endpoint facts are folded by the backend, not joined by the frontend.** Account cleanliness (fleet account summary), the latest broker execution row, and the fuller broker snapshot are folded into the relevant node's receipts by the backend so the inspector remains a faithful renderer of `lifecycle_chart`. The full histories stay in the existing bottom-bar activity and audit overlays; the inspector may deep-link to the Account Monitor for the full account snapshot. The frontend must not turn the inspector into a hidden join layer over separately-fetched endpoints.

**ID6 — Trader-facing language cleanup.** Rename the backend-authored node display labels: "Ack or reconcile" → "Acknowledgment or reconcile", "Broker ack" → "Broker acknowledgment" (and consider "Broker submit" → "Broker submission"). Node ids are unchanged to preserve the contract. No "ack", "act", or enum spelling appears in trader-facing copy.

**ID7 — Concept tooltips stay static and frontend-owned, keyed on stable ids.** Extend the existing concept-help registry from node-level to receipt/term-level concepts (poison sentinel, owner generation, WAL, etc.). A tooltip explains what a concept *means*; the receipt states what is *true now*. The two are visually and semantically distinct.

**ID8 — P&L honesty decision.** Sources conflict on whether per-bot unrealized P&L is populated: the current-risk projection assigns it from the broker view, but the broker view's P&L is not actually populated (returns null), and the catalog P&L is likewise mostly null. This PRD resolves the conflict conservatively: **treat per-bot realized/unrealized P&L as not authoritative today.** The Monitor node shows a trade-activity summary (orders today, last fill) and an explicit "P&L not yet available" until a real per-bot P&L source is built and folded. Building that source is out of scope (see Out of Scope); folding it honestly when it exists is S3.

**ID9 — Slices.**
- **S1 (frontend-only):** Make the inspector node-scoped; relocate the three global blocks below the fold; add the honest-empty and internal-gate states as frontend rendering of existing/absent node data; extend concept tooltips. No backend contract change. Preserve backend-authored action prose.
- **S2 (backend receipt contract):** Add `headline`/`detail` to `LifecycleChartReceipt`; author trader-friendly receipts for the sub-nodes whose facts already exist on the operator surface (per ID3); add the `operator_actionability` field; apply the label rename (ID6).
- **S3 (deeper backend folds):** Fold account/fleet cleanliness, the latest broker execution row, and a fuller broker snapshot into node receipts; add event mappings (or honest-empty confirmation) for the currently-unemitted nodes; surface per-bot P&L only once an authoritative source exists.

## Testing Decisions

**What makes a good test here:** assert on what the trader observes in the rendered inspector (which node's receipts appear, whether a section is present/absent, whether copy contains codes) and on the backend receipts a synthetic operator surface produces — not on private component signals or internal function calls.

**Seam (confirm):** the highest existing seams, preferred over new ones.
- **Frontend:** extend the existing bot-control component specs (Angular Testing Library + Vitest) with `LiveRunsService` faked at the DI seam — `bot-control-page`, `node-inspector`, `overview-tab`. This is the seam already used by the shipped dashboard specs.
- **Backend:** pytest over the receipt-authoring and operator-surface composition functions, driven by synthetic `OperatorSurface` fixtures — the seam already used by the lifecycle-chart/receipt tests.

**Behavior to cover:**
- **Node-scoping:** clicking node X renders node X's receipts and header; the global proof-lines / change-for-next-run / advanced-diagnostics blocks are absent from the inspector and present in the below-fold audit area.
- **Trader-copy vs receipt separation:** trader-copy markers (`data-trader-copy`) contain no raw codes; receipt markers (`data-receipt`) may contain codes, formatted via `receiptLabel`; opaque tokens (intent/order ids, refs) preserved exactly. Extend the existing node-receipts / trader-copy separation tests.
- **Backend receipt prose:** authored `headline`/`detail` are present on receipts and are backend-sourced; a parity-style lock guards the closed set of concept copy, mirroring the disabled-reason-copy parity test.
- **Honest emptiness:** a node with no evidence (strategy signal, broker acknowledgment) renders an explicit not-emitted state; the Monitor node renders "P&L not yet available" rather than a zero; an internal-only gate renders the "no operator action needed" banner while still showing receipts.
- **Label rename:** no "ack"/"act" appears in trader-facing labels for the renamed nodes; chart/frontend tests re-run after the rename.
- **Per-node sourcing:** backend tests assert each targeted sub-node authors its receipts from the correct operator-surface field and yields an honest-empty receipt when the source is absent (null last-exit, null reconciliation, absent desired-state, etc.).

## Out of Scope

- **Live config mutation** from the inspector — sealed instruction only; deferred to a future ADR (consistent with PRD #718 D1).
- **Building a real per-bot P&L aggregation source** — this PRD folds P&L only once an authoritative source exists; creating that source is separate engine/broker work.
- **Adding event emitters** for the strategy-signal and broker-acknowledgment nodes — this PRD surfaces the honest gap; making those nodes event-backed is separate engine work.
- **cockpit-v2 convergence** — the fleet console (`broker/instances`) and per-bot page (`broker/bots/:id`) continue to coexist; deletion is a later fleet-console decision.
- **New cross-asset / spread instrument modeling.**
- **A live-trading safety posture** — the system remains paper-only; no `LIVE_EXECUTION` quadrant is introduced.

## Further Notes

- **Verified against live code (Codex + exploration), with these corrections to the initial brief:** the instrument/bar plan is on operator-surface sibling fields, not on the configuration object; broker-activity health exposes `state` + `facts`, not a `publisher_state` field; halt data lives on the instance last-exit record, not the operator-surface prior-run; the strategy-signal and broker-acknowledgment nodes are not event-backed today; and per-bot P&L is not authoritatively populated (ID8 resolves the source conflict conservatively).
- **Concurrency:** a background Codex agent commits into the shared working clone. The implementing agent must review read-only against origin refs and coordinate branches, rather than assuming exclusive control of the working tree.
- **DoD recap:** every implementing PR updates `docs/bot-lifecycle-account-owner-authority.md` as the implementation snapshot (which nodes are event-backed, which receipts are folded, what safety/cleanliness claims the UI may make, and which nodes render honest-empty states).
- **Governing principle:** if a node cannot prove something from its own backend-scoped receipt, the UI says so plainly. The dashboard stays trader-friendly without becoming fictional.
