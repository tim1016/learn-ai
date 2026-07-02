# ADR 0013 — Operator-surface boundary: operational judgment versus evidence

**Status:** Accepted 2026-06-20. Drafted during the 2026-06-20 Bot Control redesign grilling session and locked in PRD #616.
**Decision drivers:** The 2026-06-20 grilling session against the revised bot control plan surfaced a structural pattern in the existing bot control: Angular-derived synthetic verdicts that compose independent server-authored facts into a single "bot status." ADR-0005 forbade this for readiness; ADR-0010 forbade it for actions; ADR-0011 forbade it for broker safety. None of the three named the bot control page-wide rule: **what belongs on `operator_surface` and what does not**. Without that rule, every new field is debated case-by-case and the synthetic-verdict pattern keeps creeping back. PRD #617's atomic UI cutover is the moment to lock the boundary so future contributors cannot drift back to a derived `botStatus()`.
**Related:** ADR 0005 (engine-authored readiness, two altitudes), ADR 0010 (operator-action contract — five canonical actions, canonical render-site rule), ADR 0011 (broker safety verdict — reactive, fail-closed), PRD #616 (backend authority + operator_surface contract completion), PRD #617 (atomic UI replacement).

## Context

The existing bot control page (eight slices merged into the Bot Control redesign branch) has the structural failure mode that the redesign exists to fix: synthetic verdicts have crept back in. The sticky control bar composes `process_state`, `readiness_verdict`, `broker_safety_verdict`, and `intent` into a single banner-glow that the operator reads as "the bot status." There is no single thing called "the bot status" — there are independent facts the operator needs to read independently. A bot can legitimately be `WAITING_FOR_HOST + RUNNING intent + BLOCKED readiness` and the bot control page must show all three.

The same pattern has appeared in three distinct places:

1. **Sticky control bar verdict-glow** — Angular composed PROCESS/INTENT/READINESS/BROKER/SAFETY into one banner color.
2. **Pre-Trade Checklist + Can-It-Trade** — two surfaces rendered the same readiness gates with different authority.
3. **POISON RUN keycap + Diagnostics tab** — the destructive action had multiple render sites.

In each case the structural cause was the same: no document named *which decisions belong server-side and which belong Frontend-side*. The bot control page code's authors made reasonable per-case choices that summed to "Frontend is allowed to derive judgments." Prose-only guidance ("the bot control page reads server-authored verdicts") has not prevented the regression: every reviewer agreed with the prose and still allowed the next synthetic verdict through.

PRD #617's atomic cutover deletes the synthetic verdicts that exist. ADR 0013 names the rule that prevents the next ones.

## Decision

### 1. The verbatim rule

**`operator_surface` contains verdicts, semantic classifications, capabilities, attention-routing inputs, notices, and remediation descriptors.**

**Decisions, trades, incidents, sizing audit rows, provenance, charts, and logs remain evidence on their canonical channels.**

**Angular may format evidence and map stable classifications to display copy. Angular may not derive verdicts, action eligibility, or remediation behavior from evidence.**

### 2. What this means concretely

| Bot Control needs to render | Belongs on `operator_surface`? | Authored by | Notes |
|---|---|---|---|
| "Is this bot safe to trade?" | **Yes** (`broker.safety_verdict`) | Python projection of ADR-0011 reactive verdict | The operator's mental model is "trust or not"; the bot control page reads. |
| "Is this gate fixable in-flight, and how?" | **Yes** (`readiness_gates[].suggested_action`) | Python projection over engine gates | A closed `kind` union; destructive actions reach the operator only via `focus_action`. |
| "Is Resume currently allowed?" | **Yes** (`actions.resume.enabled` + `disabled_reasons`) | Python shared resolver | All entry points (capability, mutation endpoint, CLI) consume the same resolver. |
| "Did the bot enter at 09:31?" | No — **evidence** | Decisions channel | The bot control page's Activity tab renders evidence; classification of *that* row is server-side (`signal`, `intended_action`). |
| "What's the next session boundary?" | **Yes** (`trading_session.next_transition_ms`) | Python projection of NY wall-clock + session policy | Lets the bot control page schedule a boundary-aligned refresh. |
| "What incident copy do I show for an ERROR row?" | **No** — evidence channel; classification on the row | Engine logs (`incident_category`); Bot Control maps category to display copy | The category enum is closed; mapping it to a string is presentation, not judgment. |
| "Is the account row in attention?" | **Yes** (`fleet_account_summary.account_identity` + `contamination.verdict` + `policy_blocks_starts`) | Python aggregator | The Angular formula is stable: `identity != CONSISTENT || verdict != clean || policy_blocks_starts`. |
| "What does this gate's `detail` field say?" | **No** — evidence (gate detail prose is server-authored but operator-formatted) | Engine readiness sidecar | The bot control page renders the prose; it does not parse it for verdicts. |

### 3. The Playwright meta-rule (structural enforcement)

Prose-only enforcement has failed historically. The structural enforcement clause:

> **Every Playwright scenario in the bot control page suite must assert independent PROCESS, INTENT, READINESS, BROKER, and SAFETY values rather than looking for a synthetic master status.**

This is the meta-rule that catches regressions when prose drifts. A new "the bot is healthy" pill added to the bot control page cannot pass the test matrix without explicitly asserting each underlying fact — which is the moment the contributor notices they have re-invented a derived verdict and reaches for the projection layer instead.

PRD #617 carries the meta-rule into every bot control Playwright spec. A new spec that asserts a single master verdict is rejected at code review.

### 4. Frontend-allowed derivations

Some derivation IS allowed Frontend-side, narrowly:

- **Polling-delta computations** the server cannot author (e.g. `classifyReadinessTransition(previous_verdict, current_verdict)`). The classification is closed (`'initial' | 'entered-attention' | 'attention-changed' | 'recovered' | 'stable'`); the function is a pure switch the server cannot run because it lacks the previous-poll snapshot.
- **Clock arithmetic** the server cannot author (the operator's local clock vs `as_of_ms`; the `CLOCK DIFFERENCE` advisory; the boundary-aligned refresh at `next_transition_ms + 1000`).
- **Presentation copy lookup** keyed on a closed server-authored enum (`incident_category` → operator-language string; gate `name` → label fallback). The server is authoritative for the enum; the bot control page chooses the typography.
- **Manual-selection state** that has no server counterpart (which inner tab the operator last clicked; whether they have seen an attention marker on a background instance).

The shared rule across all four: the bot control page derives over **enumeration values** and **polling-delta state**, never over **evidence content**. A derivation that reads a decision row's `intended_action` to decide whether a button should be enabled would violate the rule; a derivation that reads `incident_category` to choose a string color does not.

### 5. Inclusion test for new `operator_surface` fields

When adding a new field, ask in order:

1. *Does the bot control page make an operational decision from this field?* If yes (button enabled/disabled, attention routing, tab forcing), it belongs on `operator_surface`.
2. *Is the value a stable classification (closed enum, structured verdict)?* If yes, it belongs on `operator_surface`.
3. *Is it evidence (an event, a row, a number with no inherent classification)?* If yes, it belongs on the corresponding evidence channel (decisions, trades, incidents, audit), and the operator-facing classification (if any) is a separate field on `operator_surface`.
4. *Does it require Frontend to know about bot control affordances?* If yes, the field belongs on the projection layer (`operator_surface`), not the engine sidecar (per ADR 0005 §Amendment B1).

A field that fails (3) and (4) is the wrong shape — propose the classification field on the projection layer first, and keep the raw evidence on the evidence channel.

### 6. The shared resolver pattern

When a verdict is consumed by both a server-side mutation gate and a UI capability, it MUST be resolved once and shared. PRD #616's `ResumeGuardState` is the canonical example:

- The capability projection (`operator_surface.actions.resume`) reads the resolver.
- The desired-state mutation endpoint re-runs the resolver immediately before the durable write.
- The CLI `cmd_resume` consumes the same resolver.

A bypass at any entry point (the deleted `--force` flag) invalidates the structural claim. Future verdicts that join the structural-safety guarantee must follow the same pattern — and named entry points must consume the canonical resolver, not a parallel implementation.

## Consequences

**Positive:**
- The "what belongs on `operator_surface`" question has a written answer; future contributors do not re-derive the boundary case-by-case.
- The synthetic-verdict regression mode is structurally blocked by the Playwright meta-rule. A `botStatus()` derivation cannot pass review without an explicit override of the meta-rule, which makes it visible.
- The destructive-action canonical-render-site rule (ADR 0010 §A2) is coupled to the projection layer: `suggested_action.kind == "focus_action"` is the *only* way a destructive action reaches the operator from a readiness gate. The structural rule replaces dispersed prose warnings.
- The "single resolver for every entry point" pattern formalizes the PRD #616 shape. Future verdicts (e.g. a deploy-time safety check) can follow the same template.

**Negative:**
- A new `operator_surface` field requires Python authoring even when the bot control page could derive it Frontend-side in three lines. This is intentional — the friction is the boundary's enforcement mechanism — but it costs a small amount of upfront work per field.
- The Playwright meta-rule rejects the convenience of a single "everything is healthy" pill in bot control tests. Reviewers must hold the line when a new spec proposes one.
- The ADR documents the rule that prose-only enforcement has failed. Contributors must accept that the meta-rule is *also* a structural enforcement and not a stylistic preference.

**Non-consequences:**
- The engine readiness sidecar shape (ADR 0005) is unchanged.
- The five canonical actions (ADR 0010) are unchanged; this ADR names the projection layer that surfaces them.
- The reactive broker safety verdict (ADR 0011) is unchanged; this ADR names the projection layer that consumes it.
- No new mutation endpoints, no new wire-shape fields beyond PRD #616.

## References

- `PythonDataService/app/services/operator_surface.py` — the projection layer this ADR names.
- `PythonDataService/app/services/operator_capability.py` — the shared capability evaluator.
- `PythonDataService/app/services/resume_guard_state.py` — the shared resolver template (PRD #616).
- `PythonDataService/app/schemas/live_runs.py` — `OperatorGate`, `GateSuggestedAction` closed union, `FleetAccountSummary`.
- `Frontend/tests/e2e/bot-control-*.spec.ts` — the Playwright suite where the meta-rule is enforced (PRD #617).
- `docs/architecture/adrs/0005-engine-authored-readiness-two-altitude-broker-ownership.md` § Amendment 2026-06-20 — the projection layer's authority over the engine sidecar.
- `docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md` § Amendment 2026-06-20 — the canonical-render-site rule, the five canonical actions.
- `docs/architecture/adrs/0011-broker-safety-verdict-fail-closed-reactive-halt-on-transition.md` — the reactive `BrokerSafetyVerdict.final_verdict` consumed by `_project_broker`.

## Amendment 2026-06-22 — `trader_guidance` presentation projection

**Context:** The 2026-06-22 trader-guidance amendment found that the current bot control page gives each independent fact — PROCESS, INTENT, READINESS, BROKER, SAFETY, PRIOR RUN, SESSION — equal visual weight, leaving traders without a primary message and without a single recommended next step. The amendment permits a backend-authored `trader_guidance` projection on `operator_surface` that selects one prioritized situation, one recommended remediation, and a deduplicated list of additional attention groups.

This amendment permits that addition under the constraints below. It does **not** loosen §1 (the verbatim rule), §3 (the Playwright meta-rule), or any other authority in this ADR. The synthetic-master-status regression mode that motivated this ADR remains prohibited.

**Permitted addition.** `operator_surface.trader_guidance: TraderGuidance` — a presentation classification with:

- `situation_code` — closed enum governed by `PythonDataService/app/schemas/live_runs.py` and authored by `PythonDataService/app/services/operator_trader_guidance.py`;
- `headline`, `explanation`, `risk_headline`, `risk_explanation` — operator-language copy, server-authored;
- `primary_remediation` — a closed discriminated union whose every kind references an existing server-authored capability (`invoke_capability`), an existing remediation descriptor (`focus_action`, `focus_view`, `redeploy`, `open_runbook`), an already-authorized control endpoint (`start_bot_process` against ADR 0006 `POST /runs/{run_id}/start`), an operator command sourced from trusted deployment configuration (`copy_host_command`), a structured broker-direct workflow (`broker_manual_risk_reduction`), or `none`;
- `additional_attention_groups` — a closed `AttentionGroup[]` list with backend-authored deduplication rules.

**Required constraints. Any one violation removes the permission.**

1. **`trader_guidance` is presentation, not a safety verdict.** It never gates a mutation. The five canonical actions (ADR 0010) and the broker safety verdict (ADR 0011) remain authoritative for action eligibility. `trader_guidance.primary_remediation` references an action whose enablement is determined by the existing capability and gate resolvers; the field never re-asks the question.
2. **`trader_guidance` never enables or disables an action.** Disabled-action reason codes continue to flow from `operator_capability`. The `start_bot_process` and `copy_host_command` remediation kinds reference the existing authenticated start endpoint (ADR 0006) and the trusted-configuration command string, respectively; neither bypasses any gate. The start endpoint must re-evaluate the same `host_process.start_capability` before launch.
3. **`trader_guidance` never replaces or mutates the independent facts.** `host_process`, `runtime_freshness`, `broker`, `prior_run`, `trading_session`, `readiness`, `actions`, and the configuration projections remain rendered, asserted, and authoritative on their canonical channels. §3's Playwright meta-rule continues to require each independent fact assertion in every bot control scenario test; a spec that asserts only `trader_guidance.situation_code` and omits the underlying facts is rejected at code review.
4. **The `situation_code` enum is closed and changes are gated.** Adding or splitting a value requires (a) a compatibility review against this ADR, (b) a backend decision-table update, (c) a fixture and backend projection test, (d) a trader-visible copy review. The implementation does not test the full Cartesian product; it table-tests every winning situation, every adjacent priority collision, named hazardous collisions, and the preservation of all independent source facts.
5. **The field name is `trader_guidance`.** Not `state`, `bot_status`, `master_status`, or any synonym. The naming is structural: it identifies the field as presentation guidance, separate from the verdicts it composes.
6. **Angular renders verbatim.** Headlines, explanations, instruction strings, and command strings are server-authored. Angular maps `situation_code` and `primary_remediation.kind` to component selection only — it does not assemble copy, interpolate commands, or compose remediation from independent facts.

**Inclusion-test outcome (this ADR §5).** `trader_guidance` passes test 1 (the bot control page makes operational decisions from it — visual priority, attention routing, recommended next step) and test 2 (the `situation_code` enum and `primary_remediation` kinds are closed classifications). It does not violate test 3: the underlying decisions, trades, incidents, and audit rows continue to flow on their evidence channels. `trader_guidance` is a derived classification over closed verdicts, not a new evidence stream and not a parallel verdict.

**Frontend-allowed derivations (§4).** Unchanged. Angular may still map `situation_code` to a layout component and `primary_remediation.kind` to a button-or-link choice — these are closed-enum lookups, the same shape as the existing `incident_category` → display-string mapping permitted by §4.

**References:**
- `PythonDataService/app/services/operator_trader_guidance.py` — authors the prioritized findings, trader copy, primary remediation, and attention groups.
- `PythonDataService/tests/services/test_operator_surface.py` — regression coverage for the emitted trader-guidance situations.
- `docs/architecture/adrs/0006-deploy-control-plane-host-daemon-init-ledger.md` — the authenticated `POST /runs/{run_id}/start` path that `primary_remediation.start_bot_process` references.
- `docs/architecture/adrs/0007-host-daemon-shared-secret-auth.md` — the `X-Live-Runner-Token` auth boundary that makes the start path safe for the bot control page to invoke through the data-plane proxy.

## Amendment 2026-06-30 — per-bot lifecycle action prose

**Context:** ADR 0017's per-bot lifecycle workbench found that disabled lifecycle actions could leak raw enum codes as primary trader copy. Slice 1 allowed a temporary frontend copy lookup keyed on the closed server action-code enum. Slice 3 removes that exception for the per-bot workbench: the backend now authors lifecycle action prose directly on `lifecycle_chart.actions[]`.

**Permitted addition.** `lifecycle_chart.actions[]` may carry:

- `reason_code` — a raw receipt code, nullable when the action is enabled or when no structured code exists;
- `reason_headline` — short trader-facing copy authored by Python;
- `reason_detail` — longer trader-facing explanation authored by Python.

**Required constraints. Any one violation removes the permission.**

1. **Action eligibility remains elsewhere.** `enabled` still comes from the canonical `ActionCapability` / host-start / redeploy proof paths, and mutation endpoints still re-evaluate the same gates before executing. `reason_headline` and `reason_detail` explain; they never gate.
2. **Raw codes are receipts only.** Angular may render `reason_code` only in receipt/provenance regions. It may not render the code as primary trader copy, and it may not map the code through a per-bot frontend action-copy table.
3. **Angular renders action prose verbatim.** The per-bot Act-now bar consumes `reason_headline` / `reason_detail` from `lifecycle_chart.actions[]`. It must not infer action prose from `operator_surface.actions[id].disabled_reason_code`, `host_process.start_capability.disabled_reason_code`, node status, or host state.
4. **Frontend copy-map permission is narrowed.** ADR 0013 §4 still permits closed-enum presentation lookup for legacy bot-control/bot-catalog surfaces while they exist. That permission no longer applies to per-bot lifecycle action prose.
5. **Coverage is backend-owned.** Every server-emitted lifecycle action reason code must have backend-authored headline/detail coverage. Adding a code requires updating the backend lifecycle-action reason table and tests.

**Inclusion-test outcome (this ADR §5).** Lifecycle action reason prose passes test 1 because the bot control page displays it next to live command controls, and test 2 because `reason_code` is a stable classification over existing server-authored gates. It does not create a new evidence channel: raw evidence and gate receipts remain on their canonical surfaces, while the lifecycle action carries the presentation explanation for an already-authored action capability.
