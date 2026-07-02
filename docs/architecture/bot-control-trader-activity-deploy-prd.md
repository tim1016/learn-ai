# PRD: Bot Control Trader Activity and Deploy Strategy Packages

**Status:** Published for implementation
**Created:** 2026-06-25
**Published issue:** #689
**Hardened after review:** 2026-06-25
**Owner:** Inkant
**Architectural anchor:** ADR 0016 — Bot Control trader-authored activity and deploy packages
**Related ADRs:** ADR 0013, ADR 0014, ADR 0015
**Primary objective:** Make Bot Control and Deploy a strategy trader-readable, durable-trade-visible, backend-authored, and stable under refresh.

## Problem Statement

Traders using Bot Control cannot reliably answer the most important live-paper questions: what trades happened, what broker evidence means, what incidents repeated, what configuration the bot actually ran with, and whether the deploy form is asking for meaningful decisions or implementation details.

The most urgent failure is trade visibility. The JUNE-25 bot has durable trade evidence in its run artifacts, but the Activity table does not show the trades because the current Activity projection depends on a per-instance broker activity stream that may not have populated after recent changes. A trader should never see an empty Activity table when fills or closed-trade summaries exist in durable evidence.

The second failure is language. Bot Control currently leaks code-shaped terms such as `endpoint_snapshot`, raw JSON, `spec_path`, account-source values, and request/callback names into primary UI. These strings may be useful to engineers in a drill-down, but they do not help a trader understand broker activity, audit evidence, or incidents.

The third failure is refresh behavior. Activity and evidence tables can flash or redraw during local file/projection refresh even when no usable trader row changed. This makes the page feel unstable and erodes trust in live monitoring.

The fourth failure is deploy complexity. Deploy a strategy asks the trader to assemble raw strategy paths, settings files, account identifiers, backtest IDs, audit copy paths, and action-plan primitives. The trader should create or select a validated strategy package, name the bot, review connected broker account evidence, choose sizing/action instruments, and deploy.

## Solution

Ship a Bot Control and Deploy overhaul grounded in ADR 0016.

Bot Control Activity will be backed by a backend-authored activity projection that always surfaces durable trade evidence. Broker activity remains the preferred live authority, but when the per-instance broker activity stream is missing or stale, the backend repairs or supplements the projection from raw broker callbacks or legacy run artifacts. This repair/supplement path is net-new production wiring: existing reconstruction code is test-covered but not called by the production Activity projection.

The trader sees broker fills and closed-trade summaries in Activity without manual reconstruction. Fill reconstruction is sourced from raw callback WALs or `executions.parquet`; closed-trade summaries require a new `trades.parquet` projection path because the existing reconstruction module reads execution rows only.

All trader-facing evidence rows, incident panels, audit summaries, and deploy validation summaries will be authored by the backend through a closed event narrative registry. The frontend renders finished labels, explanations, severities, fold counts, and diagnostic facts. Angular may format layout, ET display time, numbers, PrimeNG badges, expansion state, and chart rendering, but it does not infer event meaning or repeated-row sameness.

Activity and incidents will fold repeated consecutive phenomena using backend-authored fold keys and counts. Activity will also use backend-authored structural cluster keys for order/fill grouping; clustering and duplicate folding are different concepts. The visible table will use incremental merge behavior keyed by a backend-authored `visible_row_id` so the parent Activity tab remains mounted and the table only highlights or visibly changes when a usable row is added or a visible fold count changes.

Audit and Configuration will be separated: Configuration shows what the bot was intended and configured to run with; Audit shows evidence of what happened and whether that evidence supports the intended configuration. Raw JSON and exact codes move to technical details.

Deploy a strategy will create or select validated strategy packages through a backend-owned package registry. The package registry is net-new: today's deploy form still submits loose raw fields. The final bot name is the durable, system-safe `strategy_instance_id`; unrelated bots cannot reuse an existing identity, while recovery redeploys continue the same identity through explicit parent lineage. The broker account is display-only evidence from the connected broker session; if it is unavailable or ambiguous, Deploy fails closed. Strategy settings appear only when the selected package requires trader-tunable controls. Stock and option action-plan selection uses rich trader-readable pickers. PrimeNG components and app theme tokens are the default UI vocabulary, with Apache ECharts retained for charts.

## Grounded Implementation Gaps

This PRD depends on several pieces that are not production-wired today. They are first-class scope, not assumed infrastructure.

1. **Activity repair trigger.** `broker_activity_reconstruction.py::reconstruct_broker_activity_for_run()` exists and is test-covered, but no production route or projection calls it. The Activity repair slice must add trigger semantics, idempotency, and cost controls without making Activity GET append to the broker-activity WAL.
2. **Closed-trade summary path.** Existing reconstruction reads `executions.parquet`; it does not read `trades.parquet`. Closed-trade history needs a separate projection or explicit pairing from execution fills.
3. **Evidence narrative registry for diagnostics.** Broker fills already use backend templates; endpoint/evidence rows such as `endpoint_snapshot` are currently open diagnostics from the IBKR evidence recorder. The registry slice must catalog known recorder request/callback names and define an unmapped diagnostic fallback.
4. **Backend cluster and fold identities.** Current Angular groups activity rows by `perm_id`/`exec_id`. The backend must author both structural clustering (`cluster_key`, `cluster_label`) and duplicate folding (`fold_key`, `fold_count`) before the frontend can stop deriving groups.
5. **Stable visible row identity.** The anti-flash UI cannot be implemented until the backend supplies a stable `visible_row_id` for every rendered row or folded group.
6. **Deploy package registry.** No package registry exists today. The package slice must define package storage, validation assertions, and how today's raw deploy fields map into a package.
7. **Broker position evidence source.** Activity `position_snapshot` is currently `source="unavailable"` in the projection. Trader-readable position evidence depends on wiring a real broker position snapshot feed or rendering an explicit "not captured" narrative.

## Current Bot Control Business Logic For Review

This section documents the current implementation model as of 2026-06-26. It is intentionally written as a precise business-logic contract for peer review by other reasoning systems. It describes what the bot control page currently means by "bot exists", "bot is alive", "bot died", "bot is retired", and "bot can be acted on". It is separate from the proposed Activity and Deploy improvements above.

### Core entities and evidence sources

1. A **bot identity** is `strategy_instance_id`. It is the durable bot control identity, the broker attribution namespace component, and the grouping key for historical runs.
2. A **run** is a content-addressed run directory under `live_runs/<run_id>`, with `run_ledger.json` as the durable deployment evidence. Multiple runs may belong to one `strategy_instance_id`.
3. A **live binding** exists only when the host daemon reports a process for the instance and that process is in a live state with a visible `run_id`. The bot control page does not infer liveness from the latest run directory alone.
4. **Evidence binding** points to the latest known run for an instance, even when no live process is bound. This lets the bot control page show last decisions, last exit, provenance, and artifacts after a process exits.
5. **Desired state** is durable operator intent: `RUNNING`, `PAUSED`, or `STOPPED`. It is not the same as actual process state.
6. **Process state** is daemon-observed runtime state: `RUNNING`, `STOPPING`, `EXITED`, `IDLE`, `WAITING_FOR_HOST`, or `UNREACHABLE` after projection.
7. **Readiness** is a backend verdict over gates such as broker connection, paper safety, reconcile receipt, runtime freshness, and configuration.
8. **Operator surface** is the backend-authored bot control projection that composes process state, desired state, readiness, broker evidence, reconciliation, mutation evidence, action capabilities, and notices. Frontend renders this projection; it must not invent operational verdicts.

### Bot creation and start lifecycle

1. Deploy creates a new run ledger by forwarding to the host daemon. The daemon owns git-clean checks, content hashing, and run directory creation.
2. The data-plane deploy endpoint derives account identity from the connected broker account. If the connected account is unavailable or mismatches a legacy client-supplied account hint, deploy fails closed.
3. The run ledger records `strategy_instance_id`, `strategy_key`, strategy spec path/hash, audit copy path/hash, account id, live config, sizing provenance, and action plan.
4. Starting a run is a separate operation. The bot control page may show Start enabled only when the operator surface says it is enabled, but the data-plane `start_run` endpoint rechecks the same gates immediately before forwarding to the daemon.
5. Start is blocked if:
   - the run has `poisoned.flag`;
   - durable desired state is `STOPPED`;
   - the daemon already reports `running`;
   - the daemon reports `stopping`;
   - the daemon is unreachable;
   - required start settings are incomplete.
6. If Start is accepted, the host daemon spawns `python -m app.engine.live.run start ...` and records process metadata.
7. After successful start, broker-activity publisher registration is attempted. Registration failure does not roll back the process start; lazy bootstrap may retry when Activity is read.

### Cold-start reconciliation lifecycle

1. Before a live runner may enter its bar loop and submit new orders, it must run cold-start reconciliation and write `reconciliation_receipt.json`.
2. The orchestrator writes an `in_progress` receipt first so a crash cannot leave a stale `passed` receipt from an earlier boot.
3. The reconciler reads:
   - stable live-state sidecar;
   - current run intent WAL;
   - prior run unresolved intent tail, when available;
   - prior emergency-flatten audit, when available;
   - broker open orders and executions, synchronized from IBKR.
4. The classifier returns one of:
   - `Continue`: broker state matches known projection and allowed namespaces.
   - `Adopt`: broker confirms an owned but unresolved/orphaned order. The run records adoption and may pause when exposure is ambiguous.
   - `Poison`: broker or artifact state cannot be proven safe.
5. Poison reasons include sidecar corruption, WAL corruption, broker probe failure, missing or unparseable order references, foreign or unknown namespaces, and foreign broker ids.
6. Unknown namespace executions are normally poison. The 2026-06-26 fleet-reset fix allows a specifically listed fresh deployment to ignore historical unknown completed executions only when all are at or before a flat-account baseline, account positions were empty, open orders were empty, and the new `strategy_instance_id` is explicitly listed in the fleet baseline. Unknown open orders are never ignored.
7. No submit is allowed without a durable final receipt. A failed receipt write is fatal.

### Alive, paused, stopped, dead, and retired semantics

The bot control page should use these meanings consistently:

| Term | Current source of truth | Meaning | Can resume without redeploy? |
|---|---|---|---|
| Alive / running | Daemon process state projected as `RUNNING` with live binding | A host child process is active for this instance. It may be trading, paused, waiting for bars, or waiting for signals. | Already running |
| Trading-enabled | `RUNNING` process plus desired state `RUNNING`, broker/readiness gates acceptable, runtime not demoted | The engine is permitted to process strategy decisions and submit orders when the strategy emits actionable signals. | Yes |
| Paused | Durable desired state `PAUSED`; optionally a live process still exists | The bot should not open new positions. Existing live process can still drain commands such as flatten/reconcile. | Yes, via Resume if guards pass |
| Flatten-and-pause | Composed endpoint: first persist `PAUSED`, then enqueue `FLATTEN_NOW` if a live binding exists | Prevent re-entry first, then flatten owned exposure. If command enqueue fails, durable PAUSE remains. | Yes, after operator review |
| Exited | Daemon/process sidecar has terminal `ended_at_ms` or `exit_code`; no live binding | The process ended. This may be clean, operator-driven, exception, max-order halt, or fatal halt. | Maybe; blocked if poisoned or STOPPED |
| Dead | Informal synonym for terminal process exit | A process died when it transitioned out of running into a terminal sidecar/daemon state. Death does not always mean poison. | Depends on exit reason and flags |
| Poisoned / retired run | `poisoned.flag` exists or `last_exit.halt_trigger` resolves to a poisoned halt | The run is permanently unsafe to resume. Start is blocked with redeploy-required semantics. | No; redeploy required |
| Permanently stopped instance | Durable desired state `STOPPED` | The instance is retired through operator intent; Start is blocked. | No; redeploy required |
| Host unreachable | Daemon fetch fails or projection maps to `UNREACHABLE` | Bot Control cannot prove process state. This is unknown, not proof of death. | Not until host service returns |
| Waiting for host | Desired state `RUNNING`, daemon reachable, no tracked subprocess | Operator requested trading but process is not active. | Start process |

Important distinction: a bot can be **alive but not currently in a trade**. A running deployment-validation bot that emits `HOLD` and has no position is alive and waiting, not dead.

### Runtime command semantics

1. `PAUSE`, `RESUME`, and `STOP` are durable desired-state writes, not one-shot run commands.
2. If a live binding exists and its run directory is visible, the desired-state endpoint also enqueues the corresponding command for the live engine to acknowledge.
3. If no live binding exists, desired-state writes are durable-only and affect the next start.
4. `FLATTEN`, `RECONCILE`, and `MARK_POISONED` are one-shot commands that require a live binding, except account-wide `emergency-flatten`.
5. `flatten-and-pause` is the only normal bot control path that composes durable `PAUSED` with one-shot `FLATTEN_NOW`.
6. `emergency-flatten` is account-wide, paper-guarded, daemon-mediated, and independent of a live binding. It is for recovery after the binding is gone or unsafe.
7. Runtime reconcile requires a live engine because the engine must acquire the submit lock, probe the broker, run reconciliation, and write receipt/ack evidence.
8. Mutation reconciliation is read-only. It classifies whether a previously ambiguous bot control mutation likely took effect; it never replays the original mutation.

### Trade and signal semantics for deployment-validation bots

The deployment-validation strategy is a lifecycle-validation strategy, not an alpha model. Its canonical implementation is `DeploymentValidationConsecutiveGreen`.

1. It consumes 1-minute bars.
2. It starts detection at 09:45 ET.
3. While flat and not entry-pending, two consecutive green minute bars (`close > open`) emit `ENTER`.
4. The live action plan converts `ENTER` into a fixed one-share market buy for the configured instrument.
5. After entry fill, it counts three bars and emits `EXIT`.
6. The live action plan converts `EXIT` into close-leg / market sell.
7. At or after 15:45 ET, any open/pending position is liquidated and the bot stops detecting new entries for the day.
8. Different symbols using the same deployment-validation strategy naturally emit different signals because each symbol has its own minute-bar stream.

### Failure classes reviewers should challenge

1. **Fatal halt with poison:** the process exits and writes `poisoned.flag`. Current business rule blocks restart and requires redeploy.
2. **Fatal halt without poison:** the process exits non-zero but does not mark the run poisoned. Current bot control classifies prior run as error/halt depending on sidecar fields and may allow Start unless other guards block.
3. **Exception exit:** runtime broker/IO or process error. Current bot control exposes `EXITED_WITH_ERROR`; recovery depends on start guards, broker state, and reconciliation.
4. **Max-orders exceeded:** order cap guard terminates the run to prevent runaway submits.
5. **Operator clean stop / keyboard interrupt:** terminal but not poison by itself.
6. **Daemon unreachable:** not a death assertion. It is inability to observe; bot control page must degrade actions and tell the operator not to trust liveness until the daemon is reachable.
7. **Unknown broker state:** fail closed at reconciliation. Do not permit submits when broker positions, open orders, executions, or ownership cannot be proven.

## User Stories

1. As a trader, I want the Bot Control Activity tab to show every broker fill that happened, so that I can trust the bot control page during a live-paper session.
2. As a trader, I want closed-trade summaries to appear in Activity when `trades.parquet` exists, so that a missing broker activity stream does not hide real trade history.
3. As a trader, I want the Activity table to explain where a reconstructed row came from, so that I can distinguish live broker capture from repaired historical evidence when needed.
4. As a trader, I want Activity rows to use plain trading language, so that I do not need to interpret API callback names.
5. As a trader, I want raw event codes to be hidden in technical details, so that the primary table stays readable.
6. As a trader, I want `endpoint_snapshot`-style rows to say what broker evidence was refreshed, so that I know whether the bot checked positions, executions, open orders, or account state.
7. As a trader, I want broker account position evidence to be summarized in trading language when a real broker position snapshot is captured, and to see a clear "not captured" explanation when it is unavailable.
8. As a trader, I want account-source evidence to explain where the account value came from, so that I know whether it was observed from the broker session or loaded from saved config.
9. As a trader, I want all primary market/session times shown in ET, so that I can match the bot control page to U.S. market hours and broker session context.
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
32. As a trader, I want Deploy a strategy to prefill a random bot name candidate, so that I can start from a sensible system-safe identity.
33. As a trader, I want to edit the bot name before deploy, so that the Bot Control identity is meaningful to me.
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
53. As a trader, I want PrimeNG tables, panels, accordions, badges, forms, and dialogs used consistently, so that the bot control page feels stable and accessible.
54. As a trader, I want existing charts to keep using Apache ECharts where appropriate, so that working chart behavior is not reinvented.
55. As an engineer, I want bespoke UI replaced by PrimeNG only within the narrow surface owned by the current slice, so that future UI changes are less fragile without diff sprawl.
56. As a reviewer, I want backend narrative coverage tests proving every currently known recorder event code maps to a registry outcome and unknown recorder codes route to the unmapped-diagnostic fallback, so that no raw event code leaks into primary UI.
57. As a reviewer, I want frontend tests to assert rendered trader language rather than private component state, so that tests match user behavior.
58. As a reviewer, I want frontend no-op refresh tests to preserve row DOM identity and expansion state, so that Activity no-flash regressions are caught without flaky pixel assertions.
59. As Claude reviewing the PRD, I want clear implementation and testing seams, so that I can challenge scope before coding starts.
60. As an implementation agent, I want the PRD broken into vertical slices later, so that each slice can be independently verified.
61. As an engineer, I want the CI path to use recorded/replayed fill fixtures, so that implementation is not blocked on waiting for a future market session.
62. As an engineer, I want Deploy's removal of manual broker-account entry called out as an intentional behavior change, so that redeploy behavior during broker outages is reviewed deliberately.
63. As a trader, I want closed-trade summaries visually tied to their constituent fills when the backend has a reliable join key, so that I do not double-count a round trip as extra broker activity.
64. As an engineer, I want package evidence drift to fail closed at deploy/redeploy, so that a package cannot silently point at changed validation artifacts.

## Implementation Decisions

1. The canonical trader-facing product term is **Bot Control**.
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
25. Deploy may prefill a random editable bot name, but the final identity is system-safe and cannot collide with an unrelated historical bot.
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
- A closed-trade summary appears as its own `closed_trade_summary` row type/section. It references constituent fill visible-row ids only when the backend has a reliable join key. It is an economic round-trip summary, not another broker execution, and it must not be counted in broker-fill totals.

Visible row contract:

- Every visible row or folded group has `visible_row_id`.
- Rows that represent a single authored broker row carry the broker row sequence in technical details.
- Folded rows carry `fold_key`, `fold_count`, and the child evidence rows/ids.
- Structurally clustered rows carry `cluster_key` and `cluster_label` separately from fold fields.
- Closed-trade summary rows may carry constituent fill references when the backend can prove the relationship without time-window guessing.

## Event Narrative Registry Contract

The registry is closed over **bot-control-visible meanings**, not over every possible IBKR API callback string.

Required cataloging:

- Enumerate all request/callback/source names currently emitted by the IBKR API evidence recorder.
- Map known recorder events to trader meanings such as broker positions refreshed, open orders refreshed, executions refreshed, account summary refreshed, broker probe unavailable, and unmapped broker diagnostic.
- Fills/order lifecycle rows continue to use the broker-activity template path from ADR 0014.
- Unknown evidence events render as a backend-authored unmapped diagnostic with raw details only in expansion.

Coverage rule:

- Adding a new bot-control-visible event meaning requires a backend registry entry, tests, and copy.
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

1. **A0: Activity projection response contract.** Define the full response schema once: `visible_row_id`, row type, `cluster_key`, `cluster_label`, `fold_key`, `fold_count`, provenance, child evidence references, optional constituent fill references, and all timestamp fields as `int64 ms UTC`. A1/A1b/A2/A3 extend this stable interface instead of changing it independently.
2. **A1: Execution-fill repair wiring.** Production repair trigger, separate repair projection/cache, lock/idempotency, source artifact signature, JUNE-25-style fixture, execution reconstruction from callbacks/`executions.parquet`, and provenance. No broker-activity WAL writes from Activity GET.
3. **A1b: Closed-trade summary projection.** `trades.parquet` summary rows, distinct `closed_trade_summary` row type/section, optional proven constituent fill references, no double-counting with broker fills.
4. **A2: Evidence narrative registry.** Catalog current evidence recorder events, backend-authored labels/explanations, unmapped diagnostic fallback, per-surface raw-code tests for Activity.
5. **A3: Backend cluster/fold contract.** `visible_row_id`, `cluster_key`, `cluster_label`, `fold_key`, `fold_count`, child evidence details; migrate away from Angular-derived grouping.
6. **A4: Frontend incremental merge.** Signal-backed row store keyed by `visible_row_id`; preserve expansion state and avoid full-table replacement. `resource()` may load raw responses, but it must not be bound directly to table replacement when no-flash behavior is required.
7. **A5: Incidents, Audit, Configuration, and Broker API Evidence polish.** Apply the same narrative, fold, ET display, and theme-token rules per surface with per-surface acceptance tests. Keep each PR to one narrow surface unless the shared response/schema slice explicitly owns the shared change.

### Epic B — Deploy validated packages

1. **B1: Package registry contract.** Backend schema, storage, validation assertions, package creation/selection API, and mapping from today's raw deploy fields.
2. **B2: Deploy form package flow.** Bot-name prefill, unrelated-identity collision protection, recovery redeploy lineage, package selector/creator, display-only connected account, fail-closed account behavior, package settings controls.
3. **B3: Instrument pickers and UI modernization.** Trader-readable stock/option pickers, PrimeNG forms/panels/badges/dialogs within the owned Deploy surface, app theme-token styling.

Cross-cutting gates such as "no raw codes in primary UI" are scoped per surface and per slice. A slice passes when its owned surfaces are clean; the full PRD passes when all named surfaces are clean.

## Testing Decisions

The highest-value test seams are:

1. **Activity projection contract seam.** Test the backend Activity projection from external inputs to response shape. This seam should prove that live broker rows, reconstructed callback rows, legacy execution artifact rows, and closed-trade summary rows all produce trader-readable Activity rows with `int64 ms UTC` timestamp fields, fold/cluster metadata, stable `visible_row_id`, provenance, and no raw primary codes.
2. **Deploy package contract seam.** Test the deploy package creation/selection boundary from package inputs and connected broker account evidence to deploy-ready payload or trader-readable validation failure.
3. **Bot Control render seam.** Test the Angular/PrimeNG components through rendered behavior: no raw codes in primary UI, fold badges render, expansion shows individual details, ET time is displayed, and refresh preserves visible state.
4. **End-to-end live-session seam.** Use Playwright or equivalent browser-level tests against mocked/replayed backend responses for Activity and Deploy. Use the next real market bot run as post-merge manual/operational validation that a real fill appears in Activity without manual reconstruction.

Good tests should assert external behavior and contracts, not implementation details. Backend tests should call projection/composer/package boundaries rather than private helpers. Frontend tests should assert rendered labels, badges, expanded details, stable row identity, and absence of raw codes. E2E tests should assert what the trader sees.

No-flash invariant:

- On a poll that adds no usable row and does not change a visible fold count, existing table row DOM nodes are not destroyed/recreated.
- `@for` tracks by backend-authored `visible_row_id`.
- Expanded rows/panels remain expanded across refresh.
- Tests should assert node identity and expansion persistence, not pixel flashing.

Prior art exists in the bot control component specs, broker activity service tests, broker activity truthfulness tests, live instance router tests, operator notice tests, and Playwright bot control scenarios. New tests should extend those patterns rather than inventing a second test style.

Acceptance gates:

- A backend projection test proves a JUNE-25-style recorded fixture with `executions.parquet` and no per-instance broker activity WAL produces repaired Activity fill rows in CI.
- A backend projection test proves a recorded fixture with `trades.parquet` produces closed-trade summary rows in CI without inferring constituent fill references from time windows.
- A frontend render test proves repaired fill rows and closed-trade summary rows render in the correct Activity sections without double-counting fills.
- A replayed recent-stream fill fixture appears in Activity without manual reconstruction in CI.
- A fresh live-paper fill from the next market-session bot run is used as post-merge operational confirmation, not as a PR merge blocker.
- No primary Bot Control Activity surface renders raw event/type codes where trader language should appear in the Activity slices; the same gate is repeated per surface for Incident, Audit, Broker API Evidence, and Deploy validation slices.
- Repeated consecutive broker evidence and incidents fold with backend-authored count badges.
- Activity refresh does not replace the whole visible table when no usable row changed.
- All primary market/session times are displayed in ET while technical details retain canonical UTC millisecond evidence.
- Deploy can create/select a validated package, use bot name as identity, display connected account read-only, and avoid raw settings path inputs unless shown as technical provenance.
- Deploy refuses reuse of any historical bot name / `strategy_instance_id` for unrelated bots, not only currently active names, while allowing explicit same-instance recovery redeploys from a parent run.
- Deploy/redeploy fails closed with a trader-readable package-evidence-drift message when package artifact hashes no longer match.
- PrimeNG components and app theme tokens are used for ordinary tables/panels/forms/badges where replacement is straightforward within the slice's owned surface.

## Observed Evidence: 2026-06-26 Paper Session

This section records the local artifact evidence observed on 2026-06-26 so reviewers can compare the business logic above against real bot control outcomes. It is not a normative requirement by itself; it is an incident/data appendix.

### Mode-separated run summary

Artifact scan source: `PythonDataService/artifacts/live_runs/*/{run_ledger.json,run_status.json,reconciliation_receipt.json,poisoned.flag,decisions.parquet,live.log}`.

Today-specific terminal/running evidence:

| Category | Count | Notes |
|---|---:|---|
| Real runtime crash/death requiring diagnosis | 1 | `JUN26TSLA` reconciled clean, traded, then fatal-halted after watchdog/control-plane lease loss and an unprovable submit during flatten. |
| Start-gate refusal due to dirty account position | 1 | `JUN26GM` never reached reconciliation; it halted immediately because the account still held a TSLA position while GM was the expected symbol. |
| Safety-system poison refusals | 4 | The first AAPL/SPY/TSLA/DIA redeploy attempt wrote `poisoned.flag` for each bot before any new order submission. This is fail-closed behavior, not a strategy crash. |
| Currently running four-symbol deployment-validation bots | 4 | AAPL, SPY, TSLA, DIA all have daemon state `running` and clean reconciliation receipts. |
| Current open positions in latest portfolio evidence | 0 | Latest portfolio update in each live log shows `position=0.0` for AAPL, SPY, TSLA, and DIA. |

Mode-separated rows from today:

| Created UTC | Strategy instance | Symbol | Run id prefix | Mode | Exit reason | Poison? | Reconciliation |
|---|---|---|---|---|---|---|---|
| 2026-06-26T13:52:03Z | `JUN26TSLA` | TSLA | `3c7bde89` | Runtime crash/death | `fatal_halt` | No | passed / clean |
| 2026-06-26T15:12:09Z | `JUN26GM` | GM | `575fca6b` | Start-gate refusal | `fatal_halt` | No | none |
| 2026-06-26T18:58:54Z | `DEPVAL-AAPL-20260626` | AAPL | `93e5505d` | Safety-system poison refusal | `fatal_halt` | Yes | failed |
| 2026-06-26T18:58:54Z | `DEPVAL-SPY-20260626` | SPY | `490e84c1` | Safety-system poison refusal | `fatal_halt` | Yes | failed |
| 2026-06-26T18:58:54Z | `DEPVAL-TSLA-20260626` | TSLA | `67bf40c3` | Safety-system poison refusal | `fatal_halt` | Yes | failed |
| 2026-06-26T18:58:54Z | `DEPVAL-DIA-20260626` | DIA | `4fe11e27` | Safety-system poison refusal | `fatal_halt` | Yes | failed |

### Genuine runtime crash: `JUN26TSLA`

`JUN26TSLA` is the only confirmed runtime death in this evidence set. It was not a cold-start poison case:

1. Cold-start reconciliation passed with `outcome=clean`.
2. The bot traded TSLA repeatedly.
3. At 2026-06-26T14:50:05Z the child watchdog entered `SUSPECTED_LOSS` because the control-plane lease expired.
4. At 2026-06-26T14:50:10Z the watchdog recorded `CONTROL_PLANE_LEASE_LOST`, blocked submissions, persisted paused state, and requested flatten.
5. Flatten did not complete within 20 seconds.
6. The watchdog disconnected the broker and requested engine exit.
7. The engine then attempted shutdown flatten, but `cancel_open_orders` failed because the IBKR client was already disconnected.
8. The final exception was `SubmitUncertainHaltError`: submit state was not provable after `broker.place_order` raised `NotConnectedError: IBKR client is not connected`.

This is the real crash to diagnose. The likely bug class is watchdog shutdown ordering / flatten timeout handling: the system disconnected the broker before the remaining flatten/cancel path could prove whether submit/flatten state was safe. The result was a non-poison fatal halt that left the account carrying TSLA exposure.

### Dirty-account start refusal: `JUN26GM`

`JUN26GM` did not crash while trading. It never got past the start gate:

```text
[START] HALT unexpected_position:
1 unexpected position(s): [{'symbol': 'TSLA', 'quantity': 1.0, 'reason': 'non_strategy_symbol'}]
(expected long-only GM; operator must reconcile the account before starting)
```

There is no reconciliation receipt because the unexpected-position gate runs before cold-start reconciliation. This halt was caused by the leftover TSLA position from the `JUN26TSLA` failure. It is a safety refusal in a dirty account, not an independent GM strategy crash.

The four poisoned DEPVAL runs all have:

```json
{
  "trigger": "cold_start_divergence",
  "details": {
    "reason": "unknown_namespace",
    "source": "reconciliation_orchestrator"
  }
}
```

Plain interpretation: the account had historical broker executions from namespaces that the newly deployed bots did not own. The cold-start reconciler could not prove those executions were safe to ignore, so it correctly refused the first redeploy attempt before any new order submission. These bots were blocked at the starting gate; they did not crash during trading.

### Compounding failure chain

The observed sequence was:

1. `JUN26TSLA` genuinely fatal-halted and left TSLA exposure.
2. `JUN26GM` used a fresh strategy id and a different expected symbol, then failed the unexpected-position gate because TSLA was still open.
3. The first DEPVAL fleet redeploy minted four new strategy ids. Their namespaces did not own the morning executions, so the cold-start reconciler poisoned all four with `unknown_namespace`.
4. After an explicit flatten/cancel/flat-account baseline, the fleet-reset redeploy succeeded and all four bots ran cleanly.

Business-logic implication: most later "deaths" were downstream safety refusals caused by deploying new namespaces into a dirty account. The bot control page should not collapse these into one "bot died" bucket.

### Fleet-reset redeploy evidence

The practical fix used today was a fleet-reset baseline: after the operator flattened the account, canceled open orders, and verified no positions/open orders, a baseline was recorded for the explicit four new strategy instances. The classifier may ignore only completed unknown-namespace executions at or before that baseline, and only for the listed strategy ids. Unknown open orders remain poison.

Current successful four-symbol run evidence:

| Strategy instance | Symbol | Current run id prefix | Reconciliation receipt | Process state |
|---|---|---|---|---|
| `DEPVAL-AAPL-20260626` | AAPL | `ccef6a3c` | passed / clean | running |
| `DEPVAL-SPY-20260626` | SPY | `5e9d9d10` | passed / clean | running |
| `DEPVAL-TSLA-20260626` | TSLA | `db9057fd` | passed / clean | running |
| `DEPVAL-DIA-20260626` | DIA | `fdd47e3e` | passed / clean | running |

### Current trade/signal summary for the four live bots

Latest decisions snapshot from the current runs:

| Bot | Decision rows | ENTER count | EXIT count | Latest signal | Current position evidence |
|---|---:|---:|---:|---|---|
| AAPL | 48 | 3 | 3 | HOLD | Flat |
| SPY | 48 | 4 | 4 | HOLD | Flat |
| TSLA | 48 | 3 | 3 | HOLD | Flat |
| DIA | 48 | 5 | 5 | HOLD | Flat |

Business-logic implication: these bots are alive and have traded, but at this snapshot they are out of the market. `HOLD` plus `position=0.0` means "waiting for the next entry condition", not "dead".

### Review questions raised by today's evidence

1. Should an operator-requested account flatten create an explicit first-class "fleet reset" workflow in Bot Control rather than a hidden artifact written by an engineer/operator tool?
2. Should the bot control page distinguish "fatal halt with no poison" from "fatal halt with poison" more visibly, since the recovery path differs?
3. Should `cold_start_divergence: unknown_namespace` explain "historical execution in another namespace" as the primary trader label, with raw namespace/order refs only in technical details?
4. Should a run that passes reconciliation and then fatal-halts for a non-poison reason be eligible for Start by default, or should every fatal halt require an explicit operator acknowledgement before Start?
5. Should the Activity tab show a lifecycle row for every bot death, even when no trade occurred, so deaths are counted in the same timeline as fills?
6. Should recovery redeploy default to reusing the same `strategy_instance_id` through parent lineage, and make minting a fresh id a deliberate "new bot, dirty-account risk" action?
7. Should Deploy refuse fresh-namespace bots unless the account is flat or the operator has run an explicit fleet-reset baseline workflow?
8. Should watchdog lease-loss flatten keep the broker connected until cancel/open-position proof is complete, or explicitly mark the outcome as unresolved and require emergency flatten before any further deploy?

## Out of Scope

- Live-money trading enablement.
- Changing strategy alpha logic or mathematical validation tolerances.
- Replacing Apache ECharts charts that already work.
- Internationalization of trader-facing copy.
- Full redesign of every bot control tab outside Activity, Audit, Configuration, Broker API Evidence, Recent Incidents, and Deploy surfaces named here.
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
