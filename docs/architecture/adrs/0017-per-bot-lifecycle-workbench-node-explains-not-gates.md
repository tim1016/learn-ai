# ADR 0017 — Per-bot lifecycle workbench: nodes explain, they don't gate

**Status:** Proposed 2026-06-30. Drafted during the 2026-06-30 bot lifecycle control-panel grilling session.
**Decision drivers:** The per-bot workbench (`broker/bots/:id`) was organized around implementation tabs (Status & Risk / Activity / Audit / Configuration) that no longer match the product as the lifecycle chart accrues numerical receipts and new variables; the tab layout no longer fits the page; trader-facing surfaces were at risk of leaking raw enum codes as primary copy; "posture" was used as an overloaded, sometimes frontend-derived label; and there was no clear home for "what field or proof needs action" guidance.
**Related:** ADR 0013 (operator-surface boundary: judgment vs evidence — amended by Slice 3), ADR 0014 (broker-authored operator view, backend-rendered narratives), ADR 0015 (operator notice contract), ADR 0016 (Bot Cockpit trader-authored activity and deploy packages), `CONTEXT.md` (Live operator console glossary), PRD #718, `docs/bot-lifecycle-workbench-redesign.md` (implementation spec).

## Context

The per-bot lifecycle workbench has one trader-facing job: answer **"Can this bot safely place or manage the next trade, and if not, what exact field or proof needs action?"**

The current page is organized around implementation tabs (Status & Risk, Activity, Audit, Configuration) embedded in the right pane, while the lifecycle chart embeds its own trader-guidance pane on the left. As the chart gained backend-authored numerical receipts and lifecycle variables, the tab container stopped fitting the page and stopped matching how a trader reasons about a bot. Concretely:

1. **The chart embeds guidance it should not own.** `overview-tab.component.html` renders `<app-trader-guidance-pane>` inside the chart component. The chart should render only the lifecycle graph; meaning belongs in the inspector.
2. **The right pane is a tab container, not an inspector.** Four implementation tabs answer implementation questions, not the trader's one question.
3. **Raw enum codes can reach primary trader copy.** The Act-now bar sources disabled reasons from `lifecycle_chart.actions[].reason`, whose `_capability_reason` fallback (`bot_lifecycle_chart.py`) returns a raw code such as `NO_LIVE_BINDING`, rendered verbatim by `overview-actions`. The parity-tested copy table (`disabled-reason-copy.ts`) is bypassed.
4. **"Posture" is overloaded and sometimes derived in Angular.** Execution posture (engine `effective_posture`), position posture (`current_risk.posture`), and broker safety verdict (`broker.safety_verdict`) are three different facts wearing one word; the cockpit re-derives execution posture instead of consuming a backend field.
5. **No live config-mutation path exists, yet the proposed UI implied editable fields.** Daily order cap, sizing, hydrate policy, and action plan are deploy-time settings with no PATCH endpoint and no editability contract on `operator_surface`.

## Decision

### 1. Lifecycle chart left, node-scoped evidence inspector right; chart renders graph only

The lifecycle chart is the primary map (left, unclipped). The right side is a node-scoped Action + Evidence inspector, not a tab container. The four-tab nav is unmounted from this surface. `trader-guidance-pane` is relocated out of the chart component; the chart renders only the graph, its node colors, and attention badges.

### 2. Nodes explain, they do not gate

Selecting a lifecycle node is explanatory only. It never adds an eligibility gate to a live control. Action eligibility comes solely from the backend-authored `ActionCapability` (`enabled` / `disabled_reason_code`). One persistent, sticky Act-now bar keeps emergency controls (Flatten / Stop / Pause) one click away regardless of which node is selected. Hovering an action highlights its `target_node_id` node; clicking a disabled action selects that node and shows the trader-language reason plus a receipt. Mark-poisoned is a destructive control in a header overflow with typed confirmation, never gated behind selecting the recovery node.

### 3. A running bot is a sealed instruction; config changes happen by Redeploy

No in-place config mutation. Operator-facing fields fall into three buckets: **Act now** (live lifecycle controls), **Change for next run** (deploy-time settings changed only via Redeploy, with a warning that this starts a new run), and **Evidence** (read-only proof; the only place raw reason codes may render). Live config mutation, if ever built, is a separate future ADR requiring a PATCH endpoint, a server-authored editability contract, a durable config-change receipt, gate/reconciliation handling, and a current-vs-pending distinction.

### 4. "Posture" is split into backend-authored Execution and Exposure chips; Angular never derives them

The header carries two distinct, qualified chips: **Execution** (`PAPER_EXECUTION` / `READ_ONLY` / `UNSAFE` / `UNKNOWN` — a new backend field `operator_surface.execution.posture`, an authored translation of engine `effective_posture`) and **Exposure** (`FLAT` / `LONG` / `SHORT` / `MIXED` / `UNKNOWN` from `current_risk.posture`). Slice 2 resolves the engine-`UNSAFE` mismatch by preserving it as trader `UNSAFE` rather than collapsing known danger into `UNKNOWN`; `LIVE_EXECUTION` remains unpublished until a real engine posture can produce it. The Execution chip does not render until the backend authors it verbatim; Angular must not infer execution posture from `safety_verdict`, `readonly`, action effects, or host state.

### 5. Trader copy: backend authors state; raw codes appear only as receipts

Backend authors live verdicts and reasons. Angular renders them verbatim. A raw reason code may appear only when framed as a receipt/provenance fact, never as the primary explanation. Slice 3 moves per-bot action-reason prose to the backend: `lifecycle_chart.actions[]` carries `reason_code` / `reason_headline` / `reason_detail`, the Act-now bar renders headline/detail directly, and `reason_code` is displayed only as `Receipt: ...`. The per-bot workbench must not map action codes through `disabled-reason-copy.ts` or infer action prose from `operator_surface.actions[id]`; the shared copy table remains only for legacy cockpit-v2/fleet-console surfaces while they exist.

### 6. Tooltips explain concepts; verdicts explain state

Educational tooltips are a static frontend registry keyed on stable ids (node ids, action ids, gate ids, chips, buckets, concepts) plus existing runbook links. The dividing rule: a tooltip explains what a concept *means*; a backend verdict explains what is *true right now*. Unknown/dynamic gate ids fall back to a generic gate-help entry rather than leaking "no help available".

## Scope

This decision redesigns the **per-bot lifecycle workbench only** (`broker/bots/:id`). The shared cockpit-v2 tab components (`StatusRiskTab`, `ActivityTab`, `AuditTab`, `ConfigurationTab`) remain in the codebase because the fleet console (`broker/instances`) still mounts them. No tab files are deleted in this work. Deletion requires a later fleet-console convergence decision that is explicitly out of scope here.

## Consequences

- **+** Trader-first information architecture: the first right-pane content is action guidance, every actionable item has a receipt trail, and the chart never overlaps or embeds the inspector.
- **+** Emergency controls are always reachable; node selection can never strand a trader from stopping risk.
- **+** No frontend-derived chips, so the instrument panel cannot teach false confidence.
- **+** Ships frontend-only first (Slice 1, including the raw-code fix); backend work (Execution posture, action prose) is additive and independently testable.
- **+** Slice 3 removes the per-bot action-copy side lookup: lifecycle actions now carry backend-authored trader prose and raw codes are receipts only.
- **−** Two per-bot/fleet surfaces coexist until a later fleet-console cutover; the dissolved tab components linger as cockpit-v2 dependencies.
- **−** Two backend-authored posture chips now coexist by design: Execution can say `UNSAFE` while Broker proof carries the detailed safety verdict and receipts. Operators must read them as different facts, not synonyms.
