# PRD: Bot Cockpit Trader Activity and Deploy Strategy Packages

**Status:** Published for implementation
**Created:** 2026-06-25
**Published issue:** #689
**Hardened after review:** 2026-06-25
**Owner:** Inkant
**Architectural anchor:** ADR 0016 — Bot Cockpit trader-authored activity and deploy packages
**Related ADRs:** ADR 0013, ADR 0014, ADR 0015
**Primary objective:** Make Bot Cockpit and Deploy a strategy trader-readable, durable-trade-visible, backend-authored, and stable under refresh.

## Problem Statement

Traders using Bot Cockpit cannot reliably answer the most important live-paper questions: what trades happened, what broker evidence means, what incidents repeated, what configuration the bot actually ran with, and whether the deploy form is asking for meaningful decisions or implementation details.

The most urgent failure is trade visibility. The JUNE-25 bot has durable trade evidence in its run artifacts, but the Activity table does not show the trades because the current Activity projection depends on a per-instance broker activity stream that may not have populated after recent changes. A trader should never see an empty Activity table when fills or closed-trade summaries exist in durable evidence.

The second failure is language. Bot Cockpit currently leaks code-shaped terms such as `endpoint_snapshot`, raw JSON, `spec_path`, account-source values, and request/callback names into primary UI. These strings may be useful to engineers in a drill-down, but they do not help a trader understand broker activity, audit evidence, or incidents.

The third failure is refresh behavior. Activity and evidence tables can flash or redraw during local file/projection refresh even when no usable trader row changed. This makes the page feel unstable and erodes trust in live monitoring.

The fourth failure is deploy complexity. Deploy a strategy asks the trader to assemble raw strategy paths, settings files, account identifiers, backtest IDs, audit copy paths, and action-plan primitives. The trader should create or select a validated strategy package, name the bot, review connected broker account evidence, choose sizing/action instruments, and deploy.

## Solution

Ship a Bot Cockpit and Deploy overhaul grounded in ADR 0016.

Bot Cockpit Activity will be backed by a backend-authored activity projection that always surfaces durable trade evidence. Broker activity remains the preferred live authority, but when the per-instance broker activity stream is missing or stale, the backend repairs or supplements the projection from raw broker callbacks or legacy run artifacts. This repair/supplement path is net-new production wiring: existing reconstruction code is test-covered but not called by the production Activity projection.

The trader sees broker fills and closed-trade summaries in Activity without manual reconstruction. Fill reconstruction is sourced from raw callback WALs or `executions.parquet`; closed-trade summaries require a new `trades.parquet` projection path because the existing reconstruction module reads execution rows only.

All trader-facing evidence rows, incident panels, audit summaries, and deploy validation summaries will be authored by the backend through a closed event narrative registry. The frontend renders finished labels, explanations, severities, fold counts, and diagnostic facts. Angular may format layout, ET display time, numbers, PrimeNG badges, expansion state, and chart rendering, but it does not infer event meaning or repeated-row sameness.

Activity and incidents will fold repeated consecutive phenomena using backend-authored fold keys and counts. Activity will also use backend-authored structural cluster keys for order/fill grouping; clustering and duplicate folding are different concepts. The visible table will use incremental merge behavior keyed by a backend-authored `visible_row_id` so the parent Activity tab remains mounted and the table only highlights or visibly changes when a usable row is added or a visible fold count changes.

Audit and Configuration will be separated: Configuration shows what the bot was intended and configured to run with; Audit shows evidence of what happened and whether that evidence supports the intended configuration. Raw JSON and exact codes move to technical details.

Deploy a strategy will create or select validated strategy packages through a backend-owned package registry. The package registry is net-new: today's deploy form still submits loose raw fields. The final bot name is the lifetime-unique, system-safe `strategy_instance_id`. The broker account is display-only evidence from the connected broker session; if it is unavailable or ambiguous, Deploy fails closed. Strategy settings appear only when the selected package requires trader-tunable controls. Stock and option action-plan selection uses rich trader-readable pickers. PrimeNG components and app theme tokens are the default UI vocabulary, with Apache ECharts retained for charts.

## Grounded Implementation Gaps

This PRD depends on several pieces that are not production-wired today. They are first-class scope, not assumed infrastructure.

1. **Activity repair trigger.** `broker_activity_reconstruction.py::reconstruct_broker_activity_for_run()` exists and is test-covered, but no production route or projection calls it. The Activity repair slice must add trigger semantics, idempotency, and cost controls without making Activity GET append to the broker-activity WAL.
2. **Closed-trade summary path.** Existing reconstruction reads `executions.parquet`; it does not read `trades.parquet`. Closed-trade history needs a separate projection or explicit pairing from execution fills.
3. **Evidence narrative registry for diagnostics.** Broker fills already use backend templates; endpoint/evidence rows such as `endpoint_snapshot` are currently open diagnostics from the IBKR evidence recorder. The registry slice must catalog known recorder request/callback names and define an unmapped diagnostic fallback.
4. **Backend cluster and fold identities.** Current Angular groups activity rows by `perm_id`/`exec_id`. The backend must author both structural clustering (`cluster_key`, `cluster_label`) and duplicate folding (`fold_key`, `fold_count`) before the frontend can stop deriving groups.
5. **Stable visible row identity.** The anti-flash UI cannot be implemented until the backend supplies a stable `visible_row_id` for every rendered row or folded group.
6. **Deploy package registry.** No package registry exists today. The package slice must define package storage, validation assertions, and how today's raw deploy fields map into a package.
7. **Broker position evidence source.** Activity `position_snapshot` is currently `source="unavailable"` in the projection. Trader-readable position evidence depends on wiring a real broker position snapshot feed or rendering an explicit "not captured" narrative.

## User Stories

1. As a trader, I want the Bot Cockpit Activity tab to show every broker fill that happened, so that I can trust the cockpit during a live-paper session.
2. As a trader, I want closed-trade summaries to appear in Activity when `trades.parquet` exists, so that a missing broker activity stream does not hide real trade history.
3. As a trader, I want the Activity table to explain where a reconstructed row came from, so that I can distinguish live broker capture from repaired historical evidence when needed.
4. As a trader, I want Activity rows to use plain trading language, so that I do not need to interpret API callback names.
5. As a trader, I want raw event codes to be hidden in technical details, so that the primary table stays readable.
6. As a trader, I want `endpoint_snapshot`-style rows to say what broker evidence was refreshed, so that I know whether the bot checked positions, executions, open orders, or account state.
7. As a trader, I want broker account position evidence to be summarized in trading language when a real broker position snapshot is captured, and to see a clear "not captured" explanation when it is unavailable.
8. As a trader, I want account-source evidence to explain where the account value came from, so that I know whether it was observed from the broker session or loaded from saved config.
9. As a trader, I want all primary market/session times shown in ET, so that I can match the cockpit to U.S. market hours and broker session context.
10. As an engineer, I want canonical UTC milliseconds preserved in technical details, so that forensic analysis remains exact.
11. As a trader, I want repeated broker evidence rows folded with a count badge, so that the Activity table is not filled with duplicate polling noise.
12. As a trader, I want repeated incidents folded into one panel with a count badge, so that I can see the pattern without scrolling through copies.
13. As a trader, I want to expand a folded Activity row, so that I can inspect every underlying evidence row.
14. As a trader, I want to expand a folded incident panel, so that I can inspect each individual incident occurrence.
15. As an engineer, I want fold keys and fold counts authored by the backend, so that Angular does not infer sameness from text or JSON.
16. As a trader, I want the Activity table not to flash during background refresh, so that I can monitor live trading without losing visual context.
17. As a trader, I want row expansion and scroll context preserved during refresh, so that inspecting one row is not interrupted by polling.
18. As a trader, I want the table to visibly update only when a usable row is added or a fold count changes, so that animation means something.
19. As an engineer, I want low-level file refresh churn treated as diagnostic detail, so that it does not redraw the primary table.
20. As a trader, I want a clear empty state only when there are truly no broker fills, trades, orders, or relevant evidence, so that “empty” is meaningful.
21. As a trader, I want Activity to show fills from the next live-paper bot run without manual reconstruction, so that the recent stream path is proven operationally after merge.
22. As a trader, I want Activity to repair older runs when needed, so that historical bot sessions remain inspectable.
23. As a trader, I want the Audit page run identity fields to have human labels, so that `spec_path`-style variables are not primary UI.
24. As a trader, I want Audit times such as created, started, and completed dates shown in ET, so that audit evidence aligns with the market/session timeline.
25. As a trader, I want runtime configuration grouped and formatted, so that I can scan the configuration without reading JSON.
26. As an engineer, I want exact runtime JSON available in technical details, so that debugging remains possible.
27. As a trader, I want Broker API Evidence to use readable contrast, so that evidence panels are legible in the app theme.
28. As a designer, I want evidence colors to use app theme tokens, so that dark/light theme behavior remains consistent.
29. As a trader, I want Audit to show evidence of what happened, so that it does not duplicate Configuration as another raw settings page.
30. As a trader, I want Configuration to show intended bot setup, so that I know what the bot was supposed to run.
31. As a trader, I want duplicated audit/config fields removed or linked, so that I do not have to reconcile two versions of the same fact.
32. As a trader, I want Deploy a strategy to prefill a random bot name candidate, so that I can start from a sensible lifetime-unique identity.
33. As a trader, I want to edit the bot name before deploy, so that the Bot Cockpit identity is meaningful to me.
34. As an engineer, I want the bot name to be the `strategy_instance_id`, so that there is no display-only identity drifting from durable identity.
35. As a trader, I want bot-name lifetime uniqueness enforced, so that I do not confuse two deployed bots or reuse a broker attribution namespace.
36. As an engineer, I want bot-name validation to enforce path and broker attribution constraints, so that identity remains safe.
37. As a trader, I want Deploy to create or select a validated strategy package, so that I do not assemble raw paths and IDs by hand.
38. As a trader, I want validated packages to include strategy, settings, golden fixture or parity evidence, backtest evidence, and audit provenance, so that Deploy knows the package is fit for live-paper use.
39. As an engineer, I want validated strategy packages created on Deploy a strategy, so that Engine Lab is not the package-authoring workflow.
40. As a trader, I want strategy settings hidden when a package does not require them, so that the form is not cluttered with irrelevant fields.
41. As a trader, I want required strategy settings shown as named controls, so that I do not edit a raw JSON settings path.
42. As an engineer, I want raw settings paths and hashes preserved as provenance, so that auditability remains intact.
43. As a trader, I want the connected broker account shown read-only on Deploy, so that I know which account the bot will use.
44. As a trader, I want Deploy to fail closed when no broker account is connected, so that I do not deploy against an unknown account.
45. As a trader, I want Deploy to fail closed when multiple broker accounts are ambiguous, so that account selection cannot drift from broker reality.
46. As a trader, I want stock action-plan selection to show symbol and recognizable company/exchange context, so that I can choose confidently.
47. As a trader, I want option action-plan selection to show expiry, strike, call/put, multiplier, and quote context, so that contracts are readable.
48. As an engineer, I want raw contract IDs in technical details, so that exact broker identifiers remain available.
49. As a trader, I want position sizing to remain clear and prominent, so that I can understand order size before deploying.
50. As a trader, I want Deploy to remove redundant backtest verification inputs when the selected validated package already carries validation evidence, so that I do not re-prove something the package owns.
51. As a trader, I want package validation failures explained in plain language, so that I know what to fix before deploying.
52. As an engineer, I want package validation failures backed by structured codes, so that tests can enforce every failure path.
53. As a trader, I want PrimeNG tables, panels, accordions, badges, forms, and dialogs used consistently, so that the cockpit feels stable and accessible.
54. As a trader, I want existing charts to keep using Apache ECharts where appropriate, so that working chart behavior is not reinvented.
55. As an engineer, I want bespoke UI replaced by PrimeNG only within the narrow surface owned by the current slice, so that future UI changes are less fragile without diff sprawl.
56. As a reviewer, I want backend narrative coverage tests proving every currently known recorder event code maps to a registry outcome and unknown recorder codes route to the unmapped-diagnostic fallback, so that no raw event code leaks into primary UI.
57. As a reviewer, I want frontend tests to assert rendered trader language rather than private component state, so that tests match user behavior.
58. As a reviewer, I want frontend no-op refresh tests to preserve row DOM identity and expansion state, so that Activity no-flash regressions are caught without flaky pixel assertions.
59. As Claude reviewing the PRD, I want clear implementation and testing seams, so that I can challenge scope before coding starts.
60. As an implementation agent, I want the PRD broken into vertical slices later, so that each slice can be independently verified.
61. As an engineer, I want the CI path to use recorded/replayed fill fixtures, so that implementation is not blocked on waiting for a future market session.
62. As an engineer, I want Deploy's removal of manual broker-account entry called out as an intentional behavior change, so that redeploy behavior during broker outages is reviewed deliberately.
63. As a trader, I want closed-trade summaries visually tied to their constituent fills, so that I do not double-count a round trip as extra broker activity.
64. As an engineer, I want package evidence drift to fail closed at deploy/redeploy, so that a package cannot silently point at changed validation artifacts.

## Implementation Decisions

1. The canonical trader-facing product term is **Bot Cockpit**.
2. The backend owns trader-facing event meaning through a closed event narrative registry.
3. The frontend renders backend-authored labels, explanations, severities, fold counts, and technical facts.
4. Raw codes remain diagnostic details and must not be primary trader text.
5. Activity uses broker activity as the preferred live authority but must repair or supplement from durable broker callbacks or legacy execution artifacts when the preferred stream is missing or stale.
6. Closed-trade summaries from `trades.parquet` are a separate projection path; they are not covered by the existing broker fill reconstruction module.
7. Reconstructed Activity rows must carry provenance that can be rendered in expansion.
8. Primary UI market/session times are ET; wire/storage timestamps remain `int64 ms UTC`.
9. The backend must not emit preformatted local-time strings; Angular formats integer milliseconds at the UI boundary.
10. Fold keys and fold counts are backend-authored for duplicate-noise folding.
11. Structural order/fill clustering is backend-authored separately from folding through `cluster_key` and `cluster_label`.
12. Every visible Activity row or folded group has a backend-authored `visible_row_id`.
13. Activity refresh uses incremental merge semantics keyed by `visible_row_id`, not full table replacement.
14. Usable-row changes are the only visible table-change trigger: broker fills, order lifecycle events, trader-relevant broker evidence summaries, trader-relevant incidents, or visible fold-count changes.
15. Low-level polling and file-refresh churn remains diagnostic evidence.
16. Audit and Configuration have separate responsibilities: intended setup versus evidence of what happened.
17. Runtime configuration is grouped and formatted for traders, with exact JSON preserved only in technical details.
18. Broker API Evidence uses theme-token styling and backend-authored descriptions.
19. Deploy a strategy owns validated strategy package creation and selection.
20. Engine Lab is not the package-authoring surface for this workflow, though package creation may select validation artifacts produced elsewhere.
21. A validated strategy package binds strategy/spec, approved settings, parity/golden fixture evidence, backtest/audit provenance, and deployable action-plan schema.
22. Package creation takes selected strategy/spec inputs plus selected validation evidence; storage and validation are backend-owned.
23. Strategy settings are shown only as package-defined trader controls when required.
24. Bot name and `strategy_instance_id` are the same final identity.
25. Deploy may prefill a random editable bot name, but the final identity is lifetime-unique and system-safe.
26. Broker account on Deploy is read-only evidence from the connected broker session.
27. Deploy fails closed when the connected broker account is absent or ambiguous; this intentionally removes current manual account entry.
28. Action-plan stock and option selection uses trader-readable instrument pickers.
29. PrimeNG is the default component vocabulary for ordinary UI; Apache ECharts remains the charting tool.
30. App theme tokens are the styling authority for evidence contrast, severity, spacing, and emphasis.
31. The implementation should prefer deep modules with small stable interfaces: activity projection, narrative registry, deploy package registry, and frontend incremental row merge.

## Activity Repair Contract

The first Activity implementation work is the response contract, followed by repair wiring. It must define the production repair trigger before any large UI polish work.

Trigger semantics:

- For the selected instance/session date, the backend identifies runs active on that ET session date.
- The backend reads per-instance broker-activity WAL rows for the date.
- If a run has durable raw callbacks or legacy execution artifacts for that date but no corresponding authored Activity rows, the backend runs or reads an idempotent repair projection for that run.
- Reconstruction results are written to a separate derived repair projection/cache with provenance. Existing broker-activity WAL rows remain authoritative and are not overwritten.
- Activity GET must not append to the per-instance broker-activity WAL. Broker-WAL sequence ownership remains with the live broker-activity publisher.
- The repair projection/cache is guarded by a per-instance/run lock and a source artifact signature so concurrent first readers do not duplicate work.
- The projection must not read and rewrite Parquet artifacts on every poll. It needs a cheap skip path, such as existing-row keys plus source artifact signature or a repair marker, so repeated Activity refreshes are bounded.

Idempotency and merge rules:

- Broker fills dedupe by execution identity when available.
- Lifecycle rows dedupe by order identity plus lifecycle meaning.
- Reconstructed rows carry source run id, source sequence when known, recovery provenance, and recovery reason.
- Closed-trade summaries from `trades.parquet` use a separate stable identity from broker fill rows. They do not pretend to be IBKR executions.
- A closed-trade summary appears as its own `closed_trade_summary` row type/section and references its constituent fill visible-row ids. It is an economic round-trip summary, not another broker execution, and it must not be counted in broker-fill totals.

Visible row contract:

- Every visible row or folded group has `visible_row_id`.
- Rows that represent a single authored broker row carry the broker row sequence in technical details.
- Folded rows carry `fold_key`, `fold_count`, and the child evidence rows/ids.
- Structurally clustered rows carry `cluster_key` and `cluster_label` separately from fold fields.
- Closed-trade summary rows carry constituent fill references so the UI can show the relationship without double-counting.

## Event Narrative Registry Contract

The registry is closed over **cockpit-visible meanings**, not over every possible IBKR API callback string.

Required cataloging:

- Enumerate all request/callback/source names currently emitted by the IBKR API evidence recorder.
- Map known recorder events to trader meanings such as broker positions refreshed, open orders refreshed, executions refreshed, account summary refreshed, broker probe unavailable, and unmapped broker diagnostic.
- Fills/order lifecycle rows continue to use the broker-activity template path from ADR 0014.
- Unknown evidence events render as a backend-authored unmapped diagnostic with raw details only in expansion.

Coverage rule:

- Adding a new cockpit-visible event meaning requires a backend registry entry, tests, and copy.
- Adding a new raw recorder callback without a trader meaning must still hit the unmapped diagnostic path, not raw primary UI.

## Deploy Package Contract

Validated strategy packages are net-new backend-owned artifacts consumed by Deploy.

Minimum package fields:

- package id and display label;
- strategy key;
- strategy spec path and hash;
- audit copy path and hash;
- QC/cloud backtest id or equivalent validation id;
- golden fixture or parity evidence references;
- optional settings schema and approved/default settings values;
- deployable action-plan schema;
- validation status and validation failure codes;
- created_at_ms as `int64 ms UTC`.

Package immutability:

- A package version is immutable by content hash.
- Referenced specs, audit copies, fixtures, and validation artifacts are stored as path plus hash.
- If any referenced artifact is missing or its hash differs at deploy/redeploy time, Deploy fails closed with a trader-readable package-evidence-drift message.
- Existing/running bot evidence is not rewritten when package evidence drifts; the drift blocks new deploy/redeploy until a new package version is created.

Package creation inputs:

- selected strategy;
- selected or generated strategy spec;
- selected validation evidence from existing fixture/backtest/audit catalogs;
- optional package settings values when the package schema requires them;
- selected action-plan schema or instrument plan.

Storage decision:

- The first implementation may use the repo's existing artifact substrate for a package registry. The PRD does not require a database. The registry must still be backend-owned and exposed through typed APIs so Deploy consumes package objects rather than loose raw path fields.

Validation assertions:

- referenced files exist and are confined to the repo where applicable;
- hashes are captured at package creation;
- hashes are rechecked at deploy/redeploy and mismatches fail closed;
- validation ids are present and attached to the package;
- fixture/parity references exist or the package is not deployable;
- settings satisfy the package schema;
- package status is deployable before a bot can be created from it.

Mapping from today's fields:

- `strategy_spec_path` becomes package provenance, not a normal text input.
- `qc_audit_copy_path` becomes package provenance, not a normal text input.
- `qc_cloud_backtest_id` becomes package validation evidence, not a standalone deploy field.
- `account_id` is removed from user entry and sourced from connected broker account evidence.
- `strategy_instance_id` remains the final bot name.

## Verticalization

Do not implement this PRD as one branch. Split it into two independent epics.

### Epic A — Activity truth, narratives, and stability

1. **A0: Activity projection response contract.** Define the full response schema once: `visible_row_id`, row type, `cluster_key`, `cluster_label`, `fold_key`, `fold_count`, provenance, child evidence references, constituent fill references, and all timestamp fields as `int64 ms UTC`. A1/A1b/A2/A3 extend this stable interface instead of changing it independently.
2. **A1: Execution-fill repair wiring.** Production repair trigger, separate repair projection/cache, lock/idempotency, source artifact signature, JUNE-25-style fixture, execution reconstruction from callbacks/`executions.parquet`, and provenance. No broker-activity WAL writes from Activity GET.
3. **A1b: Closed-trade summary projection.** `trades.parquet` summary rows, distinct `closed_trade_summary` row type/section, constituent fill references, no double-counting with broker fills.
4. **A2: Evidence narrative registry.** Catalog current evidence recorder events, backend-authored labels/explanations, unmapped diagnostic fallback, per-surface raw-code tests for Activity.
5. **A3: Backend cluster/fold contract.** `visible_row_id`, `cluster_key`, `cluster_label`, `fold_key`, `fold_count`, child evidence details; migrate away from Angular-derived grouping.
6. **A4: Frontend incremental merge.** Signal-backed row store keyed by `visible_row_id`; preserve expansion state and avoid full-table replacement. `resource()` may load raw responses, but it must not be bound directly to table replacement when no-flash behavior is required.
7. **A5: Incidents, Audit, Configuration, and Broker API Evidence polish.** Apply the same narrative, fold, ET display, and theme-token rules per surface with per-surface acceptance tests. Keep each PR to one narrow surface unless the shared response/schema slice explicitly owns the shared change.

### Epic B — Deploy validated packages

1. **B1: Package registry contract.** Backend schema, storage, validation assertions, package creation/selection API, and mapping from today's raw deploy fields.
2. **B2: Deploy form package flow.** Bot-name prefill and lifetime uniqueness, package selector/creator, display-only connected account, fail-closed account behavior, package settings controls.
3. **B3: Instrument pickers and UI modernization.** Trader-readable stock/option pickers, PrimeNG forms/panels/badges/dialogs within the owned Deploy surface, app theme-token styling.

Cross-cutting gates such as "no raw codes in primary UI" are scoped per surface and per slice. A slice passes when its owned surfaces are clean; the full PRD passes when all named surfaces are clean.

## Testing Decisions

The highest-value test seams are:

1. **Activity projection contract seam.** Test the backend Activity projection from external inputs to response shape. This seam should prove that live broker rows, reconstructed callback rows, legacy execution artifact rows, and closed-trade summary rows all produce trader-readable Activity rows with `int64 ms UTC` timestamp fields, fold/cluster metadata, stable `visible_row_id`, provenance, and no raw primary codes.
2. **Deploy package contract seam.** Test the deploy package creation/selection boundary from package inputs and connected broker account evidence to deploy-ready payload or trader-readable validation failure.
3. **Bot Cockpit render seam.** Test the Angular/PrimeNG components through rendered behavior: no raw codes in primary UI, fold badges render, expansion shows individual details, ET time is displayed, and refresh preserves visible state.
4. **End-to-end live-session seam.** Use Playwright or equivalent browser-level tests against mocked/replayed backend responses for Activity and Deploy. Use the next real market bot run as post-merge manual/operational validation that a real fill appears in Activity without manual reconstruction.

Good tests should assert external behavior and contracts, not implementation details. Backend tests should call projection/composer/package boundaries rather than private helpers. Frontend tests should assert rendered labels, badges, expanded details, stable row identity, and absence of raw codes. E2E tests should assert what the trader sees.

No-flash invariant:

- On a poll that adds no usable row and does not change a visible fold count, existing table row DOM nodes are not destroyed/recreated.
- `@for` tracks by backend-authored `visible_row_id`.
- Expanded rows/panels remain expanded across refresh.
- Tests should assert node identity and expansion persistence, not pixel flashing.

Prior art exists in the cockpit component specs, broker activity service tests, broker activity truthfulness tests, live instance router tests, operator notice tests, and Playwright cockpit scenarios. New tests should extend those patterns rather than inventing a second test style.

Acceptance gates:

- A backend projection test proves a JUNE-25-style recorded fixture with `executions.parquet` and no per-instance broker activity WAL produces repaired Activity fill rows in CI.
- A backend projection test proves a recorded fixture with `trades.parquet` produces closed-trade summary rows with constituent fill references in CI.
- A frontend render test proves repaired fill rows and closed-trade summary rows render in the correct Activity sections without double-counting fills.
- A replayed recent-stream fill fixture appears in Activity without manual reconstruction in CI.
- A fresh live-paper fill from the next market-session bot run is used as post-merge operational confirmation, not as a PR merge blocker.
- No primary Bot Cockpit Activity surface renders raw event/type codes where trader language should appear in the Activity slices; the same gate is repeated per surface for Incident, Audit, Broker API Evidence, and Deploy validation slices.
- Repeated consecutive broker evidence and incidents fold with backend-authored count badges.
- Activity refresh does not replace the whole visible table when no usable row changed.
- All primary market/session times are displayed in ET while technical details retain canonical UTC millisecond evidence.
- Deploy can create/select a validated package, use bot name as identity, display connected account read-only, and avoid raw settings path inputs unless shown as technical provenance.
- Deploy refuses reuse of any historical bot name / `strategy_instance_id`, not only currently active names.
- Deploy/redeploy fails closed with a trader-readable package-evidence-drift message when package artifact hashes no longer match.
- PrimeNG components and app theme tokens are used for ordinary tables/panels/forms/badges where replacement is straightforward within the slice's owned surface.

## Out of Scope

- Live-money trading enablement.
- Changing strategy alpha logic or mathematical validation tolerances.
- Replacing Apache ECharts charts that already work.
- Internationalization of trader-facing copy.
- Full redesign of every cockpit tab outside Activity, Audit, Configuration, Broker API Evidence, Recent Incidents, and Deploy surfaces named here.
- Opportunistic PrimeNG rewrites outside the surface owned by the current slice. Each UI modernization PR should name one narrow surface and leave unrelated bespoke controls alone.
- Deleting historical raw artifacts or forensic data.
- Moving validated strategy package creation to Engine Lab.
- Building a new issue breakdown; that belongs to a later to-issues pass.

## Further Notes

This PRD follows the decisions captured in ADR 0016 and the glossary entries added to `CONTEXT.md` during the grilling session.

External wording references used for framing:

- Interactive Brokers Client Portal and Account Window documentation use trader nouns such as account, portfolio, positions, trades, balances, margin, and available-for-trading.
- Nielsen Norman Group guidance recommends plain language, precise diagnosis, constructive next steps, and hiding/minimizing obscure codes except for diagnostics.

The PRD should be reviewed by Claude before implementation. The review should focus on whether the seams are high enough, whether the slices can be verticalized, and whether any frontend responsibility still implies deriving backend-owned meaning.
