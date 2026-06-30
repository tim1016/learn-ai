# Per-Bot Lifecycle Workbench — Redesign Spec

**Surface:** `broker/bots/:id` → `bot-control-page.component` (PRD #718).
**Status:** Design agreed 2026-06-30 (grilling session). Decisions proposed in ADR 0017, pending ratification.
**Scope:** the per-bot workbench *only* — not the fleet console (`broker/instances`, cockpit-v2).
**Authority:** ADR 0013 (judgment vs evidence), ADR 0014 (backend-rendered narratives), ADR 0016 (trader-authored activity), `CONTEXT.md` (operator console glossary). Implementation snapshot DoD: `docs/bot-lifecycle-account-owner-authority.md`.

## North star

> **Can this bot safely place or manage the next trade — and if not, what exact field or proof needs action?**

Governing layout principle:

> **Top of page = facts that affect the next decision · Inspector = *why* · Advanced panels = *how the system knows*** (decision → explanation → provenance).

Cross-cutting honesty rule: **no frontend-derived verdicts or chips.** Backend authors state and lifecycle-action reason prose; Angular renders it verbatim. *Concept* tooltips are the only frontend-authored copy, keyed on stable ids, never on live values.

## Layout

```
┌─ BOT HEADER (sticky) ─────────────────────────────────────────────────────────────┐
│ SPY-EMA · SPY · RUNNING                  Broker proof: PAPER_ONLY    Execution: PAPER_EXECUTION    Exposure: FLAT │
│ Submit: ⛔ cannot submit — broker state unproven                                     │
│ ── Act now ──────────────────────────────────────────────────────────────────────  │
│ [Start] [Resume] [Pause] [Flatten & pause] [Stop]        [Redeploy fresh run] [⋯ ▾] │   ⋯ = Mark poisoned (typed confirm)
│ NEXT STEP →  Reconcile now            why: no fresh reconciliation receipt          │
│ Risk:  Do not treat stale broker evidence as live truth     ▸ Why this risk matters │
│ ▸ Attention (7)                                                                      │   (auto-expands if a CRITICAL group exists)
└────────────────────────────────────────────────────────────────────────────────────┘
┌─ LEFT: lifecycle chart (ONLY) ───────────────┐ ┌─ RIGHT: inspector (selected node) ───────────┐
│ deploy→preflight→account_safety→reconcile→    │ │ Selected step: Reconcile broker state         │
│ activate→active→submit / recovery             │ │ Meaning: broker state not proven to submit    │
│  • node colors carry global lifecycle state   │ │ Related action: Reconcile now → (Act-now bar) │
│  • attention badges on affected nodes         │ │ Evidence (read-only): reconciliation state ·  │
│  • hover action ⇒ highlight target node        │ │   broker connection · AccountOwner gen · ...  │
│  • click disabled action ⇒ select its node     │ │ ▸ Advanced: gate_id · source · hash · raw ts   │
│  (NO guidance pane embedded — relocated out)  │ │ [Change for next run] (deploy node only)      │
└───────────────────────────────────────────────┘ └───────────────────────────────────────────────┘
┌─ BELOW THE FOLD (collapsed) ──────────────────────────────────────────────────────┐
│ ▸ Recent activity (ActivityTab, temporary read-only reuse)   ▸ Full audit trail (workbench-owned read-only panel) │
│ ▸ Advanced technical evidence                                                        │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Header row order (sticky region):** identity → `broker proof · submit · execution · exposure` → Act-now bar → Next Step → Risk → Attention (collapsed). The Act-now bar separates the emergency cluster `[Start][Resume][Pause][Flatten & pause][Stop]` from `[Redeploy]` and the `⋯` overflow, so the emergency controls scan as one block. Redeploy is important but is not a risk brake.

## The three trader buckets

| Bucket | Members | Affordance / component |
|---|---|---|
| **Act now** | Start, Resume, Pause, Flatten & pause, Stop, Redeploy (+ Reconcile as the dynamic Next-Step remediation) | Sticky Act-now bar; disabled → trader reason + receipt. Mark-poisoned in `⋯` with typed confirm. |
| **Change for next run** | Daily order cap, Sizing preset, Hydrate policy, Action plan, Deploy/start config | `RedeploySettingField` → "Change via redeploy" → prefilled `/broker/deploy` + warning: *"This does not change the current running bot. It creates a new run with updated settings. Pause or flatten first if you need to stop current trading behavior."* |
| **Evidence** | Broker proof, Reconciliation state, AccountOwner generation, Runtime freshness, gate ids / reason codes / source artifacts / timestamps | `LockedEvidenceField` — read-only, advanced by default. The only place raw codes may appear. |

## Decisions (pending ratification — see proposed ADR 0017)

| # | Decision |
|---|---|
| **D1** | Sealed instruction, not a live config sheet. No in-place config mutation; three buckets above; `EditableReceiptField` → `RedeploySettingField`. Live mutation = future ADR. |
| **D2** | Node selection is **explanatory only, never an eligibility gate**. Eligibility comes only from backend `ActionCapability`. One sticky Act-now bar; emergencies always one click. Hover → highlight node; click-disabled → select node + reason + receipt. Remove the legacy "What you can do now" banner (single canonical command surface). |
| **D3** | "Posture" banned as a label → **Execution chip** (new `operator_surface.execution.posture`, no Angular derivation, Slice 2; engine `UNSAFE` maps to trader `UNSAFE`) + **Exposure chip** (`current_risk.posture`, ships now). |
| **D4** | Risk gets a persistent line (`risk_headline`) under Next Step; `risk_explanation` in a "▸ Why this risk matters" disclosure. Angular renders the backend-authored headline; it never classifies risk. |
| **D5** | Attention is global (collapsed "Attention (N)" band, auto-expands on a critical group) **and** node-scoped (badges on related nodes + detail in the inspector on select). |
| **D6** | Act-now bar renders backend-authored `lifecycle_chart.actions[].reason_headline` / `reason_detail`; `reason_code` appears only as a receipt. No per-bot frontend action-copy table. Tooltips = static concept registry + runbook links. |

## Where each dissolving `trader-guidance-pane` band lands

| Current band | New home |
|---|---|
| `summary-band` (headline/explanation) | Header Submit line (concise) + Next Step "why" (fuller) |
| `readiness-band` (submit_readiness) | Header Submit chip; blocking codes → inspector receipts |
| `next-step-band` (primary_remediation) | Global Next Step band (Reconcile etc. ride here) |
| `risk-band` (risk_headline/explanation) | Persistent Risk line under Next Step; explanation in disclosure |
| `attention-band` (additional_attention_groups) | Global Attention(N) summary + node badges + inspector detail |
| `trader-guidance-timeline` | Below fold → Recent activity |
| `owner-band` (account_owner) | Inspector evidence on the `account_safety` node |
| `advanced-evidence` | Inspector ▸ Advanced + below-fold Advanced technical evidence |

## Component plan (D7)

| Component | Fate in bot-control | Notes |
|---|---|---|
| `overview-tab` (chart) | **Keep, purify** | Remove the embedded `<app-trader-guidance-pane>` (`overview-tab.component.html:75`). Chart renders the graph only. |
| `trader-guidance-pane` | **Relocate / dissolve** | Bands redistribute per the table above. |
| `StatusRiskTab` | **Dissolve (unmount)** | gates → node evidence / badges / inspector; risk metrics → header chips + Risk row + inspector Evidence. |
| `ConfigurationTab` | **Dissolve (unmount)** | → "Change for next run" `RedeploySettingField` group on the deploy node. |
| `ActivityTab` | **Relocate below fold** | Reuse as-is. |
| `WorkbenchAuditPanel` | **Relocate below fold as workbench-owned read-only provenance** | Mark-poisoned lives **only** in the header `⋯`, never duplicated below the fold. The legacy cockpit-v2 `AuditTab` keeps its destructive control for the old surface until that interface is deleted. |
| **New** | `RedeploySettingField`, `LockedEvidenceField`, concept help registry, Act-now bar | |

No tab files are deleted — cockpit-v2 (`broker/instances`) still mounts all four. The four-tab nav is removed from bot-control entirely.

## Copy model (D6)

- **Verdict/reason copy (state-dependent).** End-state: the Act-now bar consumes `lifecycle_chart.actions[]` for `{id, label, enabled, target_node_id, tone, reason_code, reason_headline, reason_detail}`. `reason_headline` / `reason_detail` are backend-authored trader copy. `reason_code` is a raw receipt only; Angular never maps it through `disabled-reason-copy.ts` on this surface and never looks sideways at `operator_surface.actions[id].disabled_reason_code` to explain a lifecycle action. Invariant:
  - Bad: `Flatten and pause` / `NO_LIVE_BINDING`
  - Good: `Flatten and pause` / `No live bot process is bound to this run.` / `Receipt: NO_LIVE_BINDING`
- **Educational copy (concept-explaining, static).** A frontend help registry keyed on stable ids: node ids, action ids, gate ids, chips (Broker proof / Submit / Exposure / Execution), the three buckets, and concepts (reconciliation, AccountOwner, runtime freshness). Rule: *a tooltip explains what a concept means; a backend verdict explains what is true right now.* Deep/debug docs stay behind existing runbook links (`OpenRunbookAction`).

## Tests (D8)

1. **No-raw-enum invariant.** Mark primary copy regions `data-trader-copy` and receipt regions `data-receipt`. In states that produce disabled actions, assert **no member of the `OperatorReasonCode` union** appears in any `data-trader-copy` region, the mapped sentence *does*, and the code appears under `data-receipt`. (Primary regions must not render raw codes; receipt/provenance regions may and should.)
2. **Backend prose coverage.** Every server lifecycle-action reason code maps to backend-authored `reason_headline` / `reason_detail`; the per-bot Act-now component does not import `disabled-reason-copy.ts`.
3. **Chart purity.** `trader-guidance-pane` is not a descendant of `overview-tab`; no tab-nav `role` in bot-control; chart and inspector panes do not overlap.
4. **Node never gates actions.** Select a passed node → emergency action buttons keep their backend-driven enabled state.
5. **Execution-chip honesty (Slice 2).** The Execution chip renders only when `operator_surface.execution` exists; Angular never derives it.
6. **Tooltip completeness.** Every stable node id / action id / chip / bucket has help text; unknown/dynamic gate ids fall back to a generic gate-help entry (never "no help available").

## Slices (D9)

- **Slice 1 — frontend-only, ships now.** Full re-layout: relocate guidance out of the chart; header chips (broker proof, submit, exposure) + Act-now bar (Redeploy split, Mark-poisoned overflow); Next-Step / Risk / Attention rows; node-scoped inspector + Change-for-next-run; below-fold Activity / workbench-owned read-only Audit / Advanced; node attention badges; tooltip registry; **fix the raw-code bug** via table + receipt; tests 1–4, 6. **No Execution chip, no backend changes.**
- **Slice 2 — backend.** `operator_surface.execution.posture` — an *authored translation* of engine `effective_posture` (`PAPER_EXECUTION` → `PAPER_EXECUTION`; `PAPER_OBSERVATION` → `READ_ONLY`; `UNSAFE` → `UNSAFE`; stale/missing broker runtime proof → `UNKNOWN`). Add the Execution chip + test 5.
- **Slice 3 — backend, end-state.** `reason_code` / `reason_headline` / `reason_detail` on `lifecycle_chart.actions[]`; retire the frontend copy table for this surface; amend ADR 0013. Implemented here pending ADR 0017 ratification.

## Out of scope

- Live config mutation (a future ADR with its own requirements list).
- cockpit-v2 ↔ bot-control convergence: the fleet console (`broker/instances`) and the per-bot workbench (`broker/bots/:id`) coexist; tab-file deletion is a later fleet-console decision.
