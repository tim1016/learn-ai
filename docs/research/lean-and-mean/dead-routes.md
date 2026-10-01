# Kill list: dead HTTP routes and schemas in the data service (#2706)

Part of the lean-and-mean map, #2700. **This plans the cuts and makes none.**

- **SHA:** `87b8e261021ec673c2e7c80448c0973bccd45378`, the map's charting SHA and `origin/master` when this was read.
- **Area:** `PythonDataService/app/routers/`, `app/schemas/`, `app/main.py`, `app/config.py`, and `contracts/openapi/python-data-service.openapi.json`.

## How the evidence was gathered

1. **Route inventory.** I imported `app.main` three times, once per fleet role (`combined`, which is what the contract export uses; `fleet_coordinator`; and `clerk_agent` with fault injection on), and listed every `APIRoute` with its handler's `file:line`. That gives 419 distinct method+path pairs. The 13 fleet routes scoped to a clerk come from the provider catalog. I dumped it with `production_provider_adapters()`: 101 operations, each with an operation id, a public path and an agent path.
2. **Caller search** covered the whole repo except tests. I left out `contracts/` and the generated `Frontend/src/app/api/broker.types.ts`, because both list every route. A route counts as reached if any of these hits it:
   - a literal path, with `{param}` matching `${…}`, in `Frontend/src`, `Backend/`, `scripts/`, `PythonDataService/scripts/`, compose files, CI workflows, `deploy/`, runbooks or `.claude/commands`;
   - a base-plus-suffix URL built inside one file (`base = '/api/x'` and then `` `${this.base}/y` ``);
   - a fleet operation id passed to `operationUrl(...)` in the Frontend. Dispatch is by name, so the agent route behind an operation is alive when the operation is;
   - an internal HTTP call from the data service itself (coordinator to agent, agent to coordinator).
3. **One level deeper on the Frontend.** For every Frontend call site I found its enclosing service method and checked that some non-spec file calls that method. A route whose only caller is an unused service method is dead.
4. **Orphan cascade.** I removed the handlers of the dead routes, then iterated: any module-level definition under `PythonDataService/app/` that live code no longer names (import lines don't count) becomes an orphan. Functions decorated as routes are excluded, because FastAPI dispatches them. I also checked the registries: `FEATURE_REGISTRY`, the edge `STRATEGY_REGISTRY` and the strategy registry's `/pine` hook.
5. **Test mapping.** I parsed every test file and matched each `test_*` against dead path literals and orphan names.

Matches that were only comments or docstrings (`app/routers/edge.py:5-12`, `app/engine/pine_generators.py:5`, `app/services/engine_bars_service.py:3`, `app/research/runs/window.py:7`, `app/services/lean_sidecar_compare_service.py:5`, `app/lean_sidecar/cross_runner.py:4`, and others) were confirmed by reading the line and do not count as callers. `PythonDataService/scripts/pr_shard_durations.json` lists test names, not callers.

## Kill list: routes

Column key: **OpenAPI** = the route is in the committed snapshot, so cutting it changes the contract. Every cut marked yes needs `export_openapi_contract.py` plus `npm run codegen:openapi` (`broker.types.ts`), and those PRs merge one at a time.

### Research, data and chart routes (data-plane core)

| Route | Handler | OpenAPI | Evidence |
|---|---|---|---|
| `GET /api/research/features` | `app/routers/research.py:651` `list_features` | yes | No caller anywhere. The Frontend research pages call only `run-*` routes, through the Backend (`Backend/Services/Implementation/ResearchService.cs:87-495`). |
| `GET /api/research/documentation` | `app/routers/research.py:673` `get_documentation` | yes | No caller anywhere. |
| `POST /api/research/recency/runs/{run_id}/restore` | `app/routers/recency.py:105` | yes | `Frontend/src/app/services/recency-chart.service.ts:169-194` has trades, hero, launches and `runs/{id}/soft-delete` only. No restore call exists anywhere. |
| `POST /api/research/recency/launches/{launch_id}/soft-delete` | `app/routers/recency.py:112` | yes | Same service. No launch mutation exists. |
| `POST /api/research/recency/launches/{launch_id}/restore` | `app/routers/recency.py:119` | yes | Same as above. |
| `DELETE /api/research/backtest-runs/{run_id}` | `app/routers/backtest_runs.py:66` | yes | `Frontend/src/app/services/backtest-runs.service.ts:26-39` does list, get and patch notes only. No `.delete(` targets it, and no Backend or script calls it. |
| `GET /api/research/golden-validations/{golden_run_id}` | `app/routers/golden_validation.py:78` | yes | `Frontend/src/app/services/golden-validation.service.ts:17-31` has list, designate and review only. |
| `POST /api/research/golden-validations/{golden_run_id}/applicability` | `app/routers/golden_validation.py:86` | yes | No caller anywhere, including tests. |
| `POST /api/dataset/generate-csv` | `app/routers/dataset.py:279` | yes | Data Lab exports through `/api/jobs-internal/dataset-zip` (`Backend/Jobs/JobsApi.cs`). Nothing posts to `generate-csv`. |
| `POST /api/dataset/generate-metadata` | `app/routers/dataset.py:318` | yes | No caller anywhere, including tests. |
| `POST /api/dataset/generate-metadata-csv` | `app/routers/dataset.py:356` | yes | No caller anywhere, including tests. |
| `POST /api/dataset/validation-report-download` | `app/routers/dataset.py:652` | yes | The Frontend calls only `/api/dataset/validation-report` (`Frontend/src/app/components/data-lab/validate/validate.component.ts:213`). |
| **Whole router** `app/routers/volatility.py` (9 routes: `/api/volatility/surface/build`, `build-from-ticker`, `build-from-csv`, `{id}/grid`, `{id}/smiles`, `{id}/diagnostics`, `{id}/query`, `{id}/export/{format}`, `batch-summary`; handlers at lines 315-900) | mount `app/main.py:1290`, import `:92` | yes | No caller anywhere. The Frontend Volatility page is a stub (`Frontend/src/app/components/options-lab/volatility-stub/volatility-stub.component.ts:74`). The only mentions are in `PythonDataService/app/volatility/docs/DASHBOARD_PLAN.md:39-46`. |
| `GET /api/lean-sidecar/runs` | `app/routers/lean_sidecar.py:878` | yes | `Frontend/src/app/services/lean-sidecar.service.ts:43-90` calls only `trusted-runs`, `diagnose` and `calendar/next-trading-day-open`. |
| `GET /api/lean-sidecar/calendar/blocked-dates` | `app/routers/lean_sidecar.py:996` | yes | Same service. No caller anywhere. |
| `GET /api/lean-sidecar/runs/{run_id}/manifest` | `app/routers/lean_sidecar.py:1090` | yes | Same as above. Only `tests/lean_sidecar/*` reach it. |
| `GET /api/lean-sidecar/runs/{run_id}/observations` | `app/routers/lean_sidecar.py:1109` | yes | Same as above. |
| `GET /api/lean-sidecar/runs/{run_id}/normalized` | `app/routers/lean_sidecar.py:1131` | yes | Same as above. |
| `POST /api/lean-sidecar/runs/{run_id}/reconcile` | `app/routers/lean_sidecar.py:1245` | yes | Same as above. The only other mentions are in `docs/architecture/lean-sidecar-lab.md:530`. |
| `POST /api/lean-sidecar/runs/{run_id}/cross-reconcile` | `app/routers/lean_sidecar.py:1481` | yes | No caller. The cross-runner docstring calls the endpoint a 501 stub (`app/lean_sidecar/cross_runner.py:4-5`). |
| `GET /api/lean-sidecar/runs/{run_id}/log` | `app/routers/lean_sidecar.py:1850` | yes | No caller anywhere. |
| `POST /api/lean-sidecar/compare` | `app/routers/lean_sidecar.py:1958` | yes | The docstring says ".NET … sends via POST /api/lean-sidecar/compare" (`app/services/lean_sidecar_compare_service.py:5`), but `Backend/` has no `lean-sidecar` path at all. |
| `POST /api/chart/allowed-timeframes` | `app/routers/chart.py:165` | yes | The only mention is a field description (`app/schemas/chart.py:168`). The Frontend calls `chart/data`, `chart/indicators*` and `range-presets`. |
| `GET /api/chart/timeframes` | `app/routers/chart.py:212` | yes | No caller anywhere, including tests. |
| `GET /api/chart/available-indicators` | `app/routers/chart.py:218` | yes | No caller anywhere, including tests. |
| `POST /api/engine/strategies/{name}/pine` | `app/routers/engine.py:176` | yes | The only mentions are docstrings (`app/engine/pine_generators.py:5`, `app/engine/strategy/registry.py:284`). No Frontend export button calls it. |
| `GET /api/engine/data/availability` | `app/routers/engine.py:210` | yes | The only mention is a docstring (`app/schemas/engine_availability.py:3`). |
| `GET /api/engine/bars` | `app/routers/engine.py:279` | yes | The only mentions are docstrings (`app/services/engine_bars_service.py:3`, `app/engine/data/policy_store.py:11`). |
| `POST /api/edge/realized-vs-iv/signals` | `app/routers/edge.py:429` | yes | `Frontend/src/app/components/edge/services/edge-api.service.ts:119-164` calls only `realized-vs-iv/series`, `iv30/*` and `aggregates/fetch`. The cross-asset and regimes pages render `edge-mock-data.service.ts`. |
| `GET /api/edge/realized-vs-iv/coverage/{symbol}` | `app/routers/edge.py:452` | yes | Same as above. |
| `POST /api/edge/cross-asset/run` | `app/routers/edge.py:487` | yes | Same as above. |
| `GET /api/edge/cross-asset/strategies` | `app/routers/edge.py:503` | yes | Same as above. Dispatch goes through `STRATEGY_REGISTRY` and only reaches this route. |
| `POST /api/edge/regimes/cluster` | `app/routers/edge.py:528` | yes | Same as above. |
| `POST /api/edge/regimes/strategy-fit` | `app/routers/edge.py:575` | yes | Same as above. |
| `POST /api/edge/trade-sim/run` | `app/routers/edge.py:595` | yes | Same as above. |
| `POST /api/edge/edge-score/series` | `app/routers/edge.py:628` | yes | Same as above. |
| `POST /api/portfolio/live-greeks` | `app/routers/portfolio.py:75` | yes | The Backend's `PortfolioLiveGreeksAsync` reuses `/api/portfolio/scenario` (`Backend/Services/Implementation/PolygonService.cs:435-446`). Nothing posts to `live-greeks`. |
| `GET /api/spec-strategy/schema` | `app/routers/spec_strategy.py:186` | yes | The only mention is a field description (`app/routers/research_runs.py:92`). |
| `GET /api/research/trading-calendar` (and the `calendar_router` that carries it) | `app/routers/research_runs.py:293` | yes | The only mentions are docstrings (`app/research/runs/window.py:7`, `app/routers/research_runs.py:73`). |
| `GET /api/iv-recorder/series/{ticker}` | `app/routers/iv_recorder.py:127` | yes | The Backend IV job posts only `/api/iv-recorder/snapshot` (`Backend/Jobs/IvRecorderJob.cs:44`). Nothing reads the series. |
| **Whole router** `app/routers/market_monitor.py` (`GET /api/market/status`, `/holidays`, `/dashboard`, lines 23-81) | mount `app/main.py:1233` | yes | The only caller is `MarketMonitorService` (`Frontend/src/app/services/market-monitor.service.ts:14`), which nothing outside its spec injects. |

### IBKR read-only diagnostics (clerk role)

These are diagnostics, not the feed. The live bar feed, its Gateway client and the live routes `/api/broker/health`, `/connect`, `/disconnect`, `/reconnect`, `/expirations`, `/strikes`, `/option-*` and `/api/brokers/{broker}/lane/ibkr-bar-check` all stay. `get_market_data_feed` is still wired at `app/main.py:817-832`.

| Route | Handler | OpenAPI | Evidence |
|---|---|---|---|
| **Whole router** `app/routers/market_data_feed.py` (`GET /api/market-data-feed/health`) | `:27`, mount `app/main.py:1324` | yes | No caller anywhere. The Frontend reads IBKR health through `/api/broker/health` (`Frontend/src/app/services/broker-health.service.ts:12`). |
| `GET /api/broker/ibkr/evidence` | `app/routers/broker.py:99` | yes | The only caller is `MarketDataFeedService.ibkrApiEvidence()` (`Frontend/src/app/services/market-data-feed.service.ts:40`), which nothing calls. The options pages use only `.expirations` and `.strikes` of that service. |
| `GET /api/broker/ibkr/evidence/stream` | `app/routers/broker.py:108` | yes | No caller anywhere. |
| `GET /api/broker/data-plane/health` | `app/routers/broker.py:141` | yes | The only caller is the unused `dataPlaneHealth()` (`market-data-feed.service.ts:34`). `compose.yaml:230` mentions it in a comment only. |
| `GET /api/broker/bars/snapshot` | `app/routers/broker.py:554` | yes | No caller anywhere. Only the retirement contract tests name it (see hazards). |
| `GET /api/broker/bars-5s/snapshot` | `app/routers/broker.py:581` | yes | Same as above. |
| **Whole router** `app/routers/broker_capability.py` (`GET /api/broker/capability`, `POST /api/broker/capability/probe`) | `:37`, `:21`, mount `app/main.py:1334` | yes | The only callers are the unused `capability()` and `probeCapability()` (`market-data-feed.service.ts:66,72`). |

### Alpaca lane routes (clerk role) and their fleet operations

A lane route reached through the fleet is dead when its catalog operation is. Cutting the operation removes the generated clerk-scoped route `/api/brokers/{broker}/clerks/{clerk_id}<path>` and changes both catalog snapshots (see hazards).

| Route | Handler | OpenAPI | Evidence |
|---|---|---|---|
| `GET /api/brokers/{broker}/order-groups` | `app/routers/brokers.py:326` | yes | No catalog operation and no caller. Only `Frontend/src/app/fleet/operation-url.spec.ts:98` and router tests name it. |
| `GET /api/brokers/{broker}/activities` (operation `activities_read`) | `app/routers/brokers.py:341` | yes | The only caller of the operation is `BrokersService.listActivities()` (`Frontend/src/app/services/brokers.service.ts:185`), which only its spec calls. `activities/period` is a separate route and stays live. |
| `GET /api/brokers/{broker}/fees/session-reconciliation` | `app/routers/brokers.py:409` | yes | No catalog operation and no caller. The only mentions are docs (`docs/references/alpaca-regulatory-fees.md:56`). |
| `GET /api/brokers/{broker}/assets` | `app/routers/brokers.py:440` | yes | No catalog operation and no caller. |
| `GET /api/brokers/{broker}/clock` | `app/routers/brokers.py:449` | yes | No catalog operation and no caller. |
| `POST /api/brokers/{broker}/live-envelope/loss-hold/clear` | `app/routers/brokers.py:980` | yes | **Money path. Dead because a live route supersedes it:** the Frontend clears holds through `configuration/risk-limits/clear-hold`, operation `configuration_risk_hold_clear` (`app/routers/broker_configuration.py:479`). That route calls the same `clear_loss_hold` (`app/broker_configuration/account_risk.py:103-113`). No script, runbook or Frontend call hits this one. |
| `GET /api/brokers/{broker}/bots/{strategy_instance_id}/runs/current` (unscoped) | `app/routers/broker_bots.py:146` | yes | The Frontend uses the account-scoped twin, operation `bot_run_current_read` at `broker_bots.py:170`. |
| **Whole router** `app/routers/run_replay.py` (GET and POST `.../runs/{run_id}/replay-receipt`) | `:42`, `:62`, mount `app/main.py:1364` | yes | No catalog operation and no caller. The only mentions are `app/broker/fleet/agent_identity.py:54-61`, a fence exemption naming the POST, and `docs/references/run-replay-proof.md:38`. |
| `GET /api/brokers/{broker}/bots/catalog` (unscoped "single-account alias") | `app/routers/broker_v2_panel.py:282` | yes | The Frontend uses the scoped `bots_catalog_read` (`broker-v2-panel.service.ts:285`). |
| `GET /api/brokers/{broker}/bots/{sid}/panel` (alias) | `app/routers/broker_v2_panel.py:579` | yes | The scoped `bot_panel_read` is live. The alias has no caller. |
| `GET /api/brokers/{broker}/bots/{sid}/chart/live` (alias) | `app/routers/broker_v2_panel.py:960` | yes | No caller anywhere. |
| `GET /api/brokers/{broker}/bots/{sid}/chart/history` (alias) | `app/routers/broker_v2_panel.py:1006` | yes | The scoped `bot_chart_history` is live. The alias has no caller. |
| `GET /api/brokers/{broker}/bots/{sid}/evidence` (alias) | `app/routers/broker_v2_panel.py:1067` | yes | The scoped `bot_evidence` is live. The alias has no caller. |
| `GET /api/brokers/{broker}/accounts/{account_id}/bots/{sid}/authority-facts` (operation `bot_authority_facts`) | `app/routers/broker_v2_panel.py:542` | yes | The operation id appears nowhere in the Frontend. |
| `GET /api/brokers/{broker}/accounts/{account_id}/bots/{sid}/chart/live` (operation `bot_chart_live`) | `app/routers/broker_v2_panel.py:946` | yes | The only caller is `BrokerV2PanelService.getLiveChart()` (`.../v2-panel/lib/broker-v2-panel.service.ts:553`), which only a spec mock calls (`bot-panel-shell.component.spec.ts:548`). |
| `GET` and `PATCH /api/brokers/alpaca/configuration/owner` (`configuration_owner_read` / `_update`) | `app/routers/broker_configuration.py:183,188` | yes | Neither operation id appears in the Frontend. `broker-configuration.service.ts` uses 20 other configuration operations. |
| `GET /api/brokers/alpaca/configuration/events` (`configuration_events`) | `app/routers/broker_configuration.py:443` | yes | The operation id appears nowhere in the Frontend. |
| `GET /api/alpaca-clerk-sqlite/.../bots/{sid}/decision-evidence` (`bot_decision_evidence`) | `app/routers/alpaca_clerk_sqlite.py:278` | yes | The only caller is `app/services/paper_live_evidence_reader.py:23`, and nothing outside tests imports that module. |
| `POST /api/alpaca-clerk-sqlite/.../bots/{sid}/runs/stop` (`custody_runs_stop`) | `app/routers/alpaca_clerk_sqlite.py:325` | yes | **Money path.** The operation id appears nowhere in the Frontend, scripts or runbooks. Operators stop runs through `bot_panel_action` and `lane_stop_all_bots`. |
| `GET /api/alpaca-clerk-sqlite/.../commands/{command_id}` (`custody_command_read`) | `app/routers/alpaca_clerk_sqlite.py:390` | yes | The operation id appears nowhere in the Frontend. |
| `GET /api/alpaca-clerk-sqlite/.../bots/{sid}/snapshot` (`custody_bot_snapshot`) | `app/routers/alpaca_clerk_sqlite.py:422` | yes | The operation id appears nowhere in the Frontend. The account-level snapshot (`:402`) stays live. |
| `GET /api/alpaca-clerk-sqlite/.../bots/{sid}/timeline` (`custody_bot_timeline`) | `app/routers/alpaca_clerk_sqlite.py:523` | yes | The operation id appears nowhere in the Frontend. The account timeline (`:493`) stays live. |
| `POST /api/alpaca-clerk-sqlite/accounts/{account_id}/reconcile` (`custody_reconcile`) | `app/routers/alpaca_clerk_sqlite.py:870` | yes | **Money path.** This manual reconcile trigger has no caller of the operation or the route. The service it wraps is #2701's to judge. |
| **Whole router** `app/routers/account_pnl_attribution.py` (`GET /api/accounts/{account_id}/pnl-attribution`, `custody_pnl_attribution`) | `:17`, mount `app/main.py:1408` | yes | The operation id appears nowhere in the Frontend. |
| `GET /api/broker-clerks/aggregate/directory` | `app/routers/broker_clerks.py:175` | yes | No caller. The Frontend directory reads `GET /api/broker-clerks`. |
| `GET /api/broker-clerks/audit/routing-receipts` | `app/routers/broker_clerks.py:361` | yes | No caller, and no runbook mentions it. |
| `GET /api/brokers/{broker}/live-verdict` (the coordinator-only alias in `app/routers/fleet_compatibility_reads.py:50-65`) | mount `app/main.py:1541` | no (the coordinator role isn't exported, and the combined snapshot has the lane route at this path) | A "retired" 410 stub. The Frontend polls `/clerks/{clerk_id}/live-verdict` (`Frontend/src/app/services/alpaca-live-verdict.service.ts:109`). Keep its sibling `/panel-profile`, which `broker-v2-panel.service.ts:168` calls. |
| **Whole router** `app/routers/alpaca_fault_injection.py` (`/api/brokers/alpaca/fault-injection/{arm,disarm,status}`) | mount `app/main.py:1444-1455`, gated on `ALPACA_FAULT_INJECTION_ENABLED` | no (the export forces it off) | No compose file, script or runbook sets the flag or calls the routes. Only `tests/routers/test_alpaca_fault_injection.py` arms it. |

### Fleet operations to drop with those routes (13)

Each one is a `ProviderOperation` in `app/broker/alpaca/clerk/fleet_adapter.py`. That file sits in #2701's area, but the cut is one change, so these rows travel with the route rows above.

| Operation | Generated public route | Declared at |
|---|---|---|
| `activities_read` | `GET …/clerks/{clerk_id}/activities` | `fleet_adapter.py:113` |
| `bot_authority_facts` | `GET …/accounts/{account_id}/bots/{sid}/authority-facts` | `fleet_adapter.py:542` |
| `bot_chart_live` | `GET …/accounts/{account_id}/bots/{sid}/chart/live` | `fleet_adapter.py:565` |
| `bot_decision_evidence` | `GET …/accounts/{account_id}/bots/{sid}/decision-evidence` | `fleet_adapter.py:506` |
| `configuration_events` | `GET …/configuration/events` | `fleet_adapter.py:404` |
| `configuration_owner_read` | `GET …/configuration/owner` | `fleet_adapter.py:254` |
| `configuration_owner_update` | `PATCH …/configuration/owner` | `fleet_adapter.py:261` |
| `custody_bot_snapshot` | `GET …/custody/bots/{sid}/snapshot` | `fleet_adapter.py:680` |
| `custody_bot_timeline` | `GET …/custody/bots/{sid}/timeline` | `fleet_adapter.py:690` |
| `custody_command_read` | `GET …/custody/commands/{command_id}` | `fleet_adapter.py:700` |
| `custody_pnl_attribution` | `GET …/custody/pnl-attribution` | `fleet_adapter.py:828` |
| `custody_reconcile` | `POST …/custody/reconcile` | `fleet_adapter.py:722` |
| `custody_runs_stop` | `POST …/custody/bots/{sid}/runs/stop` | `fleet_adapter.py:710` |

## Kill list: schemas, symbols and config already dead (not tied to a route)

| Item | Kind | Evidence |
|---|---|---|
| `app/schemas/alpaca_clerk_sqlite.py:105` `DurableConflictResponse` | schema | Named only at its definition, nowhere else in the repo. |
| `app/schemas/clerk_transaction_projection.py:21` `TRANSACTION_FEED_STATES` | constant | Named only at its definition. |
| `app/schemas/operator_blocker.py:189` `DeployPreflightResponse` | schema | No route uses it, so it is absent from OpenAPI. A TS interface with the same name exists separately (`Frontend/src/app/api/operator-blocker.types.ts:125`, #2708's call). |
| `app/routers/iv_recorder.py:42` `set_store` | function | No caller, including tests. |
| `app/routers/lean_sidecar.py:147` `_count_weekdays_between` | function | No caller. |
| `app/routers/lean_sidecar.py:1348` `_CROSS_ENGINE_DIVERGENCE_CATEGORIES` | constant | No reader. |
| `app/routers/broker.py:89` `reset_option_contracts_cache_for_testing` | test-only helper | Its only caller is `tests/routers/test_broker_option_contracts_endpoints.py`. Move the reset into the test fixture when cutting it. |
| `app/config.py:282-284` `MAX_NULL_PERCENTAGE`, `REMOVE_DUPLICATES`, `FILL_METHOD` | config | No code reads them. They appear in `PythonDataService/.env.example:13-15` and `deploy/fleet/topology.snapshot.json:215,236,242`. `FILL_METHOD="ffill"` also contradicts the forward-fill ban. |
| `app/config.py:290` `MAX_REQUESTS_PER_MINUTE` | config | No reader anywhere. |
| `app/config.py:320` `CLERK_TRANSACTION_PROJECTION_ENABLED` | config | No reader anywhere. |
| `app/config.py:271` `ALPACA_FAULT_INJECTION_ENABLED` | config | Goes with the fault-injection router. Also delete it from `app/broker_configuration/legacy_environment.py:138` and the force-off at `PythonDataService/scripts/export_openapi_contract.py:49-52`. |
| `contracts/data-plane-control-surfaces.json:16` `"/api/market-data-feed"` | config | A protected-read prefix for the router cut above. `Frontend/proxy.conf.js` and the Python guard both read this file. |

## What the cuts orphan

### Inside this area (router-local or schema), so they go in the same PRs

- `app/routers/volatility.py`: the whole file (21 of its 23 module symbols are orphans, plus `_cache` and `linspace`, which only it uses).
- `app/routers/broker_capability.py`, `market_data_feed.py`, `run_replay.py`, `account_pnl_attribution.py`, `alpaca_fault_injection.py` and `market_monitor.py` (its `monitor` singleton has no other user): whole files, plus their `app/main.py` imports and mounts (`:43, :52, :73, :74, :85, :92, :1233, :1290, :1324, :1334, :1364, :1408, :1444-1455`).
- `app/routers/edge.py`: `_parse_iv_series_for_regime`, `SignalsRequest`, `SignalsResponse`, `CrossAssetBars`, `CrossAssetRunBody`, `RegimeClusterBody`, `RegimeStrategyFitBody`, `TradeSimRunBody`, `EdgeScoreBody`. The `realized-vs-iv/series` handler and its helpers stay.
- `app/routers/lean_sidecar.py`: 29 symbols, including `RunSummaryModel`, `RunIndexResponseModel`, `_safe_load_manifest_summary`, `_resolved_workspace_or_404`, `_report_to_model`, `RunReconciliationReportModel`, `FeeDivergenceModel`, `CrossReconcileRequestModel`, `_extract_cross_run_inputs_from_manifest`, `_build_cross_engine_report`, `_TradeRecordModel`, `_DivergenceModel`, `_CompareRequestModel`, `_CompareResponseModel`, `_MAX_CALENDAR_RANGE_DAYS`, `_LEAN_LOG_TAIL_MAX_BYTES`, `_RUN_INDEX_CAP`, `_SCAN_HARD_CAP`, `_VALID_LEAN_ERROR_CATEGORIES` and `_parse_categories_note`.
- `app/routers/alpaca_clerk_sqlite.py`: `_RUNS_STOP`, `_conflict_response`, `_unknown_bot_response` and `_decision_evidence_page`.
- `app/routers/brokers.py`: `_DEFAULT_READ_LIMIT`, `_MAX_ACTIVITY_LIMIT` and `_live_envelope_not_installed`.
- `app/routers/broker_v2_panel.py`: `_live_chart` and `_resolve_default_account`. The default-account resolver exists only for the aliases.
- `app/routers/broker.py`: `_ibkr_api_evidence_to_sse`.
- `app/routers/engine.py`: `EngineBarsResponse` and `EngineBarsCoverageResponse`.
- `app/routers/iv_recorder.py`: `SeriesResponse`.
- `app/routers/research_runs.py`: `calendar_router` and its mount.
- Schemas:
  - `app/schemas/alpaca_clerk_sqlite.py:94` `StopRunRequest`
  - `app/schemas/broker_bots.py:77` `BotControlAuthorityFacts`, and `:500-533` `BotRunReadBrokerErrorDetail`, `BotRunReadRunnerErrorDetail`, `BotRunReadNotFoundResponse`, `BotRunReadRunnerErrorResponse`
  - `app/schemas/broker_capability.py:51,57` (the probe and read responses)
  - `app/schemas/chart.py:178` `AllowedTimeframesRequest`
  - `app/schemas/fault_injection.py` (the whole file)
  - `app/schemas/golden_validation.py:58,87` (the applicability request and response)
  - `app/schemas/recency.py:75` `RecencyLaunchMutationResponse`

### Outside this area: pointers, not rows

The owning ticket should confirm each one at its own SHA.

- **#2704** (engine, LEAN sidecar, volatility):
  - `app/volatility/data_loader.py` (the whole file), 6 of 8 symbols in `app/volatility/analytics.py`, and 17 of 33 classes in `app/volatility/models.py`. The golden-fixture modules `solver.py`, `fitting.py`, `vix_replication.py` and `basis.py` are not orphaned, because the live `/api/edge/iv30/*` routes use them.
  - All of `app/engine/edge/robustness_stats.py` (PBO, DSR) and `portfolio_aggregator.py`.
  - Most of `regime_clustering.py` (HMM), `features_realtime/regime_features.py` and `cross_asset_runner.py`, including its `STRATEGY_REGISTRY` consumers.
  - `period_splitter.rolling_windows` and `calendar_year_buckets`, `confidence.regime_feature_weight`, `regime_strategy_eval.partition_by_regime`, and `threshold_events.log_*`.
  - `app/lean_sidecar/cross_reconciler.py:369` `internal_fill_to_dict`.
  - `app/engine/pine_generators.py` and the `/pine` hook in `strategy/registry.py:284`, whose only consumer is the dead `/pine` route.
  - Their tests: `tests/edge/test_robustness_stats.py` (whole), `test_period_splitter.py` (4 of 5), `test_regime_clustering.py` (4 of 6), `test_confidence_gating.py` (9 of 18), `tests/volatility/test_analytics.py` (20 of 26), `app/engine/tests/test_pine_generators.py` (4 of 7) and `tests/lean_sidecar/test_cross_reconciler.py` (1). #2727 and #2728 should skip these.
- **#2705** (research, data lake):
  - `app/research/features/registry.py`: `FEATURE_REGISTRY`, `FeatureMetadata`, `get_feature_metadata` and `list_available_features`. I checked the registry: nothing else dispatches through it.
  - `app/research/documentation/formulas.py` (the whole file).
  - `app/research/recency/repository.py:458` `set_launch_deleted`.
  - `app/models/research_models.py:257` `FeatureInfoResponse`.
  - Tests: `tests/test_feature_registry.py`, `tests/test_formulas_documentation.py`, `tests/research/test_ta_features.py::test_all_registered_features_compute`, and `tests/research/recency/test_repository.py` (2 tests).
- **#2703** (services):
  - In `app/services/alpaca_fee_reconciliation.py`, the session reconciliation path: `session_fee_reconciliation`, `reconcile_session_fees`, `_read_session_fills` and their constants (10 of 19 symbols). `deployment_fee_attribution` stays.
  - `app/services/broker_order_groups.py` (all but `_TERMINAL_STATUSES`).
  - `app/services/dataset_service.py:1224` `build_metadata_json`.
  - `app/services/broker_v2_panel/panel_data_source.py:290` `get_authority_facts` and `panel_scope.py:81` `bot_process_fact`.
  - `app/services/paper_live_evidence_reader.py`, which nothing outside tests imports.
  - `app/models/portfolio.py:178` `LiveGreeksRequest`.
  - Tests: `tests/services/test_broker_order_groups.py` (whole), and 6 of 24 in `test_alpaca_fee_reconciliation.py`.
- **#2701** (Alpaca clerk): the 13 operations above. If the fault-injection router goes, the registry hooks in `app/broker/alpaca/fault_injection.py`, `client.py:425` and `trade_updates.py:928` are left with no arming path.
- **#2702** (fleet, IBKR, broker configuration):
  - `app/broker/fleet/agent_identity.py:54-61`: `_STRANDED_OPERATOR_MUTATIONS` empties once both routes it names go, so the whole exemption mechanism goes too.
  - `app/broker/fleet/lane_runtime.py:204-222`: the compatibility-read inventory names `activities`, `assets`, `clock`, `order-groups`, `fees/session-reconciliation` and `live-envelope/loss-hold`.
  - `app/broker/ibkr/bar_models.py:61` `IbkrBarsSnapshot`.
  - `app/broker/contract/models.py:249` `BrokerOrderGroup`.
  - The IBKR capability snapshot store behind `broker_capability`.
- **#2708 and #2709** (Frontend):
  - `MarketMonitorService` (the whole file and its spec).
  - `MarketDataFeedService.dataPlaneHealth`, `ibkrApiEvidence`, `connect`, `disconnect`, `reconnect`, `capability` and `probeCapability` (none is called). `BrokerHealthService` covers health and connect.
  - `BrokersService.listActivities`, `BrokerV2PanelService.getLiveChart`, and the `operation-url.spec.ts` order-groups case.
  - The edge cross-asset and regimes pages, which render mock data only.
  - `VolatilityStubComponent`.
  - Removing operation ids from `fleet-operation-catalog.snapshot.json` breaks compilation of any TS still naming them.
- **#2707** (scripts) has routes that are **alive only through one script**. If #2707 cuts the script, that route joins this list:
  - `POST /api/dataset/generate-zip` (only `scripts/verify_dataset_multipliers.py:21`).
  - `GET /api/brokers/{broker}/bots` and `GET /api/brokers/{broker}/bots/{sid}` (only `scripts/dev/fleet/_api.py:95`).
  - `GET /api/brokers/alpaca/market-status-snapshot` and its operation `market_status_read` (only `PythonDataService/scripts/run_broker_fleet_compose_qualification.py:289`).
  - The four `lane_*` operations: `stop-all-bots`, `account-quiet`, `ibkr-bar-check` and `go-live/release`. They are reached only through `app/installation_migration/` via `PythonDataService/scripts/migrate_installation.py`. `attention_read` and `bot_history_read` are live through the coordinator aggregates.
  - The qualification-only routers (`app/routers/fleet_qualification.py` and `fleet_qualification_history.py`, `include_in_schema=False`), which live for the Compose qualification script.
- **#2709**: `GET /api/examples/alpaca-bot-control/fixtures` (`app/routers/alpaca_bot_control_examples.py:34`) is an OpenAPI anchor. Nothing calls it, but it is how `AlpacaBotControlFixtureEnvelope` reaches `broker.types.ts`, and the examples gallery uses that type (`.../examples/alpaca-bot-control/alpaca-bot-control-fixtures.ts:1`). It lives or dies with that gallery page.
- **#2711, #2712, #2713 and #2714** (docs that cite cut routes):
  - `docs/design/fleet-b-route-inventory.md`
  - `docs/references/run-replay-proof.md:38`
  - `docs/references/alpaca-regulatory-fees.md:56`
  - `docs/references/paper-live-decision-comparison.md:53`
  - `docs/architecture/lean-sidecar-lab.md:272,530`
  - `docs/architecture/edge-feature-design.md`
  - `docs/architecture/engine-authority-map.md:69,81,99`
  - `docs/ibkr-integration-authority.md:69`
  - `docs/architecture/alpaca-configuration-ownership-inventory.md:124-131`
  - `PythonDataService/app/volatility/docs/DASHBOARD_PLAN.md`
  - `CONTEXT.md:2101`

## Tests that go with the cut routes (kind 5, owned here)

Whole files to delete:

| File | Evidence |
|---|---|
| `PythonDataService/tests/lean_sidecar/test_compare_endpoint.py` | All 8 tests call `/api/lean-sidecar/compare`. |
| `PythonDataService/tests/lean_sidecar/test_determinism_gate.py` | Its only test reads `runs/{id}/manifest` and `runs/{id}/normalized`. |
| `PythonDataService/tests/lean_sidecar/test_log_tail_endpoint.py` | Both tests read `runs/{id}/log`. |
| `PythonDataService/tests/routers/test_alpaca_fault_injection.py` | All 8 tests exercise the fault-injection router. |
| `PythonDataService/tests/routers/test_data_plane_health.py` | Its only test reads `/api/broker/data-plane/health`. |
| `PythonDataService/tests/routers/test_run_replay.py` | All 5 tests exercise replay-receipt. |
| `PythonDataService/tests/routers/test_engine_bars_endpoint.py` | All 6 tests drive `GET /api/engine/bars`, through the helper at `:61` or directly. That includes `test_bars_endpoint_equals_live_run_chart_bars` (`:126`), which compares the dead route against `/api/engine/backtest`. |
| `PythonDataService/tests/routers/test_engine_availability_endpoint.py` | All 5 tests drive `GET /api/engine/data/availability` (helper `:43`) or pin its wire contract (`:91`). |

Tests to delete within surviving files, where the whole test exercises a dead route:

| File | Dead of total | Tests |
|---|---|---|
| `tests/lean_sidecar/test_router_lean_sidecar.py` | 51 of 96 | The tests for `calendar/blocked-dates`, `GET runs`, `runs/{id}/manifest`, `observations`, `normalized`, `reconcile`, `cross-reconcile` and `log`, plus their orphaned models. For example `test_returns_blocked_dates_with_reasons` (`:246`) and `test_manifest_endpoint_returns_written_manifest` (`:745`). |
| `tests/routers/test_alpaca_clerk_sqlite.py` | 24 of 44 | The tests for decision-evidence, `runs/stop`, `commands/{id}`, bot snapshot, bot timeline and `reconcile`, starting at `test_decision_evidence_exposes_full_trace_and_run_identity_without_mutation` (`:206`). |
| `tests/broker/test_brokers_router.py` | 11 of 26 | The tests for order-groups (`:387`), activities (`:426`, `:520` and others), assets and clock. |
| `tests/broker/alpaca/clerk/sqlite/test_scheduled_end.py` | 5 of 31 | `test_the_raw_stop_route_*` (`:609`, `:651`, `:681`, …) and `test_a_raw_stop_naming_the_account_*`. They are money-path tests, but the route they exercise is dead. See the hazard on stop coverage. |
| `tests/test_market_monitor.py` | 5 of 18 | `test_market_status_endpoint`, `test_market_holidays_endpoint`, `test_market_holidays_limit_param`, `test_market_dashboard_endpoint`, `test_market_status_handles_error`. |
| `tests/research/runs/test_endpoint.py` | 4 of 24 | `test_trading_calendar_*` (`:378-429`). |
| `tests/routers/test_broker_bots.py` | 3 of 9 | `test_the_current_run_is_a_lazy_read_only_view`, `test_run_reads_reject_an_unknown_bot`, `test_run_read_openapi_documents_error_envelopes`. |
| `tests/research/test_endpoint.py` | 2 of 5 | `test_list_features`, `test_get_documentation`. |
| `tests/broker/v2panel/test_panel_router.py` | 2 of 25 | `test_live_chart_accepts_five_second_resolution`, `test_chart_contract_rejects_unknown_resolution_and_timeframe`. |
| `tests/marketdata/test_feed.py` | 2 of 39 | `test_health_endpoint_returns_feed_health`, `test_health_endpoint_503_when_feed_not_installed`. |
| `tests/routers/test_broker_fee_reconciliation.py` | 2 of 4 | The two route tests (`:48`, `:73`). |
| `tests/routers/test_broker_capability.py` | 2 of 5 | `test_probe_endpoint_returns_snapshot_contract`, `test_read_endpoint_returns_persisted_snapshots`. The other 3 test the capability snapshot service, which is #2702's to judge. |
| `tests/services/test_iv_recorder.py` | 1 of 12 | `test_series_window_filters`. `test_snapshot_then_read_back` is in the trim list. |
| `tests/broker/fleet/test_b_scoped_contracts.py` | 1 of 32 | `test_a_clerk_agent_still_answers_the_stranded_operator_mutations_unpinned` (`:935`). |
| `tests/routers/test_brokers_live_envelope.py` | 1 of 4 | `test_an_unsupported_broker_is_404` (the loss-hold clear route). |
| `tests/routers/test_clerk_transactions.py` | 1 of 13 | `test_pnl_attribution_endpoint_passes_the_inclusive_window_to_c2` (`:93`). |
| `tests/routers/test_dataset_plan_endpoint.py` | 1 of 22 | `test_generation_out_of_range_ms_window_is_422` (`:317`, generate-csv). |
| `tests/services/test_dataset_export_columns.py` | 1 of 21 | `test_generate_csv_unknown_column_is_422_without_fetching` (`:200`). |
| `app/engine/strategy/spec/tests/test_spec_router.py` | 1 of 7 | `test_schema_endpoint` (`:66`). |
| `tests/routers/test_recency_endpoints.py` | 1 of 6 | `test_soft_delete_and_restore_verbs_replace_the_graphql_mutations` (`:66`). It exercises run restore and launch soft-delete and restore. |

Tests to trim, because each mixes a live and a dead route. The cut removes only the dead assertions and rewrites nothing:

- `tests/routers/test_backtest_runs_endpoints.py::test_notes_round_trip_and_delete_refuses_a_live_recency_member`: drop the `DELETE` assertions at `:148-154`. The tests at `:69` and `:103` read the live `GET /{run_id}`; my path scan flagged them, but they stay.
- `tests/routers/test_golden_validation_endpoints.py`: drop the `GET /{id}` and `applicability` calls from the two workflow tests.
- `tests/lean_sidecar/test_router_lean_sidecar_e2e.py`: posts the live `trusted-runs`, then reads the dead `observations`/`log`; drop the read.
- `tests/services/test_iv_recorder.py::test_snapshot_then_read_back`: keep the live snapshot POST and drop the `series/{ticker}` read-back.
- `tests/broker/v2panel/test_shadow_operator_surfaces.py`: `:208`, `:309` and `:619` touch authority-facts and the live chart.
- `tests/broker/fleet/test_lane_runtime.py`: `:387` and `:472` pin the compatibility inventory.
- `tests/contracts/test_alpaca_active_authority_wiring.py:166` pins the custody routes in the contract.
- `tests/test_data_plane_control_security.py:158` pins the `/api/market-data-feed` prefix.
- `tests/contracts/test_ibkr_order_actuation_retirement.py:53-62` (`PRESERVED_IBKR_READ_ROUTES`) and `tests/contracts/test_ibkr_evaluator_plane_retirement.py:52-66`: see the first hazard.
- `tests/scripts/test_export_openapi_contract.py:36` asserts that fault-injection is absent from the export. It becomes moot.
- `tests/broker/fleet/test_compatibility_reads.py` and `tests/contracts/test_fleet_role_openapi_agreement.py` cover the coordinator's live-verdict alias.

## Hazards the cutting PRs must carry

1. **IBKR preservation tests contradict these cuts.** Two tests pin these routes as preserved. `tests/contracts/test_ibkr_order_actuation_retirement.py:53-62` declares `PRESERVED_IBKR_READ_ROUTES`: capability, the capability probe, `ibkr/evidence`, `ibkr/evidence/stream`, `bars/snapshot` and `bars-5s/snapshot`. `tests/contracts/test_ibkr_evaluator_plane_retirement.py:49-66` declares `PRESERVED_ROUTES`: capability and `bars/snapshot`. Both record a #1813-era decision to keep these routes as IBKR's read-only surface. Under the locked rules (dead beats sacred; the read-only feed is sacred, these diagnostics are not the feed) they go. The PR must shrink both sets rather than delete the tests: `/api/broker/health` and the Alpaca V2 entries stay pinned.
2. **Contract regeneration, merged one at a time.**
   - Every OpenAPI "yes" row needs `python PythonDataService/scripts/export_openapi_contract.py` plus `cd Frontend && npm run codegen:openapi` (`codegen:check` gates `broker.types.ts`).
   - The 13 operation cuts also change `PythonDataService/app/broker/fleet/operation_catalog.snapshot.json` and `Frontend/src/app/fleet/fleet-operation-catalog.snapshot.json`. `OperationId` is derived from the latter, so a removed id still named in TS fails `ng build`.
   - `contracts/data-plane-control-surfaces.json` changes with the market-data-feed cut.
3. **Money-path cuts: re-check, keep if unsure.** These are dead on today's evidence, with no caller of any kind: `custody_runs_stop`, `custody_reconcile`, `live-envelope/loss-hold/clear` (superseded by `risk-limits/clear-hold`, which calls the same `clear_loss_hold`), and the fault-injection seam. Before deleting `test_scheduled_end.py::test_the_raw_stop_route_*`, the clerk-SQLite test tickets (#2717-#2719) should confirm that the live stop paths, `bot_panel_action` stop and `lane_stop_all_bots`, already prove "a stop cancels the scheduled end". That is a coverage check, not a rewrite.
4. **Fence exemption.** `agent_identity._STRANDED_OPERATOR_MUTATIONS` names exactly the two routes cut here. Delete the set and its branch in the same PR, or the fence keeps an exemption for paths that no longer exist.
5. **Fault injection is env-gated.** It is absent from the snapshot, so that cut is snapshot-free. It also touches `app/config.py:271`, `app/broker_configuration/legacy_environment.py:138` and `scripts/export_openapi_contract.py:47-52`.
6. **Test sharding.** `PythonDataService/scripts/pr_shard_durations.json` lists test node ids for `scripts/pytest_shard.py` (CI `ci.yml:361`). Deleting whole test files may leave stale entries. Check how `pytest_shard.py` treats a missing id.
7. **Docs link-contract tests** (`pytest tests/contracts`) will catch any doc deleted alongside. The docs above only cite routes, so a route cut doesn't break links. Doc cuts belong to the doc tickets.
8. **The LEAN determinism test goes too.** `test_determinism_gate.py` only proves determinism through the dead manifest and normalized endpoints. Dead beats sacred, so it goes. #2728 should know that no other test then pins LEAN run determinism.
9. **Kill lists age.** Each cutting PR re-runs the caller search at its own SHA, especially for the Frontend methods (`listActivities`, `getLiveChart`, `MarketDataFeedService`). Someone wiring one up again would revive a route.

## Not reviewed

- **Routes whose only caller is the Backend.** I treated these as alive, for example `/api/snapshot/*`, `/api/quantlib/*`, `/api/research/run-*`, `/api/backtest/rule-based/run` and `/api/strategy/analyze`. Whether those Backend resolvers are themselves reachable from the Frontend is #2710's question. If #2710 finds a resolver dead, its Python route joins this list.
- **Frontend reachability past the service method.** I checked one level: a service method with at least one non-spec caller counts as live. Whether that calling component is routed and reachable is #2708's and #2709's question.
- **Dead branches inside live handlers.** I scanned module-level symbols only, plus `app/main.py` comments that mark retired or compatibility modes. Branch-level dead code inside surviving handlers and the `app/main.py` lifespan was not swept, apart from the compatibility live-verdict alias.
- **Unused fields on live schemas.** Not reviewed.
- **Route-to-test mapping** is by path literal and orphan name. Tests that reach a dead route through a helper that assembles the URL at runtime could be missed, so the cutting PR runs the touched suites.
- **Qualification routers.** I treated `fleet_qualification*` as live. They are `include_in_schema=False` and serve the Compose qualification script, which is #2707's call. I did not trace those routes one by one.
