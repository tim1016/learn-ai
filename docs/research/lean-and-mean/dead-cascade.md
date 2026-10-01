# Kill list — what the first cuts orphan (#2744)

Part of map #2700. Read at **`6a4d7d396108ef16471d8df888b9ded74d3c2892`** (`origin/master`, 2026-09-30; the map was charted at `87b8e261`, and the closed lists were read there). Paths are relative to `PythonDataService/` unless they start with `Backend/`, `Frontend/`, `docs/`, `compose` or `.github/`.

## How this was judged

- **Scratch cut, never pushed.** In my worktree I made one scratch commit (`a73f1d63`, reset before this file was committed). It deleted what the closed lists cut: 70 whole files, and the handlers of every dead route the routes list (#2706) names. It also deleted the Python routes the Backend list (#2710) leaves with no caller. The handlers were removed with an AST script, by `(method, path)` or by handler name.
- **Two diffs against an untouched `git archive` of master.**
  - vulture 2.16 at 60% confidence, from a scratch venv. One pass ran on `app/` + `scripts/` (finds code that only tests reach), and one ran with `tests/` too (finds code nothing reaches).
  - An AST import graph rooted at `app.main`, every `scripts/**`, and every app module with a `__main__` guard. It lists modules that newly become unreachable.
- **Every row below was then confirmed by caller search** (`git grep -w` across `PythonDataService/`, `Backend/`, `Frontend/src`, `scripts/`, compose, `deploy/` and `.github/`), with the registries checked where the code dispatches by name.
- **Rules applied:** ☆ (test-only helpers follow their tests), ◇ (validated volatility and options math stays; the routes around it go), and ◆ (a trim is part of a cut; money trails stay; other write-only output goes). Closed-list addenda override the list text.
- **Key finding.** The closed lists were built in parallel at one SHA, so none of them absorbed another's pointers. **None of the routes list's "outside this area" pointers appears on its owning dead-code list** (#2701–#2705). I re-proved each one here and folded it in as a row.

## Kill list

Column **PR** names the cutting PR the row rides with. **R** = the routes cut (#2706). Its sub-PRs merge one at a time, because they regenerate the OpenAPI snapshot and the fleet catalogs. **Hashed** rows sit in `DECLARED_PROGRAM_SOURCE_PATHS` files (`app/engine/strategy/program_sources.py:230`) and ride the one re-qualification PR.

### A. Python routes whose only caller the Backend list cuts (new PR "Backend cascade", lands with or after #2710's PR)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| A1 | `app/routers/research.py` (whole: `run-feature`, `run-signal`, `build-iv-history`, `run-options-feature`, `run-batch-options`, plus `features` and `documentation`, which the routes list already cuts) and its mount `app/main.py:81,1238` | router | Only callers: `Backend/Services/Implementation/ResearchService.cs:87,230,401,456,495`, which #2710 cuts (B4). No Frontend, script or compose caller (`git grep "research/run-"`). No module imports `app.routers.research`. The engines behind the routes stay live through `/api/jobs` (`jobs.py:39-48` imports `run_cross_sectional_study`, `run_feature_research`, `run_signal_engine`). | Backend cascade |
| A2 | `app/models/research_models.py` (whole, 49 classes) | schemas | The only importer is `app/routers/research.py:13`. The same-named Frontend interfaces in `research.service.ts` are GraphQL types, which #2709 owns. | Backend cascade |
| A3 | `app/routers/indicators.py` (whole: `/calculate`, `/generate-table`) and its mount `main.py:68,1230` | router | Only callers: `Backend/.../TechnicalAnalysisService.cs:37,67`, which #2710 cuts (B1). | Backend cascade |
| A4 | `app/services/ta_service.py` (whole) | module | Its only importer is `routers/indicators.py`. The newly-unreachable import-graph diff lists it. `TechnicalAnalysisService` is otherwise named only in docstrings (`engine/indicators/macd.py:5`, `research/divergence/indicators/__init__.py:7`). It is a pandas-ta pass-through with no golden fixture. | Backend cascade |
| A5 | `app/routers/sanitize.py` (whole) and its mount `main.py:86,1229` | router | Only caller: `Backend/.../SanitizationService.cs:45` (#2710 B3). | Backend cascade |
| A6 | `app/services/sanitizer.py::sanitize_generic` | method | Its only caller is `routers/sanitize.py:27` (vulture: newly unused). The `sanitize_aggregates` path stays live through `/api/aggregates/fetch`. | Backend cascade |
| A7 | `app/routers/snapshot.py`: `POST /movers`, `POST /unified` | routes | Only callers: `PolygonService.cs:217,249` (#2710 B11). `/options-chain`, `/ticker` and `/market` stay. | Backend cascade |
| A8 | `app/services/polygon_client.py::get_market_movers`, `::get_unified_snapshots` | methods | The only callers are the A7 handlers (vulture, both passes). | Backend cascade |
| A9 | `app/routers/options.py`: `POST /contracts` | route | Only caller: `PolygonService.cs:546` (#2710: `getOptionsContracts` is a dead wrapper). `/expirations` stays. `polygon_client.list_options_contracts` stays live (`options_companion_service.py:193`, `research/options/contract_finder.py:151`, `scripts/build_iv30_golden.py:71`). | Backend cascade |
| A10 | `app/routers/quantlib_options.py`: `GET /status`, `POST /strategy`, plus `QuantLibStatusResponse`, `QuantLibStrategyRequest` and `QuantLibStrategyResponse` (`:72-118`) | routes and schemas | Only callers: `PolygonService.cs:755,775` (#2710 A18/A19, B12). `/price` and `/compare` stay. | Backend cascade |
| A11 | `app/services/quantlib_pricer.py::price_strategy` | function | Its only caller is the `/strategy` handler. No test. | Backend cascade |
| A12 | `app/models/requests.py`: `SanitizeRequest`, `OhlcvBar`, `IndicatorConfig`, `CalculateIndicatorsRequest`, `IndicatorTableRequest`, `OptionsContractsRequest`, `MarketMoversRequest`, `UnifiedSnapshotRequest` | schemas | Used only by the A3/A5/A7/A9 handlers. Both vulture passes report them as orphans. | Backend cascade |
| A13 | `app/models/responses.py`: `SanitizeResponse`, `IndicatorDataPoint`, `IndicatorResult`, `CalculateIndicatorsResponse`, `IndicatorTableResponse`, `OptionsContractItem`, `OptionsContractsResponse`, `MarketMoversResponse`, `UnifiedSnapshotSession`, `UnifiedSnapshotItem`, `UnifiedSnapshotResponse` | schemas | Same. (`IndicatorTableRow`, `IndicatorInfo`, `AvailableIndicatorsResponse` and `TradeRequest` are already on #2705.) | Backend cascade |
| A14 | `app/services/dataset_service.py::indicator_table_params_to_entries`, `::rename_to_indicator_table_columns` | functions | The only caller is `/generate-table` (vulture, both passes). | Backend cascade |
| A15 | `POST /api/jobs-internal/backtest` (`app/routers/jobs.py:323` `start_rule_based_backtest_job`), with `RuleBasedBacktestJobRequest` (`:157`), `_serialize` (`:1131`), and the `"backtest"` row in `Backend/Jobs/JobsApi.cs:39` | route | The only Frontend starter is `components/jobs/backtest-job-page.component.ts:143`, the `/jobs-demo` page that #2709 cuts (B3, routed but unlinked, owner ruling). There is no other caller. | Backend cascade, with or after #2709 B3 |
| A16 | `app/services/rule_based_backtest.py` (whole) | module | Its only importer is `jobs.py:64` (A15). The ◇ addendum names "the rule-based backtest reference tests" as unused math that still goes. | Backend cascade |
| — | `/api/backtest/rule-based/run`, `/api/trades/fetch` | — | **Nothing to cut on the Python side.** Neither route exists any more (`git grep` finds them only in `Backend/GraphQL/Mutation.cs:314` and `PolygonService.cs:640`). The Backend has been calling a 404. #2710 already cuts both callers. | — |
| — | `/api/dataset/available` | — | **Stays.** `Frontend/.../indicator-catalog.service.ts:72` calls it directly. | — |

Tests that go (kind 5):
- `tests/test_indicators.py` (5), `tests/test_indicators_endpoint.py` (3), `tests/test_sanitize_endpoint.py` (5) and `tests/test_ta_service.py` (all 20; #2703 already lists 5).
- `tests/test_sanitizer.py::TestSanitizeGeneric` (6) and `::TestSanitizeGenericTimestampRoundTrip` (2).
- `tests/research/test_endpoint.py`: the three `test_run_feature_*` tests (the other two are already on #2706), so the whole file goes.
- `tests/test_router_registration.py::test_quantlib_router_is_mounted`. Its probe is `/status`. **No Python test then hits a mounted `/api/quantlib/*` route.** The surviving routes are proven only through the Backend.
- `tests/services/test_data_lab_chart_indicator_warmup.py::test_the_indicator_table_window_ignores_the_server_time_zone` (drives `/generate-table`; no list has it).
- `tests/test_rule_based_backtest_validation.py` (17, whole) and `tests/routers/test_rule_based_backtest_job_cancellation.py` (1).
- The `RuleBasedBacktestJobRequest` cases in `tests/routers/test_jobs_defaults.py`. The signal-engine cases stay.
- `Backend.Tests/Unit/Jobs/JobsApiTests.cs:63` `[InlineData("backtest")]`.

### B. Leftovers of the routes cut (#2706) that no list owns

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| B1 | `app/services/market_monitor.py` (whole) and `app/models/responses.py`: `ExchangeStatus`, `MarketStatusResponse`, `MarketHolidayEvent`, `MarketHolidaysResponse`, `MarketDashboardResponse` | module and schemas | `PolygonMarketMonitor`'s only importer is the cut `routers/market_monitor.py`. Newly unreachable in the import graph. | R (market monitor) |
| B2 | `tests/test_market_monitor.py` (whole, 18 tests: 5 on #2706, plus 13 `TestPolygonMarketMonitor`) | tests | They test B1 only. | R |
| B3 | `app/research/features/registry.py`: `FeatureMetadata`, `FEATURE_REGISTRY`, `OPTIONS_FEATURES`, `get_feature_metadata`, `list_available_features`. **`FeatureName` stays** (`ta_features.py:11`). | symbols | The only app reader is the cut `research.py:70`. `OPTIONS_FEATURES` is read only by `test_ta_features.py:64`, which #2706 cuts. I checked the registry: nothing dispatches through it. | R (research `features`) |
| B4 | `app/research/documentation/formulas.py` (whole). The rest of `documentation/` stays (the metric catalog is live). | module | Its only importer is `research.py:64`. Newly unreachable. | R (research `documentation`) |
| B5 | `tests/test_feature_registry.py` (9), `tests/test_formulas_documentation.py` (5) | tests | They test B3/B4. | R |
| B6 | `app/schemas/paper_live_experiments.py` (whole) | schemas | #2703 cuts every model except `ClerkDecisionEvidencePage`. That one's only user is `alpaca_clerk_sqlite.py:91,282` (`_decision_evidence_page`), which #2706 cuts with the decision-evidence route. | R, after #2703 |
| B7 | `app/services/data_plane_health.py::data_plane_health` and the helpers only it uses (`_reload_mode`, `_env_truthy`, `_env_falsey`, `_PROCESS_START_MS`). Also `DataPlaneHealth` and `DataPlaneReloadMode` (`app/broker/ibkr/models.py:194`) once the fixture check goes. `resolved_code_revision` **stays** (`recency/service.py:34`, `sweep/identity.py:63`). | function and models | The only caller is the cut `/api/broker/data-plane/health` (`broker.py:57`). The map's hazard list already names the `data-plane-health-v1.json` fixture check. Its Frontend half is in `api/broker-contracts.spec.ts`. | R |
| B8 | `app/broker/fleet/service.py::aggregate_directory_reads` (`:2810`) | method | The only caller is the cut `GET /broker-clerks/aggregate/directory`. | R |
| B9 | `tests/broker/fleet/test_directory_and_aggregation.py`: the six `test_http_aggregate_directory_*` tests and the two `test_aggregate_directory_reads_*` tests | tests | The routes list's test table missed them. #2721 cuts two of the six as duplicates. Keep `test_the_schema_refuses_a_second_live_assignment_for_one_clerk` and the `aggregate_lane_reads_async` tests. | R |
| B10 | `app/broker/fleet/service.py::list_routing_receipts` (`:2529`), its projection helpers, and `store.list_routing_receipts` (`store.py:931`) | read surface | The only caller chain is the cut `GET /broker-clerks/audit/routing-receipts` → service → store. No other app reader. The receipts are still **written**, and the idempotency read stays (◆: the record stays, the dead reader goes). | R |
| B11 | `tests/broker/fleet/test_audit_routing_receipts.py` (whole, 28; #2721 already cuts 5 as duplicates) | tests | Every test drives the audit route, the service paging or the store's `since_ms`/`before_*` seeks. None asserts that a command writes its receipt. Those proofs live in `test_routing_attempts.py`, `test_drain_ceremony.py` and others, which stay. | R |
| B12 | `app/research/backtest_runs/repository.py::delete_run` (`:311`) and `DeleteOutcome` (`:57`) | function | The only caller is the cut `DELETE /api/research/backtest-runs/{id}`. It is the only `DELETE FROM research_backtest_runs` in the app. | R |
| B13 | Tests that only prove the hard delete: `tests/research/backtest_runs/test_repository.py::test_a_run_backing_a_live_recency_run_cannot_be_hard_deleted` and `::test_deleting_the_lean_side_takes_its_parity_verdict_with_it`. Also `tests/research/golden_validation/test_service.py::test_designation_locks_landed_companion_until_protection_is_visible`, `::test_designation_locks_a_landed_companion_before_its_verdict_exists` and `::test_companion_landing_after_designation_is_protected_before_a_verdict_exists`, whose observable is that a concurrent `delete_run` blocks or refuses. **Trim (◆):** in `::test_only_python_runs_can_be_designated_and_linked_runs_are_retained`, drop the `delete_run` steps (`:589,602-603`) and keep the designation asserts. | tests | Once no code path hard-deletes a run, "a delete waits or is refused" protects nothing. #2726 should confirm at cut. | R |
| B14 | `app/research/golden_validation/service.py::assess` and `app/schemas/golden_validation.py::GoldenValidationApplicabilityRequest.as_configuration` | function | The only caller is the cut `POST /{id}/applicability`. The routes list cut the schema classes but missed the service function. | R |
| B15 | `app/schemas/engine_availability.py` (whole: `AvailabilityResponse`, `from_report`) | schema | The only user is the cut `GET /api/engine/data/availability` (`engine.py:33`). `engine/data/availability.check_availability` stays (7 live callers). | R |
| B16 | `app/engine/pine_generators.py` (whole), `StrategyRegistration.pine_generator` (`registry.py:285`), the import at `registry.py:19-23`, and the three `pine_generator=` kwargs (`:1359,1579,1762`) | module and field | The only reader is the cut `POST /api/engine/strategies/{name}/pine`. The routes list pointed this at #2704, which did not take it. `registry.py` is not hashed. The map already has `test_pine_generators.py` going whole. | R (engine `pine`) |
| B17 | `app/services/broker_v2_panel/panel_data_source.py::get_live_chart` (`:596`), `panel_chart_data_source.py::get_live_chart` (`:140`), and any helper only the latter uses | functions | Their only caller is `broker_v2_panel.py:902` `_live_chart`, which the routes list cuts with `bot_chart_live` and its alias. `gallery_hub.py:245` and `broker_v2_gallery.py:106` name it only in docstrings. Plus two `tests/.../test_chart_projection.py` tests (map pointer). | R (`bot_chart_live`) |
| B18 | `panel_data_source.py::get_authority_facts` (`:290`) and `panel_scope.py::bot_process_fact` (`:81`) | functions | Only `bot_authority_facts` reaches them. vulture: newly unused. (Pointer from #2706 that #2703 never took.) | R |
| B19 | `app/services/alpaca_fee_reconciliation.py`: `session_fee_reconciliation`, `reconcile_session_fees`, `_read_session_fills` and their constants. `deployment_fee_attribution` stays (`brokers.py:356`). | functions | The only caller is the cut `fees/session-reconciliation`. (Pointer from #2706 that #2703 never took.) | R |
| B20 | `tests/services/test_alpaca_fee_reconciliation.py` (whole, 24) | tests | Every test calls only B19's functions (`session_fee_reconciliation` ×4, `reconcile_session_fees` ×3, `_read_session_fills` ×1). The FEE-001 golden fee math is untouched. | R |
| B21 | `app/services/broker_order_groups.py` (all but `_TERMINAL_STATUSES`), `app/broker/contract/models.py:249` `BrokerOrderGroup` and `tests/services/test_broker_order_groups.py` | module and model | The only user is the cut `/order-groups`. Caller search is empty outside the router and the module. | R |
| B22 | `app/services/dataset_service.py::build_metadata_json`, `app/models/portfolio.py::LiveGreeksRequest`, `app/research/recency/repository.py::set_launch_deleted`, `app/broker/ibkr/bar_models.py::IbkrBarsSnapshot` | symbols | Each one's only user is a cut route (generate-metadata, live-greeks, launch soft-delete, bars snapshot). vulture flags each one in both passes. These are pointers from #2706 that #2703, #2705 and #2702 never took. | R |
| B23 | `app/broker/fleet/agent_identity.py:54-61` `_STRANDED_OPERATOR_MUTATIONS` and its branch; `app/broker/fleet/lane_runtime.py:204-222` compatibility-read inventory entries | fence code | These are routes-list hazards 4 and the `lane_runtime` pointer. #2702 never listed them. **Money path (fence):** delete the set and its branch in the same PR as the routes they name. | R |
| B24 | `app/broker_configuration/service.py::rename_owner` (`:257`) | method | The only caller is the cut `PATCH /configuration/owner`. Its tests (`test_profiles_service.py::test_renaming_the_owner_keeps_the_owner_id`, `test_legacy_import.py:481`) are already on #2722 or go with C3. | R (owner/events) |

**Correction to #2722 (rows 101 and 152): `service.events` and `store.list_events` stay (☆).** Surviving tests read through them to prove live writes:
- `test_profiles_service.py::test_archiving_preserves_every_revision_and_event` (`:148`)
- `::test_restoring_a_profile_records_restoration_in_the_audit_history` (`:168`)
- `test_schema_migration.py:71` (migration keeps the events)

Only the two event-log paging tests (`:440`, `:458`) go, as #2722 says.

### C. Scripts cut (#2707) leftovers that no list owns

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| C1 | `app/broker/alpaca/clerk/sqlite/activation_inventory.py` (whole) and `tests/.../sqlite/test_activation_inventory.py` (9) | module | Its only importer is `scripts/qualify_alpaca_activation_inventory.py` (#2707 row 13). Newly unreachable. #2707 pointed it at #2701, and #2701 pointed the script back at #2707. | #2707 (same PR as row 13) |
| C2 | `app/services/manual_order_qualification.py` (whole) | module | Its only importer is `scripts/run_manual_order_qualification.py` (#2707 row 15). #2703 kept it "reached only through" that script. Newly unreachable. | #2707 (row 15) |
| C3 | `app/broker_configuration/legacy_import.py` (whole), `tests/broker_configuration/test_legacy_import.py` (33) and `test_cutover_rehearsal.py` (1) | module | #2702 kept it because `scripts/manage_broker_configuration.py` is "the documented way off" the env path. #2707 cuts that script (row 11). After that, nothing imports it (newly unreachable). **Money path:** it lands only after #2707's own check that every clerk binds a saved profile. | #2707 (row 11) |

### D. The clerk qualification stack (the "orphan with no owner"; one PR, carried as one cut)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| D1 | `scripts/run_alpaca_sqlite_qualification.py` and `tests/scripts/test_run_alpaca_sqlite_qualification.py` (23) | script | Its only runners are `.github/workflows/ci.yml:521` (`--profile smoke`, which #2716 cuts) and the compose `alpaca-clerk-qualification` service (`compose.yaml:311-345`, which #2707 cuts). Once both land, nothing runs it. | Qualification PR |
| D2 | `app/broker/alpaca/clerk/sqlite/qualification.py`, `qualification_performance.py`, `qualification_polygon_replay.py`, `qualification_storage_recovery.py`, `qualification_synthetic_rehearsal.py`, `qualification_ui_campaign_contract.py`, `qualification_ui_evidence.py` | modules | The only importers are D1 and each other (`git grep` on every module name). | Qualification PR |
| D3 | `app/schemas/account_custody_synthetic_qualification.py`, `app/services/account_custody_synthetic_scenarios.py`, `alpaca_sqlite_synthetic_drills.py`, `alpaca_sqlite_synthetic_drill_support.py`, `alpaca_sqlite_synthetic_fault_drills.py`, `alpaca_sqlite_synthetic_frame_drills.py`, `alpaca_sqlite_synthetic_state_drills.py` | modules | They are reached only from `qualification_synthetic_rehearsal.py:42` and D1:42-45. | Qualification PR |
| D4 | `tests/_helpers/ui_correlation.py`; `tests/broker/alpaca/clerk/sqlite/test_qualification.py` (15); `test_qualification_ui_campaign_contract.py` (3); `tests/schemas/test_account_custody_synthetic_qualification.py` (21); `tests/services/test_alpaca_sqlite_synthetic_drills.py` (10) | tests and helper | They test D1–D3 only. | Qualification PR |
| D5 | **Trim (◆), IBKR feed suite:** in `tests/marketdata/test_feed_ibkr_data_loss_1101.py::test_every_bar_client_satisfies_the_realtime_bar_protocol`, drop the `_PolygonReplayClient` assert (`:584`) and its import (`:27`). The test stays for the IBKR and adversarial clients. | test edit | It is the only use of D2 from a surviving test. | Qualification PR (re-run `tests/marketdata`) |

**Correction to the ticket's exception:** `qualification_shadow_trace.py` is **not** kept. Its only app importer is `app/services/run_replay_proof.py:29`, which goes with the replay-receipt writer (E1). So it goes in PR E, not in the qualification PR.

### E. The replay-receipt writer (◆; its own PR, on the Stop path)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| E1 | `app/services/bot_runner.py`: `_schedule_run_replay_receipt` (`:2154`), `_generate_replay_receipt_in_background`, `_resume_pending_replay_receipts` (`:2232`), `run_replay_receipt`, `generate_run_replay_receipt`, `_replay_receipt_tasks`, and the call sites `:1319,1987,2267,2337,2351,2358`, plus the import at `:193` | writer | The owner ruled that replay receipts record decision parity, not money (◆). Its readers are the cut `routers/run_replay.py` and nothing else (vulture: `run_replay_receipt`, `generate_run_replay_receipt` newly unused). | Replay PR |
| E2 | `app/services/run_replay_proof.py` (whole), `app/schemas/run_replay.py` (whole), `app/broker/alpaca/clerk/sqlite/qualification_shadow_trace.py` (whole) | modules | Chain: `bot_runner` → `run_replay_proof` → `qualification_shadow_trace`. There is no other importer (`RunReplayProofService`, `RunReplayReceipt` and `run_shadow_trace_evaluation` are named only along this chain). `engine/engine.py:170` names it in a docstring only. | Replay PR |
| E3 | Whole test files: `tests/services/test_run_replay_{engine_parity (4), fidelity (12), live_evidence (4), proof_assembly (15), proof_service (9), receipt_end_to_end (3), receipt_store (3), stop_trigger (7)}.py` and `tests/broker/alpaca/clerk/sqlite/test_qualification_shadow_trace.py` (8) | tests | They test E1/E2 only. "Engine parity" here means replay-vs-engine decision parity, not a golden fixture. | Replay PR |
| E4 | **Trims (◆):** `test_account_worlds.py`, `fleet/test_b_scoped_contracts.py`, `bot_runner/test_bot_end.py`, `bot_runner/test_trade_bot_sqlite_decisions.py`, `test_bot_decision_quarantine.py`, `test_dry_run_boot_isolation.py`, `test_feed_continuity_end_to_end.py`, `test_retained_tail_join.py`. Each names replay receipts. Drop the replay step or assert. A test whose subject is the receipt goes. | test edits | These are money-path files (Stop, bot end, Dry Run). Keep every custody, flatten and "no order sent" assert. | Replay PR |

### F. IBKR diagnostics (◆; the evidence-recorder PR, which re-runs the IBKR, market-data and structural suites)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| F1 | `app/broker/ibkr/capability.py::probe_session_data_capability` and its private helpers (`_request_contract_details`, `_sample_market_data_type`, `_preview_outside_rth_limit_order`, `_build_session_capabilities`, …), plus `MarketDataCapabilityService.probe`, `.persist` and `.read_latest` | functions | The only caller of `probe` is the cut `POST /api/broker/capability/probe`. `read_latest` serves only the cut `GET /capability` (vulture: newly unused). | IBKR evidence PR |
| F2 | The IBKR evidence recorder's writer and the `get_ibkr_api_evidence_recorder().record(...)` calls (`ibkr/contracts.py:78,110,154,200,264` and `capability.py`). Also `app/schemas/broker_capability.py` (whole, once the probe and read responses go). | writer | ◆: these are feed diagnostics, and their readers are the cut evidence routes. | IBKR evidence PR |
| F3 | `app/broker/ibkr/config.py:152` `IbkrSettings.account_gate_authority`, plus **trims** of the two asserts in `tests/broker/ibkr/test_config.py:29,47` (and `:38`'s `setenv`). The tests stay for their other defaults. | config key | No reader anywhere (`git grep -i account_gate_authority`: only its definition and the test). No compose or env file sets it. | IBKR evidence PR |

**Not dead, so kept: the capability *read* path.** The ticket's pointer says `market_data_capability_service.py` "becomes unreachable". It does not.
- `read_latest_for` feeds the start admission (`bot_runner.py:566`, `bot_start_admission.py:694`), the panel (`panel_data_source.py:504`), `market_pulse.py:11`, and the ENTER gate's `extended_phase_proven_at_ms` (`market_data_capability_service.py:114`, `market_liveness.py:358`). That is the money path.
- After F1, nothing writes new snapshots. That branch then reads only what is already under `live_runs/_broker/session_capabilities/`, and it fails closed (`None` means not proven). A declared `extended_window` (ADR 0059 D5.2) still proves extended hours.
- Removing the snapshot branch itself is a gate edit, not a dead-code cut. See "Needs the map".

### G. The operator-notice package (one PR, after #2703's `mutation_rung_receipts` cut and #2709's A13–A15)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| G1 | `app/operator/` (whole: `__init__.py`, `notices/__init__.py`, `notices/schema.py`, `notices/snapshot.json`; `incidents/` is already on #2702) | package | Its only importer outside itself is `app/schemas/live_runs.py:34`. | Operator PR |
| G2 | `app/schemas/live_runs.py`: `MutationRungReceipt` (`:62`), `MutationBlockageStageId`, the import at `:34-40`, and `ReconciliationReceipt` (`:147`) | schemas | `MutationRungReceipt`'s only user is `mutation_rung_receipts.py` (cut by #2703). `ReconciliationReceipt`'s only user is `engine/live/reconciliation_receipt.py` (cut by #2704). Both vulture passes flag them. `GateResult` and `BotDutyOutcomeView` stay (they have live users). | Operator PR |
| G3 | `tests/operator/` (whole: `test_notice_codes_snapshot.py` (2), `test_notice_schema.py` (24), `test_safety_halt_notices.py` (2), `_helpers.py`; `test_incident_store.py` is already on #2702) | tests | They test G1 only. The Frontend half (`api/operator-notice-codes.snapshot.spec.ts`, `models/operator-notice.ts`) is #2709's A14–A15. | Operator PR |

### H. Engine symbols (#2704's PR, or the one re-qualification PR where hashed)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| H1 | `DecisionSnapshot` (`strategy/base.py:35`), `Strategy.last_decision_snapshot` (`base.py:266`), the writes at `ema_crossover_signal.py:384` and `deployment_validation.py:108` (`_DeploymentDecisionSnapshot`, `:74`) | **hashed** | The only reader was `engine/live/artifacts.py` (the decision-row writer), which #2704 cuts. No other `decision_snapshot` reader exists (`git grep`). | Re-qualification PR |
| H2 | `CONSOLIDATOR_PERIOD_MIN` and `STRATEGY_KEY` on `ema_crossover_signal.py:108-109` and `deployment_validation.py:80-81` | **hashed** constants | The only reader was `engine/live/indicator_state.py:319,325,589,595` (cut by #2704). `spy_vwap_reversion.py:37-38,66` reads its own copy, and that whole module goes with the conditional SPY VWAP cut. `STRATEGY_KEY` is new here (vulture: newly unused). Its only other reader is `tests/engine/live/test_spy_ema_persistence.py:143`, which dies with `indicator_state`. | Re-qualification PR |
| H3 | `StrategyRegistration.class_name` (`registry.py:267`) and its seven kwargs, plus `tests/engine/test_deployment_validation_strategy.py::test_live_start_registry_class_name_resolves_strategy_class` | field and test | No reader in app or scripts. The test claims "the runner reads" it, but no runner does. The test only proves a `getattr` it performs itself. `registry.py` is not hashed. | #2704 |
| H4 | The take-profit/stop-loss bracket path: `engine/execution/intrabar_resolver.py` (whole), `engine/engine.py:23,65-66,297-335` (bracket bookkeeping), the `take_profit_price`/`stop_loss_price` fields and kwargs in `execution/order.py:73-108` and `portfolio.py:114-142`, and the re-export in `execution/__init__.py:5` | code path | No strategy, spec, Signal Program or service sets a TP/SL price. Every setter is a test (`git grep "take_profit_price="`). Spec stop-loss exits use manage rules, a separate path (`spec/tests/test_spec_manage_rules.py`). There is no provenance or golden fixture, and the files are not hashed. | #2704 |
| H5 | `app/engine/tests/test_bracket_exits.py` (5) and `app/engine/tests/test_intrabar_resolver.py` (13 functions; #2737 counted 14). **Trim (◆):** drop the `take_profit_price=` kwarg in `app/engine/tests/test_evaluation_boundary.py:233`. | tests | They test H4 only. | #2704 |
| H6 | `app/schemas/action_plan.py::ParityWarning` (`:173`) | schema | Its only user was `engine/action_plan/parity.py` (#2704). Both passes flag it. | #2704 |
| H7 | Edge analysis stack (the routes list pointed it at #2704, which did not take it): `app/engine/edge/cross_asset_runner.py`, `edge_score.py`, `trade_simulator.py`, `regime_strategy_eval.py`, `regime_clustering.py`, `robustness_stats.py`, `portfolio_aggregator.py`, `period_splitter.py`, `features_realtime/regime_features.py` (all whole) | modules | After the edge-route cut, the scratch `routers/edge.py` names every one of them only on its import lines (count = 1 each). Outside `edge.py` they import only each other (`git grep`). Regime, HMM, PBO and DSR are not volatility math, so ◇ does not keep them ("other unused math still goes"). `confidence.py`, `vrp.py`, the realized-vol, IV30 and `labels_oracle/` modules stay, because the live `realized-vs-iv/series` route uses them. | R (edge PR) |
| H8 | `tests/edge/test_edge_score.py` (7), `test_trade_simulator.py` (5), `test_robustness_stats.py` (7), `test_period_splitter.py` (5), `test_regime_clustering.py` (6), and the `regime_feature_weight` cases in `test_confidence_gating.py` (the 9 the routes list named) | tests | They test H7 only. The `realized_vol`, `hf_realized_vol`, IV30 and spread-model tests stay (◇). | R (edge PR) |

### I. The adjusted-prices notice (new cross-stack PR)

| # | Item | Kind | Evidence | PR |
|---|---|---|---|---|
| I1 | `app/services/chart_bar_source.py`: `"price_adjustment_unsupported"` in `SpanReason` (`:101`), the `notice_code` branch (`:173-174`), and `"adjusted_prices_provider_only"` in `NoticeCode` | branch | Nothing has produced the reason since #1839. The test says so itself (`tests/services/test_chart_split_read.py:858`, "retained, unproduced, on purpose"). | Adjusted-notice PR |
| I2 | `test_chart_split_read.py::test_the_adjusted_prices_notice_stays_in_the_contract_though_nothing_emits_it`; the `adjusted_prices_provider_only` entry at `Frontend/.../data-lab-chart.component.ts:109` and its spec case (`data-lab-chart.component.spec.ts:148-153`) | tests and UI | They pin I1 only. The literal is in the OpenAPI response type, so regenerate `broker.types.ts`. | Adjusted-notice PR |

## Found and kept (not rows)

- **The IBKR-era identity reader stays for now** (money path, unsure, so keep): `services/alpaca_bot_identity.py::_legacy_ibkr_run_dir`, `engine/live/historical_run_identity.py`, and the nine tests in `test_alpaca_bot_identity.py`.
  - The reader is a deny guard on every Alpaca lifecycle mutation (`bot_runner.py:528`, `bot_binding_authority.py:602`, `bot_clerk_lifecycle.py:130`). It walks `<live_runs_root>/*/run_ledger.json`. It refuses a bot whose id matches an IBKR-era ledger, and it **refuses every bot** if any such ledger cannot be read.
  - Host check, read-only (`ls`/`find` only): `/Users/inkant/learn-ai/artifacts` does not exist, and `/Users/inkant/learn-ai/PythonDataService/artifacts` has **no `live_runs/`**. So the data-plane root `IBKR_LIVE_RUNS_ROOT=/app/artifacts/live_runs` (`compose.yaml:226`) holds no ledger.
  - The clerk lanes read `/app/artifacts/alpaca_clerk/live_runs` inside the named volumes `alpaca-clerk-data`, `alpaca-paper-clerk-data` and others (`compose.fleet.dev.yaml:51,137,166`). Those live inside the Podman VM. I did not inspect them, because that needs a container or the VM.
  - The cut is safe only after a read-only `find <lane live_runs root> -maxdepth 2 -name run_ledger.json` per lane comes back empty.
- **The capability read path** (section F): money path.
- **`StrategySpec.decision_columns` / `DecisionColumnSpec`** (`spec/schema.py:469`). Its only reader was the cut `artifacts.py` writer. But it is a field of the sealed `*.spec.json` fixtures (`spec/fixtures/ema_crossover_2_bps.spec.json:45`, `spy_ema_crossover.spec.json:44`). Removing it changes spec parsing and hashes, so it is left alone unless a re-qualification PR wants it.
- **◇ math:** `app/volatility/analytics.py` (newly unreachable once the router goes), `surface.py` (including `to_grid`, whose only caller was the router), and `fitting.py` stay importable as ruled. vulture's new hits there are route-only plumbing around kept math. A later sweep must not cut them.
- **☆ test-only helpers:**
  - `catalog_client.close_pool`: test teardown in 18 files.
  - `broker_account_snapshot.clear_broker_account_snapshot_cache_for_testing` and `ibkr/config.reset_settings_for_testing`: they lost a script caller but keep test callers.
  - `service.events` / `store.list_events` (above).
- **Research engines** behind the cut `run-*` routes (`run_iv_diagnostics`, `build_iv_history`, `run_options_feature_research`, `feature_validation`, graduation, signal, walk-forward). They stay live through `jobs.py` and `batch_runner.py:60-61`. vulture's newly-unused dataclass fields there are false positives: `jobs.py` serializes them with `asdict` (`:859-864, 1233-1234`).

## What the cuts orphan

- **Contracts:** every A/B/E/F/I route or schema change regenerates `contracts/openapi/*.json` and `Frontend/src/app/api/broker.types.ts`. A15 changes `Backend/Jobs/JobsApi.cs`, so no GraphQL change. B6/B7 also change the cross-stack fixture `data-plane-health-v1.json` and its readers (`tests/contracts/test_cross_stack_fixtures.py:57`, `Frontend/src/app/api/broker-contracts.spec.ts`).
- **Config:** D1's volume in `installation_migration/contents.py:65-66` `BUNDLED_VOLUMES` (already a map hazard). No env key is orphaned besides F3, which no env file sets.
- **`scripts/pr_shard_durations.json`:** node ids of every killed test (stale entries are harmless; regenerate after).
- **Docs that name cut code** (for the docs handoff, not rows):
  - `docs/math-sources-of-truth.md:135-138` (protected-canonical; its MACD and Bollinger rows point at `ta_service.py` and `test_indicators.py`)
  - `docs/references/run-replay-proof.md`
  - ADR 0037 `:99-101` (activation inventory)
  - the routes list's doc pointers
- **Comments in hashed files** that name cut code: `engine/indicators/macd.py:5` (`ta_service`) and `strategy/base.py:139` (brackets). Leave them, or edit them only in the re-qualification PR.

## Hazards the cutting PRs must carry

1. **Order.** A (Backend cascade) lands with or after #2710's Backend PR and #2709's jobs-demo cut, or the Backend calls removed routes. A15 must not land before the `/jobs-demo` page is gone.
2. **Contract regeneration merges serially** (A, B, E, F, I all touch OpenAPI; B16–B18 and B10 also touch the fleet operation catalogs through their routes).
3. **Hashed rows (H1, H2) ride the one re-qualification PR.** H3–H6 are not hashed. `engine.py`, `execution/*` and `registry.py` are outside `DECLARED_PROGRAM_SOURCE_PATHS`.
4. **Money-path files are edited by B23 (fence), C3 (worker env bootstrap), E1/E4 (Stop path) and F1/F2 (IBKR feed files).**
   - Each PR re-runs its suites: fleet; broker_configuration; bot_runner and clerk SQLite; IBKR, marketdata and structural.
   - Every custody, flatten, fence and "no order sent" assert stays.
5. **`tests/contracts/test_alpaca_active_authority_wiring.py:228` opens `activation_inventory.py` and the C1 script by path.** #2707 already lists it (row 14). It must land in C1's PR.
6. **The IBKR feed test trim (D5) is in a sacred suite.** Drop the one assert only.
7. **B13 leaves `golden_validation`'s designation lock in place.** It now guards against a delete that can no longer happen. That is a refactor, out of scope; do not chase it.
8. **The quantlib router loses its last Python mount test (A tests).** `/price` and `/compare` are then proven only through the Backend.
9. **The metric catalog cites test node ids.** Re-run `test_all_native_entries_carry_pinned_provenance_and_existing_evidence_receipts` after B5/H8/E3 deletions.
10. **Kill lists age.** Re-run the caller search at the cutting PR's SHA, especially `get_live_chart` and the Frontend `listActivities` / `getLiveChart` callers, and the jobs-demo route.

## Needs the map

- **The capability-snapshot branch is a gate fed by nothing** (section F). After the probe route goes, `extended_phase_proven_at_ms` and the start admission's `session_capability` can only read snapshots already on disk. That is a check that can never newly pass. Options: (a) keep the branch as is (fails closed; recommended for this cleanup); (b) a money-path follow-up that removes the snapshot branch so only declared `extended_window` terms prove extended hours.
- **The IBKR-era reader needs a read-only volume check** before it can go (see "Found and kept").

## Not reviewed

- **Frontend and Backend second-order orphans.** I traced the Python side only. A15's Backend `JobsApi` row and A/B's TS types were caller-searched, but I ran no Frontend `knip`/`ts-prune` pass after #2708/#2709/#2734 land, and no Backend symbol pass after #2710.
- **Helpers only `panel_chart_data_source.get_live_chart` uses** (B17), and the private helpers inside `capability.py` (F1). I named the entry points. The cutting PR re-runs vulture after deleting them.
- **The routes list's lean-sidecar symbol set** and `regime_feature_weight` / `confidence_gating` beyond the 9 named. They were not re-proved one by one.
- **Clerk SQLite and fleet modules that were unreachable before any cut.** The baseline import graph has 61 unreachable app modules, most of them packages, test seams or `qc-shadow` data. Example: `clerk/sqlite/repository_boundary`. They are outside this cascade.
- **The scratch cut did not delete every closed-list symbol.** It deleted whole files and route handlers only, not in-module symbols or test files. So this cascade is a floor. A symbol orphaned only by an in-module deletion would show up only on a re-sweep after the cuts land.
