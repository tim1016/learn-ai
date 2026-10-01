# Kill list: router, schema and contract tests (#2730)

Part of the lean-and-mean map, #2700. **This plans the cuts and makes none.**

- **SHA:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` when read; the map was charted at `87b8e261`).
- **Area:** `PythonDataService/tests/routers/`, `tests/schemas/` and `tests/contracts/`.

## How I read

1. A script listed every test with its docstring, its asserts, the `/api/...` literals it calls and any mock-call checks (`assert_called`, `call_args`, captured kwargs). I read each digest against the bar, then read the bodies where the digest left a doubt.
2. For each route a test calls, I checked whether #2706 (dead routes) or another closed kill list already cuts it. For each validation test, I looked one layer down for the model's own test.
3. **Skipped, because other lists own them:** everything #2706 lists in this area (`test_alpaca_fault_injection.py`, `test_data_plane_health.py`, `test_run_replay.py`, `test_engine_bars_endpoint.py`, `test_engine_availability_endpoint.py`, and its per-test lists and trims for `test_alpaca_clerk_sqlite.py`, `test_broker_bots.py`, `test_broker_capability.py`, `test_backtest_runs_endpoints.py`, `test_golden_validation_endpoints.py`, `test_dataset_plan_endpoint.py`, `test_recency_endpoints.py`, `test_alpaca_active_authority_wiring.py:166` and `test_fleet_role_openapi_agreement.py`'s live-verdict pin); `tests/contracts/test_ibkr_order_actuation_retirement.py` (#2704); `tests/contracts/test_ibkr_evaluator_plane_retirement.py` (#2706, owner addendum); `test_documentation_contract.py::test_the_served_ibkr_guide_carries_no_retired_order_capable_guidance` (#2709); and the rows #2707 and #2701 hold in `test_alpaca_active_authority_wiring.py` and `test_data_plane_image_stage.py` (see Pointers).

Three readings apply across the list:

- **Tombstones are kind 5.** A test whose only subject is a feature already deleted ("this retired name/route/file stays absent") tests a retired feature. Git history is the archive. This matches the owner's #2706 addendum, which sends the #1813 retirement pins out with the routes they preserved.
- **A 422 for a missing required field is kind 1.** It tests Pydantic and FastAPI, not our rule. A 422 for a constraint we declare (min length, ceiling, anchor) is ours and is judged as a duplicate if the model's own test proves it.
- **Structural money-path pins stay.** `test_alpaca_active_authority_wiring.py` checks the composition root's source text: the hold sync starts after its providers exist, every sweep publishes and retires dead runs, the live binding uses real ports with `custody_is_simulated=False`, a reconnect boots through the boot's own steps. These are text checks, but nothing else guards those missing-call failures, so under "on the money path, when unsure, keep" they stay: `:65`, `:181`, `:245`, `:302`, `:321`, `:352`, `:405`, `:426`, `:445`, `:476`, `:487`, `:514`, `:552`.

## Kill list

Paths are relative to `PythonDataService/tests/`.

### Kind 1: trivial

| Item | Evidence |
|---|---|
| `contracts/test_openapi_numeric_bounds_are_exact.py::test_the_domain_ceiling_is_the_end_of_year_9999` (`:81`) | It checks that the constant `MAX_TIMESTAMP_MS` equals one date literal. `::test_the_domain_ceiling_survives_the_json_round_trip` (`:74`) and `::test_no_published_bound_is_beyond_float64s_exact_integers` (`:62`) prove what the ceiling does on the contract. |
| `contracts/test_broker_configuration_route_prefix.py::test_the_prefix_is_the_one_the_contract_names` (`:104`) | `PREFIX == "/api/brokers/alpaca/configuration"`. `:53` and `:80` prove that routing at that prefix works. |
| `contracts/test_pytest_configuration.py::test_committed_pr_shard_durations_drive_the_balance` (`:107`) | It checks that the durations file has at least 1,000 positive rows. The plugin's behaviour is proven on fixtures at `:195`, `:237` and `:252`. |
| `routers/test_broker_v2_gallery.py::test_get_gallery_hub_configures_the_production_wall_for_five_second_bars` (`:136`) | It checks the private field `hub._resolution == "5s"`, not anything a caller sees. |
| `routers/test_engine_backtest_job_cancellation.py::test_the_engine_entry_point_offers_a_wait_hook` (`:101`) | An `inspect.signature` membership check. `::test_a_queued_run_is_cancelled_by_the_gates_poll` (`:60`) proves the hook works. |
| `routers/test_engine_phase_taxonomy.py::TestEngineBacktestPhaseRegistry::test_registry_contains_engine_backtest` (`:49`) | A membership check. `::test_phase_ids_in_expected_order` (`:53`) reads the same registry entry and asserts its full ordered ids. |
| `routers/test_engine_phase_taxonomy.py::TestEngineBacktestPhaseRegistry::test_friendly_lookup_returns_registered_label` (`:74`) | A getter returns the field it reads. |
| `routers/test_lean_engine_run_job_phases.py::TestLeanEngineRunPhaseRegistry::test_registry_contains_lean_engine_run` (`:47`) | Same shape as `:49` above. `::test_phase_ids_in_expected_order` (`:51`) covers it. |
| `routers/test_lean_engine_run_job_phases.py::TestLeanEngineRunPhaseRegistry::test_friendly_lookup_returns_registered_label` (`:64`) | Same shape as `:74` above. |
| `routers/test_lean_engine_run_job_phases.py::TestLeanEngineRunPhaseRegistry::test_sidecar_running_gets_a_heavier_weight` (`:68`) | It compares two constants in the registry. |
| `routers/test_lean_engine_run_job_phases.py::TestRunTrustedSamplePhaseSequence::test_progress_callbacks_are_keyword_only_and_optional` (`:93`) | Signature inspection. Callers that pass arguments positionally fail in their own tests. `::test_emit_phase_calls_match_expected_sequence` (`:80`) drives the callbacks. |
| `routers/test_data_lake_backfill_job.py::test_missing_job_id_is_422` (`:120`) | A required field missing from the body. Pydantic gives the 422. |
| `routers/test_dataset_plan_endpoint.py::test_plan_missing_ticker_is_422` (`:65`) | Same. The empty-ticker test at `:73` checks our own `min_length` and stays. |
| `schemas/test_action_plan.py::test_empty_action_plan_round_trips` (`:20`) | A Pydantic round-trip of an empty model. `:55`, `:115`, `:154` and `:245` round-trip real legs. |
| `schemas/test_action_plan.py::test_empty_action_plan_constructs_from_defaults` (`:28`) | It checks that the default lists are empty. |
| `schemas/test_ticker_request.py::TestInheritance::test_subclass_can_override_multiplier_default` (`:215`) | Tests Python class inheritance on a throwaway subclass. The real subclasses' defaults are pinned in `routers/test_jobs_defaults.py`. |
| `schemas/test_ticker_request.py::TestInheritance::test_subclass_can_override_session_default` (`:226`) | Same. |

### Kind 2: copy, doc and config-text pinning

| Item | Evidence |
|---|---|
| `contracts/test_documentation_contract.py` (whole file except `:171`, which is #2709's): `:51`, `:57`, `:66`, `:128`, `:138`, `:156` | They drive `scripts/check_documentation_contract.py`, the docs bookkeeping checker #2716 cuts (row 6): document classes, local links, ADR index rows, served-copy byte identity. **Sequencing:** this goes in the same PR as the checker, after the doc cuts (see hazard 1). |
| `contracts/test_documentation_contract.py::test_the_ibkr_authority_states_that_alpaca_execution_depends_on_this_feed` (`:100`) | It pins a heading and a verbatim sentence in `docs/ibkr-integration-authority.md`. The fact it protects now lives in CLAUDE.md's "Required broker-provider boundary". The config outcome is proven by `contracts/test_fleet_topology_snapshot.py::test_alpaca_lanes_retain_read_only_ibkr_market_data` (`IBKR_BROKER_ENABLED` and `IBKR_READONLY` on every Alpaca lane), which stays. |
| `contracts/test_pre_commit_lint_gate_parity.py` (whole file: `:26`, `:42`) | It pins the `lint-staged` text in `package.json` (`ruff check`, no `ruff format`). A change shows in the diff. Nothing a caller sees. |
| `contracts/test_pytest_configuration.py::test_fast_test_commands_filter_by_marker_not_name` (`:48`) | Reads `.claude/CLAUDE.md`, `.claude/commands/test-all.md` and `PythonDataService/CLAUDE.md` for command text, and fails if any of them is missing. |
| `contracts/test_pytest_configuration.py::test_pr_workflow_runs_bounded_python_and_frontend_shards` (`:383`) | Substring pins of `ci.yml` (`shard: [1, …, 16]`, `Frontend Test Shard …/6`). The workflow itself shows the shape, and #2738 is redeciding it. |
| `contracts/test_pytest_configuration.py::test_daily_workflow_owns_deferred_python_coverage` (`:404`) | Substring pins of `daily-tests.yml`. |
| `contracts/test_pytest_configuration.py::test_other_change_gating_suites_are_bounded_or_daily` (`:414`) | Substring pins of `ci.yml`, `daily-tests.yml`, `frontend-e2e.yml` and `run-test-budget.cjs`. |
| `contracts/test_analytical_metric_catalog.py::test_every_catalog_variant_has_trader_facing_explanation_and_caution` (`:125`) | Prose checks: `len(definition) >= 12`, `len(interpretation) >= 24`, not the generic sentence. |
| `routers/test_engine_phase_taxonomy.py::TestEngineBacktestPhaseRegistry::test_friendly_labels_are_present_and_sentence_case` (`:64`) | Label copy: non-empty and sentence case. |
| `routers/test_engine_phase_taxonomy.py::TestExecuteEngineBacktestPhaseSequence::test_the_gate_reports_the_wait_before_the_workflow_starts` (`:129`) | `'on_phase("waiting_for_engine")' in inspect.getsource(...)`: pins source text. |
| `routers/test_engine_phase_taxonomy.py::TestExecuteEngineBacktestPhaseSequence::test_the_wait_log_line_comes_from_the_registry_not_a_second_copy` (`:135`) | Pins a source substring of the log line. |
| `routers/test_lean_engine_run_job_phases.py::TestLeanEngineRunPhaseRegistry::test_friendly_labels_are_present_and_sentence_case` (`:59`) | Label copy. |
| `contracts/test_setup_macos_script_behaviour.py::test_help_documents_no_serve_flag` (`:306`) | Pins `--help` output. `::test_the_host_ng_serve_flag_is_gone` (`:298`) stays, because it proves an unknown argument is refused before anything is touched. |
| `contracts/test_handoff_script_worker_service.py::test_the_refusal_names_the_by_hand_procedure_loudly` (`:503`) | Pins script text (`cat >&2 <<EOF`, `exit 1`). The refusal's behaviour is proven by running the script in `::test_the_combined_posture_refuses_rather_than_guess_a_lane` (`:473`) and `::test_the_refusal_says_why_no_lane_was_named` (`:493`). |

### Kind 3: duplicates

| Item | Stronger survivor | Evidence |
|---|---|---|
| `contracts/test_restart_script_compose_ownership.py` (whole file: `:47`, `:65`, `:97`) | `contracts/test_restart_script_reap_behaviour.py::test_a_wrong_project_name_can_never_destroy_compose_containers` and `::test_a_stuck_clerk_is_restarted_and_never_destroyed` | The kept file runs the real `restart.sh` against a fake `podman`. Its own docstring says a text test cannot answer the question. `alpaca-live-clerk` is declared only in the overlays (`compose.fleet.dev.yaml:129`, `compose.fleet.yaml:157`), so the restart-not-reap proof also covers `:97`'s "no hardcoded name list". |
| `routers/test_data_lake_backfill_job.py::test_invalid_spec_symbol_is_422` (`:129`) | `unit/data_lake/test_types.py::TestDataRunSpec::test_lowercase_symbol_is_rejected` | The route validates the body through `DataRunSpec`, and FastAPI maps the error to 422. The rows below are the same pattern. |
| `routers/test_data_lake_backfill_job.py::test_backfill_old_field_names_rejected` (`:151`) | `…::TestDataRunSpec::test_old_field_names_are_rejected` | |
| `routers/test_data_lake_backfill_job.py::test_backfill_iso_strings_on_ms_fields_rejected` (`:167`) | `…::TestDataRunSpec::test_iso_strings_on_the_ms_fields_are_rejected` | |
| `routers/test_data_lake_backfill_job.py::test_backfill_non_integer_value_rejected` (`:173`) | `…::TestDataRunSpec::test_non_integer_values_are_rejected` | |
| `routers/test_data_lake_backfill_job.py::test_backfill_value_outside_signed_int64_rejected` (`:179`) | `…::TestDataRunSpec::test_values_outside_signed_int64_are_rejected` | |
| `routers/test_data_lake_backfill_job.py::test_backfill_off_anchor_milliseconds_rejected` (`:185`) | `…::TestDataRunSpec::test_off_anchor_milliseconds_are_rejected` | |
| `routers/test_data_lake_backfill_job.py::test_backfill_reversed_range_rejected` (`:192`) | `…::TestDataRunSpec::test_start_after_end_is_rejected` | |
| `routers/test_data_lake_backfill_job.py::test_backfill_range_over_max_cap_rejected` (`:243`) | `…::TestDataRunSpec::test_5_year_range_cap` | The provider-floor tests at `:201` and `:223` stay, because the router enforces the floor itself. |
| `schemas/test_ticker_request.py::TestBarRange::test_legacy_start_end_date_aliases_are_no_longer_accepted` (`:73`) | `::TestBarRange::test_extra_field_is_forbidden` (`:88`) | With `extra="forbid"` and no alias, an old name is just an unknown field. |
| `schemas/test_ticker_request.py::TestTickerRequest::test_legacy_ticker_alias_is_no_longer_accepted` (`:105`) | `::TestTickerRequest::test_extra_field_is_forbidden` (`:144`) | Same. |
| `schemas/test_ticker_request.py::TestTickerRequest::test_all_legacy_aliases_combined_are_no_longer_accepted` (`:115`) | `::TestTickerRequest::test_extra_field_is_forbidden` (`:144`) | Same. |
| `schemas/test_ticker_request.py::TestMultiTickerRequest::test_legacy_tickers_alias_is_no_longer_accepted` (`:164`) | `::TestMultiTickerRequest::test_extra_field_is_forbidden` (`:182`) | Same. |
| `schemas/test_ticker_request.py::TestBarRange::test_rejects_negative_multiplier` (`:59`) | `::TestBarRange::test_rejects_zero_multiplier` (`:55`) | The same lower bound. Zero is its edge. |
| `contracts/test_handoff_script_worker_service.py::test_the_operator_page_url_is_always_clerk_scoped` (`:397`) | `::test_a_fleet_lane_opens_its_own_settings_page` (`:443`) and `::test_the_combined_posture_opens_the_directorys_one_alpaca_lane` (`:450`) | `:397` greps the script for URL text. The survivors run the script's URL function and assert the clerk-scoped URL it prints. |

### Kind 4: mock theater

| Item | What it actually asserts |
|---|---|
| `routers/test_engine_backtest_job_cancellation.py::test_the_worker_checks_redis_on_every_cancel_call` (`:55`) | Only `captured["kwargs"]["cancel_check_every_n"] == 1` on a stubbed call. The outcome, that a queued run gets cancelled, is `:60`. |
| `routers/test_lean_engine_run_job_cancellation.py::test_the_worker_reads_the_cancel_flag_on_every_check` (`:111`) | The same captured-kwarg check. The outcome is `::test_a_cancel_set_before_the_work_starts_never_calls_the_orchestrator` (`:120`). |

### Kind 5: retired features (tombstones)

| Item | Evidence |
|---|---|
| `routers/test_arming_retirement.py` (whole file, 1 test) | It asserts that the retired arming route returns 404 and that no fleet operation or CLI for it survives. |
| `contracts/test_legacy_backtest_surface_retirement.py` (whole file, 4 tests) | It asserts that the pre-Engine-Lab strategy package, the live-instance router and assembler, and `/api/live-runs` do not exist (retired in #1813 PR-B). |
| `contracts/test_legacy_lifecycle_projection_retirement.py` (whole file, 2 tests) | `:29` asserts the `lifecycle_projection` names are absent. `:66` pins router source text (`Query(default=50, ge=1, le=100)` and the "unavailable" sentences). Its IBKR half already retired. |
| `contracts/test_unscoped_bot_mutation_retirement.py::test_unscoped_bot_mutation_routes_are_absent` (`:54`) | Retired routes are absent (#2069). Its "successors are present" half restates the committed OpenAPI snapshot. |
| `contracts/test_unscoped_bot_mutation_retirement.py::test_retired_unscoped_deploy_and_stop_bodies_are_absent` (`:62`) | `not hasattr(schemas, name)` for two deleted request models. (`:70`, the Frontend URL scan, is kept: it guards that bot mutations go through the clerk-scoped builder.) |
| `contracts/test_alpaca_active_authority_wiring.py::test_legacy_custody_modules_and_selector_are_absent` (`:86`) | The legacy custody selector and factory names are absent. |
| `contracts/test_alpaca_active_authority_wiring.py::test_production_has_no_import_of_a_retired_legacy_module` (`:98`) | Same, for imports. |
| `contracts/test_alpaca_active_authority_wiring.py::test_legacy_broker_v2_mutation_dispatch_is_absent` (`:110`) | Asserts deleted `@router.post` literals are not in `brokers.py`. |
| `contracts/test_alpaca_active_authority_wiring.py::test_frontend_has_no_legacy_custody_dispatch_or_orphaned_controls` (`:124`) | Retired Frontend paths and method names are absent. |
| `contracts/test_alpaca_active_authority_wiring.py::test_generated_contract_removes_generic_mutations_but_keeps_read_evidence` (`:154`) | Retired paths are not in the OpenAPI snapshot. The presence half restates the snapshot. |
| `contracts/test_alpaca_active_authority_wiring.py::test_alpaca_models_and_reads_have_no_legacy_custody_fallback` (`:203`) | Retired symbols and the `broker == "ibkr"` branch are absent, plus two pinned "unavailable" sentences (kind 2). |
| `routers/test_engine_phase_taxonomy.py::TestExecuteEngineBacktestPhaseSequence::test_no_legacy_phase_ids_remain` (`:141`) | The pre-#471 phase ids are absent from the source. |
| `schemas/test_operator_blocker.py::test_the_retired_retire_replace_move_is_rejected` (`:118`) | The closed vocabulary rejects a value already removed from it (#2590). |
| `schemas/test_operator_blocker.py::test_the_retired_desk_routing_is_rejected` (`:146`) | Same, for the retired desk routing (PRD #2560). |
| `schemas/test_operator_blocker.py::test_the_retired_account_desk_host_is_rejected` (`:311`) | Same, for the retired `account_desk` host. |

### Kind 5: tests that ride a dead-code cut the owning list missed

The owning list cuts the code. These tests exercise only that code, but the owning list didn't name them. They go in the owner's PR.

| Item | Owner | Evidence |
|---|---|---|
| `routers/test_broker_fee_reconciliation.py::test_rejects_a_value_that_is_not_a_trading_days_session_open` (`:40`) and `::test_reports_unavailable_without_an_active_sqlite_clerk` (`:59`) | #2706 | Both GET `PATH`, the dead `fees/session-reconciliation` route. #2706 lists only `:48` and `:73`, so the whole file goes. |
| `routers/test_brokers_live_envelope.py::test_the_clear_reports_no_hold_over_http` (`:38`), `::test_no_installed_runtime_is_503` (`:60`) and `::test_the_clear_is_refused_without_the_control_secret` (`:74`) | #2706 | All POST `_CLEAR_PATH = "/api/brokers/alpaca/live-envelope/loss-hold/clear"` (`:30`), which is dead. #2706 lists only `:49`, so the whole file goes. Money path: see hazard 3. |
| `routers/test_clerk_transactions.py::test_pnl_attribution_resolves_the_active_authority_on_the_canonical_route` (`:165`), `::test_pnl_attribution_still_refuses_a_foreign_route_account` (`:176`) and `::test_pnl_attribution_reports_the_failed_authority_on_the_canonical_route` (`:188`) | #2706 | All three drive the dead `account_pnl_attribution` router through `_get_pnl_attribution*`. #2706 lists only `:93`. |
| `routers/test_edge_recorder_fallback.py::TestParseIvSeriesNullCoalescing::test_parse_iv_series_for_regime_handles_explicit_null_health` (`:228`) | #2706 | Its subject, `_parse_iv_series_for_regime`, is on #2706's `edge.py` orphan list. It is a payload parser for the dead regimes route. It is not one of the volatility formulas the 2026-09-30 ruling keeps, and no reference fixture proves it. |
| `contracts/test_cross_stack_fixtures.py::test_data_plane_health_fixture_is_the_direct_fastapi_to_angular_contract` (`:56`) | #2706 | `DataPlaneHealth` reaches the wire only through the dead `GET /api/broker/data-plane/health` (`app/routers/broker.py:141`). Its Angular reader `dataPlaneHealth()` is unused. The other three tests in the file are live contract guards and stay. |
| `schemas/test_account_custody_synthetic_qualification.py` (whole file, 21 tests) | #2707 / #2701 | Its subject, `app/schemas/account_custody_synthetic_qualification.py`, is in #2707's synthetic-rehearsal island (`dead-scripts.md`, the pointer to #2701). Dead beats sacred, custody classification included. |
| `schemas/test_alpaca_bot_control_example.py` (whole file, 2 tests) | #2709 / #2706 | It validates `contracts/fixtures/alpaca-bot-control/v1/*`. The fixtures' only other readers are the unlinked `/examples/alpaca-bot-control` page (#2709 B5, dead under ☆) and its OpenAPI anchor route `GET /api/examples/alpaca-bot-control/fixtures` (`app/routers/alpaca_bot_control_examples.py:34`, #2706's pointer). This answers #2709's H7 question: yes, a Python test reads them. It goes when B5 and the anchor route go. |

## Waits on the volatility-transport ruling

Nothing in this area. No router, schema or contract test reaches `/api/volatility/surface/*`. A grep for `volatility/surface` and `routers.volatility` across the three folders finds no match. The live IV30 tests (`routers/test_iv30_router.py`) and the recorder-fallback tests on the live `realized-vs-iv/series` route stay, judged as before.

## Proven math, unused

None in this area. Every reference- or tolerance-pinned math test here exercises a live route and stays:

- `routers/test_chart_indicators_endpoint.py` (`atol=1e-9`, the live `/api/chart/indicators`);
- `routers/test_iv30_router.py`;
- `routers/test_engine_chart_endpoint.py::test_strategy_rsi_uses_canonical_wilders_value_and_bar_close_timestamp`;
- `contracts/test_strategy_lab_analytical_manual_fixture.py` (a golden fixture).

`routers/test_engine_bars_endpoint.py`'s "golden equality gate" is #2706's. It compares the dead route with the live chart, not with a reference.

## For CI shape (#2738)

Kept tests marked `slow`. None of them is a math test:

- `contracts/test_fleet_role_openapi_agreement.py` (module mark)
- `contracts/test_fleet_polygon_key_topology.py` (module mark)
- `routers/test_fleet_qualification.py::test_hold_command_requires_a_proven_coordinator_forward_through_the_real_middleware_stack`

## Surviving contract tests and the doc cuts they block

- **The OpenAPI and GraphQL guards earn their place and block no doc cut.** They read only `contracts/`, `app/` and `Frontend/src`:
  - `contracts/test_frontend_graphql_input_types.py`
  - `contracts/test_openapi_numeric_bounds_are_exact.py` (`:62`, `:74`)
  - `contracts/test_fleet_role_openapi_agreement.py`
  - `contracts/test_fleet_agent_path_templates_resolve.py`
  - `contracts/test_broker_configuration_route_prefix.py` (`:53`, `:67`, `:80`)
  - `contracts/test_cross_stack_fixtures.py` (`:20`, `:27`, `:47`)
  - `contracts/test_analytical_metric_catalog.py::test_committed_catalog_matches_deterministic_generator`
  - `contracts/test_alpaca_active_authority_wiring.py::test_generated_contract_preserves_sqlite_custody_routes` (after #2706's trim)
  - `routers/test_alpaca_clerk_sqlite.py::test_timeline_openapi_contract_enumerates_registered_transition_kinds`
  - `schemas/test_broker_v2_gallery.py::test_live_update_fields_match_the_pinned_frontend_type`
- **`contracts/test_documentation_contract.py`, until it goes with the checker.** It blocks:
  - every doc cut that leaves a kept doc linking a cut one;
  - every doc or ADR still named in `docs/doc-authority.md` or in the checker's `EXACT_DOCUMENT_CLASSES` and `RETIRED_DOCUMENTS` lists;
  - any edit to `docs/architecture-manual.md` not mirrored in its served copy;
  - removing `.claude/hooks/`.

  `:100` blocks slimming `docs/ibkr-integration-authority.md`'s load-bearing section. `:171` (#2709) blocks B6's cut of `Frontend/src/assets/docs/ibkr-setup-guide.md`.
- **`contracts/test_pytest_configuration.py::test_fast_test_commands_filter_by_marker_not_name`, while it lives.** It blocks #2715 from cutting `.claude/commands/test-all.md`, and blocks any CLAUDE.md slimming that drops the `run_fast_tests` / `-m "not slow"` command, because it fails on a missing or silent source. It is cut above, so it goes before or with those edits.
- **`contracts/test_setup_macos_script_behaviour.py::test_existing_clerk_data_stops_the_run_and_is_never_marked_ready` (`:362`)** asserts that `setup-macos.sh` prints `alpaca-sqlite-clerk-recovery-and-cutover.md`. It reads the script, not the doc, so it blocks no cut. But a rename of that runbook (kept by #2713) must change the script and this test together.

## What the cuts orphan

- **`routers/test_clerk_transactions.py`:** the helpers `_get_pnl_attribution` (`:137`) and `_get_pnl_attribution_from_account_number_authority` (`:147`), once `:93`, `:165`, `:176` and `:188` go.
- **`contracts/fixtures/data-plane-health-v1.json`** and its `contracts/fixtures/README.md:12` entry. They go once the cross-stack test above and the data-plane-health case in `Frontend/src/app/api/broker-contracts.spec.ts` (#2732) go.
- **`contracts/fixtures/alpaca-bot-control/v1/*`**, together with #2709 B5 and the anchor route.
- **`contracts/test_alpaca_active_authority_wiring.py`:** module constants read only by the cut tombstones (`RETIRED_LEGACY_CUSTODY_PATHS` `:11`, `RETIRED_FRONTEND_PATHS` `:51`, and possibly `AUTHORITY_SELECTOR_PATHS` `:37`). The cutting PR drops whichever constants lose all readers. The file and its kept tests stay.
- **`PythonDataService/scripts/pr_shard_durations.json`:** stale ids for every deleted test. The shard plugin tolerates a partial match (`test_pytest_configuration.py:237` reports "6 of 9 matched"), so this is cleanup, not breakage. #2706 hazard 6 covers it.
- **No conftest or shared helper is orphaned.** These folders have no conftest. `contracts/compose_files.py` keeps six contract-test users plus `tests/test_config.py` and `tests/broker_configuration/`.

## Hazards the cutting PR must carry

1. **Doc-test order.** `test_documentation_contract.py` goes in the same PR as `scripts/check_documentation_contract.py` (#2716 row 6), and only after the doc cuts land. Until then it is the one net catching a kept doc that links a cut one. Its `:171` belongs to #2709.
2. **One owner for `test_ibkr_order_actuation_retirement.py`.** `dead-engine.md:202` (#2704) points it to #2730, while my brief assigns it to #2704 (with #2706 for its `PRESERVED_IBKR_READ_ROUTES`). I did not judge it. Its `:159-172` test opens `engine/live/account_clerk_journal.py` by path, so it breaks when #2704 deletes that file.
3. **Loss-hold clear is money path.** Before deleting `test_brokers_live_envelope.py`, confirm that the live successor `configuration/risk-limits/clear-hold` proves both the clear and the control-secret 403. `tests/broker_configuration/test_account_risk_routes.py::test_apply_is_immediate_receipt_is_effective_and_old_hash_unchanged` proves the refuse-then-clear. I did not find the successor's 403 test.
4. **Synthetic qualification schema.** Before the 21 schema tests go, confirm at the cutting SHA that no router or report reader imports `account_custody_synthetic_qualification.py`. #2701 asked the same question.
5. **CI text pins and #2738.** The three workflow-text tests break the moment #2738 reshapes CI. Cut them in or before that PR.
6. **Tombstones.** Once they go, a retired name can come back unnoticed. That is the accepted cost: git history is the archive.
7. **The kept structural pins** in `test_alpaca_active_authority_wiring.py` will trip on any `main.py` composition refactor. That is intended.
8. **Kill lists age.** Re-check each row at the cutting SHA, then run `tests/routers`, `tests/schemas`, `tests/contracts` and `tests/unit/data_lake/test_types.py`.

## Pointers (outside this area)

- **#2706:** every route-only test it lists in these folders. Also `app/routers/broker.py:89` `reset_option_contracts_cache_for_testing`: its test `routers/test_broker_option_contracts_endpoints.py` stays, because the IBKR option-contract route is live.
- **#2707:** `contracts/test_alpaca_active_authority_wiring.py::test_migration_gate_requires_an_explicit_nonempty_inventory` (`:228`, its row 14), and `contracts/test_data_plane_image_stage.py`'s `QUALIFICATION_SERVICES` pins and `:209` (its row 32). The rest of that file guards the `runtime` target and stays.
- **#2701:** the vacuous `get_or_open_repository` assert at `test_alpaca_active_authority_wiring.py:81`. Drop the line and keep the test.
- **#2702:** `app/broker/ibkr/models.py:194` `DataPlaneHealth` and `app/services/data_plane_health.py::data_plane_health` lose their only reader with the route. `resolved_code_revision` in the same module stays, because `research/recency/service.py` and `research/sweep/identity.py` import it.
- **#2709:** `test_documentation_contract.py:171`, and the H7 answer above.
- **#2710:** `routers/test_jobs_defaults.py` and `routers/test_rule_based_backtest_job_cancellation.py` follow their job routes if #2710's cut of the Backend's rule-based and research mutations leaves those Python job routes without a caller.
- **#2732:** the data-plane-health case in `Frontend/src/app/api/broker-contracts.spec.ts`.
- **#2738:** the slow-marked tests and the workflow-text pins above.

## Not reviewed

- **`routers/test_alpaca_clerk_sqlite.py`.** I could not reconcile #2706's "24 of 44" with path literals. `:788`, `:829` and `:898` read the dead bot-snapshot route but exercise the live `recovery-actions/execute`, so I treated them as live. The cutting PR should rerun #2706's mapping.
- **Legacy-shape branches inside live handlers.** I did not check whether any caller still sends:
  - the legacy trusted-run payload (`routers/test_lean_sidecar_router.py::test_trusted_run_request_model_legacy_accepts_partial_payload_with_pr_a_defaults`);
  - the job models' camelCase legacy aliases (`routers/test_jobs_defaults.py::TestCamelCaseWire`).

  That is branch-level dead code, which #2706 also left out.
- **Duplicates one layer down** were checked only for the backfill spec and `TickerRequest`. The 400 and 422 tests in the dataset-plan, grid-search, walk-forward, recency-chart and return-distribution files were not compared against their service or model tests.
- **Behavioural twins** of the structural money-path pins in `test_alpaca_active_authority_wiring.py`, and of `contracts/test_data_plane_control_configuration.py::test_macos_bootstrap_repairs_control_secret_before_compose_startup` (a text-order check): I did not search for them, so all of these are kept.
