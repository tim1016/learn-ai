# Kill list — top-level, script, edge and structural tests (#2731)

Part of map #2700. Read at **`6a4d7d396108ef16471d8df888b9ded74d3c2892`** (`origin/master`, 2026-09-30; the map was charted at `87b8e261`). Paths are relative to `PythonDataService/` unless they start with `scripts/` (repo root) or `docs/`.

Area: `tests/test_*.py` (35 files), `tests/scripts/` (12), `tests/edge/` (12), `tests/structural/` (1), `tests/slow/` (1), and the repo-root `scripts/test_*.py` (3).

## How this was judged

- An AST pass over every file listed each test with its line, docstring, assert count, and flags for mock-call asserts (`assert_called*`, `call_args`, `call_count`), substring pins on text, and explicit tolerances. Every row below was then read in full. I looked one layer up and one layer down (endpoint ↔ service ↔ model) for duplicates, and the stronger test is named in each kind-3 row.
- I first removed everything the dead-code tickets already list (#2703, #2704, #2706, #2707, plus #2716 for gate scripts and #2710's pointers). See **Already owned elsewhere**.
- Applied the ★/☆ rulings and the coordinator's **volatility-math ruling (owner, 2026-09-30)**: validated volatility math stays even when nothing uses it. Proven-but-unused math waits on a further owner ruling and is not cut here.

## Kill list

| # | Item | Kind | Evidence |
|---|---|---|---|
| 1 | `tests/test_aggregates.py::test_fetch_aggregates_calls_polygon_with_correct_params` | 4 | Patches the Polygon client and the sanitizer. Its only assert is `fetch_aggregates.assert_called_once_with(...)` (`:144`). No response is checked. |
| 2 | `tests/test_aggregates.py::test_fetch_aggregates_calls_sanitizer` | 4 | Only asserts `sanitize_aggregates.assert_called_once_with(raw_data)` (`:188`). `::test_fetch_aggregates_success` already proves the sanitized payload reaches the response. |
| 3 | `tests/test_aggregates.py::test_fetch_aggregates_default_multiplier_and_timespan` | 4 | Only reads `call_args.kwargs` on the patched client (`:232-234`). |
| 4 | `tests/test_aggregates.py::test_fetch_aggregates_missing_required_fields_returns_422` | 1 | Sends only `ticker` and expects 422. That is FastAPI/Pydantic's own required-field check. The empty-ticker and bad-timespan 422 tests stay, because they prove constraints the schema declares. |
| 5 | `tests/test_batch_runner.py::test_weighted_aggregate_ic_carries_se_approximation_disclaimer` | 2 | Asserts that `"approximation"` and `"Lo (2002)"` appear in the note text (`:475-476`). `::test_weighted_aggregate_ic_ci_brackets_point_estimate` proves the CI itself. |
| 6 | `tests/test_config.py::test_settings_loads_with_env_key` | 1 | Proves only that pydantic-settings reads an env var, plus the `HOST`/`PORT` defaults. |
| 7 | `tests/test_config.py::test_research_config_defaults_are_frozen_and_stable` | 1 | Repeats six default literals of `ResearchConfig`. |
| 8 | `tests/test_config.py::test_research_config_frozen_raises_on_mutation` | 1 | Tests that `dataclass(frozen=True)` is frozen. It catches a bare `Exception` (`:163-166`). |
| 9 | `tests/test_config.py::test_signal_config_defaults` | 1 | Repeats seven default literals of `SignalConfig`. |
| 10 | `tests/test_data_plane_control_security.py::test_local_dev_opt_out_has_named_environment_switch` | 1 | Asserts that a constant equals its own literal (`:477`). |
| 11 | `tests/test_data_plane_control_security.py::test_broker_v2_protected_reads_are_declared_in_shared_manifest` | 3 | One `"/api/brokers" in _PROTECTED_READ_PREFIXES` check. `::test_always_guarded_reads_are_declared_in_shared_manifest` (`:158`) scans every guarded route against those prefixes, and `::test_broker_v2_read_rejects_missing_or_wrong_secret` (`:345`) proves the refusal. |
| 12 | `tests/test_engine_strategies_endpoint.py::test_every_registered_strategy_can_be_imported_by_key` | 3 | `::test_every_registered_class_name_resolves_against_its_module` (`:261`) imports the same modules, collects the same `ImportError`s, and also resolves the class. |
| 13 | `tests/test_engine_strategies_endpoint.py::test_every_registered_strategy_has_explicit_class_name` | 3 | An empty `class_name` already fails the `getattr` in that same resolve test. |
| 14 | `tests/test_engine_strategies_endpoint.py::test_deployment_validation_class_name_is_consecutive_green` | 1 | Pins one registry string literal. The resolve test proves it resolves. |
| 15 | `tests/test_engine_strategies_endpoint.py::test_deployment_validation_alias_no_longer_exists` | 1 | Asserts that a deleted alias is still absent (`hasattr(...) is False`). No behaviour is involved. |
| 16 | `tests/test_error_handlers.py::test_polygon_exception_handler_serializes_exception_message` | 3 | `::test_polygon_exception_handler_returns_500_json_response` (`:28`) asserts the whole body, `error` included, for the same handler. |
| 17 | `tests/test_feature_validation.py::test_evaluate_stage_1_finalises_decision_with_cost_erasure_message` | 2 | Asserts the substrings `"Stage 1"` and `"Cost erases"` in `final_decision`. `::test_evaluate_returns_stage_1_when_cost_erases_alpha` (`:291`) proves the stage. |
| 18 | `tests/test_health.py::test_health_includes_git_sha_field` | 5 | Its comment says the operator console compares `git_sha` against the host daemon. The host daemon is retired. `git grep git_sha` finds only `app/main.py:1592-1593` and this test, so nothing reads the field. No dead-code ticket owns the field (see Pointers). |
| 19 | `tests/test_health.py::test_root_returns_service_info` | 2 | Pins `"Polygon Data Service"` and `"1.0.0"` on `GET /`. `test_health_returns_200` stays (compose healthcheck). |
| 20 | `tests/test_indicators_endpoint.py` (whole file, 3 tests) | 3 | Each test repeats one in `tests/test_indicators.py`. `rejects_unknown_indicator` repeats `::test_calculate_invalid_indicator_name`. `rejects_empty_bars` repeats `::test_calculate_empty_bars_rejected`. `returns_success_shape` repeats `::test_calculate_sma_returns_success`, which asserts more. See also the cascade below. |
| 21 | `tests/test_insight_framework.py::TestInsightScore::test_initial_values` | 1 | Repeats three dataclass defaults. |
| 22 | `tests/test_insight_framework.py::TestInsightScore::test_set_direction`, `::test_set_magnitude` | 1 | A plain setter round trip. The clamp tests and `TestDefaultInsightScorer` exercise `set_score` with logic. |
| 23 | `tests/test_insight_framework.py::TestInsightManager::test_summary_to_dict` | 1 | Asserts only that five keys exist. `::test_get_summary_direction_accuracy` and `::test_get_summary_confidence_calibration` prove the values. |
| 24 | `tests/test_sanitize_endpoint.py::test_sanitize_custom_quantile` | 4 | Its only assert is a call-args check on the patched sanitizer. The file is also cascade-dead (below). |
| 25 | `tests/test_sanitizer.py::TestSanitizeAggregates::test_summary_has_removal_percentage` | 1 | `assert "removal_percentage" in result["summary"]` (`:148`). Key presence only. `::test_valid_ohlcv_data_retained` and `::test_invalid_high_low_filtered` prove the counts. |
| 26 | `tests/test_sanitizer.py::TestSanitizeGeneric::test_summary_has_columns_processed` | 1 | Key presence only. Cascade-dead with `/api/sanitize` (below). |
| 27 | `tests/test_snapshot.py::test_snapshot_missing_ticker_returns_422` | 1 | Pydantic's required-field check. `::test_snapshot_empty_ticker_returns_422` proves the declared constraint. |
| 28 | `tests/test_stationarity.py::test_summary_text_contains_verdict_and_pvalues` | 2 | Pins the formatted text `"ADF p=0.0100"` / `"KPSS p=0.1000"`. The verdict tests (`:31`, `:44`) prove the classification. |
| 29 | `tests/test_strategy_endpoint.py::TestStrategyEndpointValidation::test_missing_symbol_returns_422` | 1 | Pydantic's required-field check. |
| 30 | `tests/test_strategy_engine.py::TestAnalyzeStrategyValidation` (4 tests) | 3 | Each one repeats, with a weaker `pytest.raises(Exception)`, an HTTP-boundary test in `tests/test_strategy_endpoint.py::TestStrategyEndpointValidation`. `invalid_option_type` repeats `::test_invalid_option_type_returns_422`. `invalid_position` repeats `::test_invalid_position_returns_422`. `negative_spot_price` repeats `::test_zero_spot_price_returns_422` (the same `gt=0`, `app/models/strategy.py:47`). `empty_legs` repeats `::test_missing_legs_returns_422`, which sends `legs: []` (`min_length=1`, `:45`). |
| 31 | `tests/test_router_registration.py::test_return_distribution_router_is_mounted` | 3 | `tests/routers/test_return_distribution_endpoint.py` drives the same route through the mounted app (4 client calls). The quantlib half of this file is in the cascade below. |
| 32 | `tests/scripts/test_regenerate_cross_engine_study.py::test_parse_args_one_cell`, `::test_parse_args_one_ticker`, `::test_parse_args_mutually_exclusive`, `::test_parse_args_requires_one` | 1 | These test argparse's own mutually-exclusive required group. The digest-approval tests (`:32`, `:40`) and the `resolve_target_cells` tests stay. The script is a fixture regenerator, so the script itself is sacred, but its tests are judged by the bar. |
| 33 | `tests/scripts/test_run_broker_fleet_compose_qualification.py::test_parse_args_accepts_build_timeout_s_and_defaults_to_120` | 1 | Pins an argparse default and a type conversion. |
| 34 | `tests/scripts/test_run_alpaca_sqlite_qualification.py::test_profile_arguments_remain_backward_compatible` | 1 | An argparse round trip of `--profile smoke`. It is the one test #2707 left in this file, so the whole file goes. Its `synthetic_rehearsal is False` assert breaks anyway once #2707 cuts that flag. |
| 35 | `tests/structural/test_ibkr_feed_boundary.py::test_retired_modules_no_longer_resolve` | 1 | Asserts that deleted module files stay deleted. A live reference to a retired module is already caught by `::test_no_surviving_module_references_a_retired_module` (`:385`) and the transitive-import test (`:514`), which stay. The feed boundary itself is not touched. |

**Nothing to cut** in `scripts/test_alpaca_onboarding_gates.py` or `scripts/test_measure_fill_to_cash_visibility.py`. Both are money-path refusal gates: empty or mismatched credential pairs, non-flat accounts, a roundtrip that "never resells". Their `EnvFileParsing` cases prove that the gate's parsing matches compose. So the `Alpaca Onboarding Gates` job (#2716) keeps its runner role. The same goes for `tests/test_live_bar_aggregator.py` (sacred IBKR feed; its one call-count test proves a single upstream subscription), `tests/test_indicator_parity.py` and `tests/test_lean_statistics.py` (math parity and golden oracle), `tests/scripts/test_hitl_alpaca_capture.py` and `tests/scripts/test_manage_canary_admission.py` (money path), and `tests/slow/test_polygon_fixture_freshness.py` (it keeps a fixture honest).

## Keep: volatility-math ruling (owner, 2026-09-30)

All of these stay whole, including the tests other tickets marked for cutting:

- `tests/edge/test_realized_vol.py` and `tests/edge/test_hf_realized_vol.py`: realized vol and HF realized vol.
- `tests/edge/test_iv30_and_vrp.py`: whole. This **overrides #2704's partial cut** of the `delta_inversion` / `iv_change` / `iv_vol` tests (`iv30_constructor` features and `delta_inversion` are named in the ruling).
- `tests/edge/test_iv30_stability.py`: whole. This **overrides #2704's partial cut** of the legacy-IV30 / `solve_iv_chain` tests.

## Waits on the volatility-transport ruling

None of these files is in this area. No test here drives `/api/volatility/surface/*`, the surface cache, the option-chain loader or the stub page. Those tests are in `tests/volatility/` and the routes ticket's list (#2704 / #2706).

## Proven math, unused (no cut until the owner rules)

| Item | Owner of the cut | What proves it |
|---|---|---|
| `tests/test_rule_based_backtest_validation.py` (whole) | cascade, #2710 → #2706 (below) | The engine tests (`test_engine_exit_timing`, `test_engine_no_overlapping_trades`, `test_engine_indicator_snapshots_populated`, `test_summary_metrics_formulas`) run `run_rule_based_backtest` against a 50-trade reference spreadsheet (`:1-17`). If the file survives, six of its tests are kind 1: `test_reference_trade_metrics`, `test_reference_summary_consistency`, `test_entry_conditions_met`, `test_pnl_calculations`, `test_result_matches_pnl_sign` and `test_engine_pnl_calculation_matches_reference`. They run no app code, only checks on the hard-coded `REFERENCE_TRADES`/`REFERENCE_SUMMARY`, and the last never calls the engine despite its name. |
| `tests/edge/test_spread_model.py`: the `option_spread` tests | #2704 (row E) | The file calls itself "Parity tests" (`:1`). The check is against hand calculations (`test_option_spread_atm_30d_matches_hand_calc`), not a golden fixture. |
| `tests/edge/test_confidence_gating.py`: 9 of 18 (confidence calibration) | #2706 → #2704 | Named in the ruling as an example. These are synthetic-input tests. I found no golden fixture. |
| `tests/edge/test_regime_drift.py` (whole) | #2704 (row G) | Named in the ruling. These are synthetic-input tests. `reference_centroids` is a parameter name, not a reference source. |
| `tests/edge/test_robustness_stats.py` (whole, PBO / DSR), `tests/edge/test_regime_clustering.py` 4 of 6 (HMM) | #2706 → #2704 | Formulas from the literature, but no golden fixture or parity test in the file. I list them so the owner sees them. They are not "proven against a reference" by the map's definition. |

## Already owned elsewhere (skipped, not rows)

- **#2703:** `tests/test_sanitizer.py::TestSanitizeTrades` (4) and `::TestSanitizeIndicator` (2). `tests/test_ta_service.py::test_generate_indicator_table_*` (5).
- **#2704:** `tests/edge/test_regime_drift.py`, plus the `test_spread_model.py` `option_spread` tests (both on hold, above). `tests/test_insight_framework.py::TestInsightScore::test_get_score` (`InsightScore.get_score` is test-only, row D). The #2704 partial cuts in `test_iv30_and_vrp.py` and `test_iv30_stability.py` are **overridden** by the ruling (above).
- **#2706:** `tests/test_market_monitor.py`, 5 endpoint tests. The `/api/market-data-feed` prefix pin in `tests/test_data_plane_control_security.py:158`. `tests/scripts/test_export_openapi_contract.py:36` (moot after the fault-injection cut). The `tests/edge/` partials it points at #2704 (`test_robustness_stats.py`, `test_period_splitter.py` 4/5, `test_regime_clustering.py` 4/6, `test_confidence_gating.py` 9/18). `tests/test_feature_registry.py` and `tests/test_formulas_documentation.py` (pointed at #2705; see gap 1).
- **#2707:** `tests/scripts/test_bench_panel_read_latency.py`, `test_manage_broker_configuration.py`, `test_run_manual_order_qualification.py`, `test_backfill_lean_runs.py` (also #2705), and 22 of 23 in `test_run_alpaca_sqlite_qualification.py`. It also holds conditionally `test_run_broker_fleet_conformance.py` (its runbook is cut by #2713, so it goes) and `test_manage_alpaca_shadow.py` (money path, kept while `alpaca-shadow-authority.md` keeps its operator section; #2714 slims that doc).
- **#2716:** `scripts/test_check_adr_status.py`.

## Cascade gaps: kind 5 that no ticket lists yet (needs the map)

1. **Feature registry and formulas docs.** #2706 orphans `FEATURE_REGISTRY` and `app/research/documentation/formulas.py` and points their tests (`tests/test_feature_registry.py`, `tests/test_formulas_documentation.py`) at #2705. But #2705 counted route-reached modules as live and does not list them. The #2706 cutting PR should take both files. `test_formulas_documentation.py` would be kind 2 (it pins doc text) anyway.
2. **Market monitor service.** #2706 cuts the whole `app/routers/market_monitor.py`. That router is the only importer of `app/services/market_monitor.py` (`git grep PolygonMarketMonitor`). So the service and the other 13 `TestPolygonMarketMonitor` tests in `tests/test_market_monitor.py` die with it. #2703 lists only `display_dashboard`.
3. **Backend-only Python routes (#2710 → #2706).** #2710 cuts the only callers of `/api/indicators/calculate`, `/api/sanitize`, `/api/backtest/rule-based/run`, `/api/quantlib/status` and the `run-*` research routes. #2706 treated Backend-called routes as live and has not re-judged them. If those routes go, these become kind 5:
   - `tests/test_indicators.py` (whole), `tests/test_indicators_endpoint.py` (row 20) and `tests/test_sanitize_endpoint.py` (whole);
   - `tests/test_sanitizer.py::TestSanitizeGeneric` (6) and `::TestSanitizeGenericTimestampRoundTrip` (2), since `sanitize_generic`'s only caller is `app/routers/sanitize.py:27`;
   - `tests/test_router_registration.py::test_quantlib_router_is_mounted`;
   - `tests/test_rule_based_backtest_validation.py` (on hold as proven math, above).
   - The `app.research.*` modules behind `tests/test_batch_runner.py`, `test_feature_validation.py`, `test_graduation.py`, `test_signal_diagnostics.py`, `test_standardize.py`, `test_regime.py`, `test_stationarity.py` and `test_walk_forward_alpha_decay.py` may orphan too, unless the `jobs` router keeps them alive (#2705 lists them as reachable through `research` and `jobs`). Someone has to trace that.
4. **Edge scoring and trade simulator.** `app/engine/edge/edge_score.py` and `trade_simulator.py` are imported only by `cross_asset_runner.py`, `regime_strategy_eval.py` and `routers/edge.py`. #2706 cuts the consumers of all three (the edge-score and trade-sim routes and most of the cross-asset runner). If nothing live remains, `tests/edge/test_edge_score.py` (7) and `test_trade_simulator.py` (5) are kind 5 for #2704.

## What the cuts orphan

- **No fixture, conftest fixture or helper** is orphaned by rows 1–35. `tests/test_aggregates.py` keeps its client fixture through the surviving tests. `tests/test_indicators_endpoint.py`'s local `_bar`/`_synthetic_bars` helpers go with the file. `tests/scripts/conftest.py` stays (other script tests use it).
- **`app/main.py:1592-1593` `git_sha`** (and `settings.GIT_COMMIT_SHA`, if nothing else reads it) loses its only reader with row 18. That is a wire field, so it is #2706's to remove, with an OpenAPI regen if `/health` is in the snapshot.
- **`scripts/pr_shard_durations.json`**: the node ids of every killed test. Stale entries are harmless (`scripts/pytest_shard.py:7-11`); regenerate the file after the cuts.

## Hazards the cutting PR must carry

- **#2707 row 30 is wrong about the control-surface schema.** It says nothing validates `contracts/data-plane-control-surfaces.schema.json`. But `tests/test_data_plane_control_security.py:35,114` loads that schema and runs `jsonschema.validate` on the manifest (`::test_data_plane_control_surface_manifest_matches_schema`). Cutting the schema crashes that test. Either keep the schema, or drop the `jsonschema.validate` line in the same PR. The prefix-shape asserts in that test stay.
- **#2704 row F vs a surviving script test.** `HISTORICAL_LEAN_IMAGE_DIGEST_ARM64` (`app/lean_sidecar/config.py:83`, listed "test-only") is imported by `tests/scripts/test_regenerate_cross_engine_study.py::test_parse_args_accepts_retained_historical_fixture_digest` (`:32-35`). That test proves the fixture regenerator still accepts the retained historical digest. The constant is not test-only: `config.py:153` builds `RECONCILIATION_FIXTURE_IMAGE_DIGESTS` from it, the allow-list for reconciliation fixtures. So it is live through a fixture regenerator. Drop that row from #2704.
- **The volatility ruling overrides two #2704 partial cuts** (`test_iv30_and_vrp.py`, `test_iv30_stability.py`). The #2704 cutting PR must not delete those tests, or the code they exercise.
- **Kill lists age.** Re-check each row at the cutting PR's own SHA, especially the kind-3 rows: the stronger test must still exist and still assert what is cited.
- **Lint after deletion.** Removing tests can leave unused imports (`patch`, `json`, `StrategyAnalyzeRequest`). Run `ruff check PythonDataService/app/ PythonDataService/tests/` at project scope.

## For CI shape

- `tests/slow/test_polygon_fixture_freshness.py` refetches live Polygon data. It is the only slow test in this area, and it stays.

## Pointers outside this area (not rows)

- **#2706:** the `git_sha` health field, and gaps 1–3 above.
- **#2704:** gap 4. The hold on the `option_spread`, regime-drift and confidence tests. The volatility-ruling override of its IV30 partials.
- **#2705:** gap 1 (`FEATURE_REGISTRY`, `formulas.py`) and the `research.*` reachability through `jobs` (gap 3).
- **#2710:** the cascade in gap 3 starts with its Backend cuts. #2706 must re-judge those Python routes.
- **#2716:** `Alpaca Onboarding Gates` stays a runner. This list cuts neither root script test.
- **#2730 (contract tests):** not touched here.

## Not reviewed

- **`tests/edge/test_confidence_gating.py`** (the 9 tests #2706 does not point at), **`test_period_splitter.py`::`test_walk_forward_train_test_pairing`** and **`test_regime_clustering.py`**'s two k-means tests. I listed their names but did not read their bodies.
- **`tests/scripts/test_run_broker_fleet_compose_qualification.py`** (34 tests, 795 lines). I read names, docstrings and assert counts. All but row 33 read as outcome tests on qualification evidence, fault matrices and isolation proofs, but I did not read each body.
- **`tests/scripts/test_manage_alpaca_shadow.py`** and **`test_run_broker_fleet_conformance.py`** were not judged by the bar, because their fate rides on #2707's doc-conditional rows.
- **`tests/test_graduation.py`, `test_feature_validation.py`, `test_batch_runner.py`, `test_signal_diagnostics.py`, `test_dst_transitions.py`, `test_standardize.py`, `test_regime.py`, `test_walk_forward_alpha_decay.py`, `test_ta_service.py` (surviving half), `test_strategy_engine.py` (outside row 30) and `test_strategy_engine_phase1_1.py`** were judged on names, docstrings and assert counts, with bodies spot-read. Bodies were not read for every test.
- **`scripts/test_alpaca_onboarding_gates.py`** and **`scripts/test_measure_fill_to_cash_visibility.py`** use `unittest`. I judged them by class and test name and by their docstrings. Not every body was read.
