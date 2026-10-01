# Kill list: research tests

Ticket #2726, part of map #2700. **Plan, don't cut.**

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30).
- **Area:** `PythonDataService/tests/research/` (99 test files, 1,189 test functions; a parametrized test counts once).
- **Paths:** relative to `PythonDataService/tests/research/` unless they start with `app/`, `tests/`, `docs/` or `.github/`.

## How this was judged

1. **Inventory.** A throwaway `ast` script pulled every test's name, docstring and assert lines (plus `pytest.raises`, `assert_*` and `np.testing` calls, and a mock-use flag). I judged every test from that view, then opened the body of each candidate and of the test it would duplicate.
2. **One layer up and down.** For each module I checked the endpoint, runner, storage and engine tests against each other. The largest pattern: the four per-phase storage modules (`baselines`, `monte_carlo`, `walk_forward`, `runs`) are thin wrappers over one `ArtifactStore` (`app/research/artifact/store.py`). Their tests re-prove the store's mechanics, which `artifact/test_store.py` and the phase endpoint tests already prove.
3. **Kept the phase error types.** The routers catch each phase's `*CorruptError` and `*AlreadyExistsError` and map them to status codes (`app/routers/baselines.py:154,201`, `monte_carlo.py:124,166`, `walk_forward.py:182,244`, `research_runs.py:226,284`). So the per-phase corrupt and overwrite tests stay unless an endpoint test already proves the mapped status. Only `runs` has one (the 409 collision test).
4. **Sacred.** No test here proves a money-path outcome; this area is research tooling. The math-parity tests stay: every golden-fixture test (`parity/test_qc_aapl_phase3_trade_parity.py`, `parity/test_ibkr_commission_golden.py`, `parity/test_cross_engine_study.py`, `artifact/test_runs_byte_equivalence.py::test_runs_save_matches_pre_pr_golden_bytes`, `ml/test_quantconnect_fixture_*`, `ml/test_e2e_replay.py`), plus `recency/test_stats.py::TestTradeDollarPnl::test_matches_the_canonical_engine_flat_fee_formula`.
5. **Skipped: dead-code tickets own these.**
   - #2705: `parity/test_fixture_data_reader.py::test_factory_returns_callable_matching_runner_signature`, the last assert of `sweep/test_eligibility.py::test_the_operational_harness_is_excluded_by_category_not_by_a_list`, and the assert at `sweep/test_warmup.py:91`.
   - #2706: `test_ta_features.py::TestFeatureDispatcher::test_all_registered_features_compute`, `recency/test_repository.py::test_a_snapshot_for_a_tombstoned_launch_is_a_no_op` and `::test_soft_delete_and_restore_report_whether_the_row_existed` (both use the dead `set_launch_deleted`), the four `runs/test_endpoint.py::test_trading_calendar_*` tests, and `test_endpoint.py::test_list_features` and `::test_get_documentation`.

Kinds: 1 trivial · 2 copy and doc pinning · 3 duplicate (keep the strongest) · 4 mock theater · 5 retired (dead-code tickets own these).

## Kill list

### artifact/

| Item | Kind | Evidence |
|---|---|---|
| `artifact/test_baselines_byte_equivalence.py` (whole file, 1 test) | 3 | It proves that `ArtifactStore.save` writes `json.dumps(model_dump(mode="json"), ensure_ascii=False)`. Every phase goes through that one write (`store.py:55,164,168`), and the descriptor has no serialization option (`descriptor.py:72-82`). `artifact/test_runs_byte_equivalence.py::test_runs_save_matches_pre_pr_golden_bytes` pins the same write against frozen golden bytes (`tests/fixtures/golden/research-artifact-pr4/`). The docstring says it is a strangler-PR acceptance bar, and that migration is finished. |
| `artifact/test_monte_carlo_byte_equivalence.py` (whole file, 1 test) | 3 | Same as above. |
| `artifact/test_walk_forward_byte_equivalence.py` (whole file, 1 test) | 3 | Same as above. |
| `artifact/test_runs_byte_equivalence.py::test_runs_save_preserves_canonical_hash_fields_in_ledger` | 3 | Reads back the constant hash strings it wrote (`"f" * 64`, …). The golden-bytes test in the same file already pins every byte of that ledger. |
| `artifact/test_store.py::test_list_ids_corrupt_warning_uses_descriptor_log_tag` | 2 | Asserts only the descriptor's log prefix on the skip warning. The skip itself is proven by `::test_list_ids_skips_dir_with_corrupt_config`. |

### backtest_runs/

| Item | Kind | Evidence |
|---|---|---|
| `backtest_runs/test_service.py::test_the_coroutine_form_runs_the_same_write_off_the_calling_loop` | 4 | Patches `persist_run_payload_sync` to return `99`, then asserts that the async wrapper returns `99`. The `test_service_db.py` tests await the real `persist_run_payload` against Postgres. |
| `backtest_runs/test_service_db.py::test_marking_a_group_failed_through_the_service_transitions_only_a_pending_verdict` | 3 | `mark_parity_failed_sync` only passes through to `repo.mark_parity_failed` and logs (`app/research/backtest_runs/service.py:202-216`). `test_repository.py::test_mark_failed_transitions_only_a_pending_verdict` proves both the transition and the untouched terminal row. |

### baselines/

| Item | Kind | Evidence |
|---|---|---|
| `baselines/test_generators.py::TestRandomEmaWindows::test_same_seed_produces_identical_output` | 3 | `baselines/test_runner.py::test_random_ema_windows_same_seed_produces_identical_parameters` proves that the same seed gives the same parameters through the runner. |
| `baselines/test_storage.py::test_save_writes_canonical_json` | 3 | Checks one id field. The runs golden-bytes test (artifact row above) pins the store's bytes. |
| `baselines/test_storage.py::test_load_missing_raises` | 3 | `baselines/test_endpoint.py::test_get_missing_returns_404`. |
| `baselines/test_storage.py::test_save_replace_overwrites` | 3 | `artifact/test_store.py::test_save_replace_clobbers`. `save_baseline` passes `replace` straight to the store (`app/research/baselines/storage.py:67-81`). |
| `baselines/test_storage.py::test_list_empty` | 3 | `baselines/test_endpoint.py::test_list_empty`. |
| `baselines/test_storage.py::test_list_orders_by_created_at_desc` | 3 | `artifact/test_store.py::test_list_ids_orders_newest_first`. `list_baselines` keeps the store's id order. |
| `baselines/test_storage.py::test_list_filter_by_parent_run_id` | 3 | `baselines/test_endpoint.py::test_list_filter_by_parent_run_id`. |

### divergence/

| Item | Kind | Evidence |
|---|---|---|
| `divergence/test_preflight.py::test_run_preflight_summary_counts_statuses` | 2 | Asserts only that `"1 blocking"` and `"blocking issue"` appear in the summary sentence. The blocking status is proven by `::test_run_preflight_full_session_is_blocking`. |

### documentation/ (the analytical-metric catalog)

The catalog is live: run persistence records it (`app/research/backtest_runs/records.py`, `app/services/engine_persistence.py`). These tests pin its prose, or compare it with id lists written by hand into the test. These tests stay: the catalog-versus-LEAN-oracle key tests, the sentinel reproducer, the evidence-receipt existence check, the stable-id and unique-id checks, and the fixture-receipt honesty check.

| Item | Kind | Evidence |
|---|---|---|
| `documentation/test_analytical_metric_catalog_entries.py::test_lean_trade_profit_factor_keeps_the_finite_no_loss_sentinel_distinct` | 2 | Exact `display` strings and substrings of `scoring_behavior`. The sentinel value itself is proven by `::test_lean_trade_sentinels_match_the_canonical_reproducer`. |
| `documentation/test_analytical_metric_catalog_entries.py::test_runtime_order_count_is_not_misattributed_to_runtime_statistics` | 2 | Substring checks on one entry's `source_reference` citation. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_results_catalog_covers_all_frozen_verdict_inputs_as_policy_concepts` | 2 | Compares the catalog with a set of ids typed into the test, plus the phrase "does not redefine the underlying metric". Nothing derives that set from the verdict policy. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_results_catalog_preserves_sortino_unavailability_and_full_run_projection_distinctions` | 2 | Pins hand-listed ids and value-state labels in catalog prose. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_platform_headline_entries_carry_their_own_authored_category` | 2 | Repeats each metric's category in a dict typed into the test. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_verdict_policy_input_series_is_prose_not_a_bare_variant_id` | 2 | Asserts that `input_series` contains a space. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_performance_memory_entries_cover_horizon_timing_seasonality_and_overlapping_rolls` | 2 | A hand-written id set plus an `"America/New_York"` substring in prose. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_results_catalog_has_specific_trader_guidance_for_every_quantity` | 2 | Each `interpretation` must not equal one generic sentence and must be at least 24 characters long. |
| `documentation/test_analytical_metric_catalog_results_entries.py::test_policy_entries_derive_their_threshold_prose_from_the_scorer_owned_descriptor` | 2 | Checks that one prose string contains another. |

### ml/

| Item | Kind | Evidence |
|---|---|---|
| `ml/test_regression.py` (whole file, 2 tests) | 3 | `test_legacy_1_0_ledger_loads` repeats `runs/test_ledger_v1_1.py::test_ledger_loads_legacy_1_0_dict`, which asserts more. `test_existing_sma_crossover_spec_round_trips` loads `spy_ema_crossover.spec.json` and checks `predictions == []`. `runs/test_ema_acceptance.py::test_canonical_fixture_runs_and_produces_completed_ledger` loads and runs that same spec end to end. |
| `ml/test_loader.py::test_prediction_lookup_error_subclasses_value_error` | 1 | Repeats the class declaration `class PredictionLookupError(ValueError)` (`app/research/ml/loader.py:35`). |

### monte_carlo/

| Item | Kind | Evidence |
|---|---|---|
| `monte_carlo/test_endpoint.py::test_response_timestamps_are_int64_ms` | 1 | `isinstance(..., int)` on fields typed `int` (`app/research/monte_carlo/result.py:77,114`) and pinned in `contracts/openapi/python-data-service.openapi.json`. The response cannot carry another type. |
| `monte_carlo/test_methods.py::TestReshuffle::test_same_seed_produces_identical_output`, `::TestResample::test_same_seed_produces_identical_output` | 3 | `monte_carlo/test_runner.py::test_same_seed_produces_identical_results` proves determinism across the whole run (bands, quantiles, breaches). |
| `monte_carlo/test_methods.py::TestReshuffle::test_different_seeds_produce_different_orders` | 3 | `monte_carlo/test_runner.py::test_different_seeds_produce_different_results`. |
| `monte_carlo/test_storage.py::test_save_writes_canonical_json` | 3 | The runs golden-bytes test. |
| `monte_carlo/test_storage.py::test_load_missing_raises` | 3 | `monte_carlo/test_endpoint.py::test_get_missing_returns_404`. |
| `monte_carlo/test_storage.py::test_save_replace_overwrites` | 3 | `artifact/test_store.py::test_save_replace_clobbers`. |
| `monte_carlo/test_storage.py::test_list_empty` | 3 | `monte_carlo/test_endpoint.py::test_list_empty`. |
| `monte_carlo/test_storage.py::test_list_orders_by_created_at_desc` | 3 | `artifact/test_store.py::test_list_ids_orders_newest_first`. |
| `monte_carlo/test_storage.py::test_list_filter_by_parent_run_id` | 3 | `monte_carlo/test_endpoint.py::test_list_filter_by_parent_run_id`. |
| `monte_carlo/test_storage.py::test_list_filter_by_method` | 3 | `monte_carlo/test_endpoint.py::test_list_filter_by_method`. |
| `monte_carlo/test_storage.py::test_list_filter_by_since_ms` | 3 | `artifact/test_store.py::test_list_ids_filter_by_since_ms`. The `since_ms` filter runs inside the store. |

### options/

The options engines are live through `/api/jobs`: `app/routers/jobs.py:39` runs `batch_runner.run_cross_sectional_study`, which calls `build_iv_history` and `run_options_feature_research` (`app/research/batch_runner.py:313,373`).

| Item | Kind | Evidence |
|---|---|---|
| `options/test_iv_builder.py::TestQualityFilters::test_min_iv_boundary`, `::test_max_iv_boundary`, `::test_min_option_price` | 1 | Each asserts that a module constant equals its literal (`app/research/options/iv_builder.py:29-31`). `TestGetOptionPrice::test_rejects_below_min_price` proves the price floor's effect. |
| `options/test_iv_builder.py::TestNormalizeIvFallback::test_returns_none_for_any_dte`, `::test_returns_none_for_zero_dte` | 3 | `_normalize_iv_fallback` ignores its arguments and returns `None` (`iv_builder.py:225`). `::test_always_returns_none` already pins that. |
| `options/test_iv_builder.py::TestInterpolateIv::test_not_simple_linear` | 3 | Same inputs as `::test_variance_time_formula`, which pins the exact value, so it cannot also equal the linear value. |
| `options/test_iv_builder.py::TestInterpolateIv::test_result_positive` | 1 | Inside the brackets the result is the square root of a positively weighted variance, so it cannot be negative. `::test_result_bounded_by_inputs` also pins a positive lower bound. |
| `options/test_options_features.py::TestIv30d::test_preserves_length` | 3 | `::test_returns_atm_iv` asserts series equality, which already covers length. |
| `options/test_options_features.py::TestVrp::test_signal_mode_no_future_leak` | 1 | The leak assert `result.iloc[-1] is not np.nan or ...` is always true, because a pandas float is never the `np.nan` object. Only `result.name` is really checked. |
| `options/test_options_features.py::TestComputeFeatureDispatch::test_iv_30d`, `::test_iv_rank_60`, `::test_log_skew` | 3 | These assert only `len(result) == len(df)`. `test_options_runner.py::TestOptionsRunner::test_iv_30d_raw`, `::test_iv_rank_60_directional` and `::test_log_skew` send the same names through `compute_feature` (`app/research/options_runner.py:188`) and assert a full report. |

### parity/

| Item | Kind | Evidence |
|---|---|---|
| `parity/test_ibkr_commission.py::test_small_order_hits_per_order_minimum`, `::test_spy_150_shares_hits_floor` | 3 | The golden cases 10 @ $100 and 100 @ $580 (both $1.00 floor) in `tests/fixtures/golden/ibkr-commission-tiered/cases.json`, run by `parity/test_ibkr_commission_golden.py::test_commission_matches_published_schedule`. |
| `parity/test_ibkr_commission.py::test_large_order_uses_per_share_rate` | 3 | The golden case 1000 @ $580 → $5.00. |
| `parity/test_ibkr_commission.py::test_aapl_phase3_representative_fill` | 3 | The golden case 500 @ $100 → $2.50 (exact per-share rate, no floor or cap). |
| `parity/test_ibkr_commission.py::test_tsla_221_shares_uses_per_share_rate` | 3 | `::test_aapl_365_shares_uses_per_share_rate` pins the same half-up rounding of a fee ending in a half cent. |
| `parity/test_qc_fixture_smoke.py::test_orders_fixture_has_expected_event_fields` | 3 | `parity/test_qc_aapl_phase3_trade_parity.py::test_qc_aapl_phase3_trade_level_parity` parses the same `qc_orders.json` through `parse_qc_orders`, which raises `FixtureSchemaError` on a missing field, and pins the aligned fill. |
| `parity/test_qc_fixture_smoke.py::test_orders_fixture_fee_presence_branch_decider` | 1 | Its own comment says it "never fails on branch identity". It asserts that the file exists and calls `print()`. The parity test asserts fee presence itself (`test_qc_aapl_phase3_trade_parity.py:220`). |
| `parity/test_qc_fixture_smoke.py::test_price_history_fixture_has_daily_ohlcv` | 3 | A CSV-header check. The parity test reads the same CSV through `FixtureDataReader` and pins a fill price. |
| `parity/test_qc_fixture_smoke.py::test_equity_fixture_parses` | 1 | Has no assert. `qc_equity.json` is "diagnostic only; not asserted by the reconciler" (`tests/fixtures/golden/qc-aapl-phase3/attribution.md:45`), and nothing else reads it. |
| `parity/test_qc_fixture_smoke.py::test_fixture_is_minute_resolution` | 3 | The parity test pins a fill at one minute (`fill_price == 273.238170408`, `:276`). A daily recapture cannot pass it. |
| `parity/test_qc_fixture_smoke.py::test_fixture_bars_are_tz_aware_ny` | 1 | It converts `start_ms` with `datetime_at_ms(..., tz=NY)` and then asserts that the result is in New York time. It tests its own conversion, not the reader. |
| `parity/test_qc_reconciler.py::test_minute_reader_detects_resolution` | 3 | `::test_minute_audit_flags_fill_outside_bar_range` only reaches its minute-range reason through the `is_minute_resolution` branch (`app/research/parity/qc_reconciler.py:406`). |
| `parity/test_qc_reconciler.py::test_render_markdown_includes_status_and_window` | 2 | Asserts that `"PASSED"` and one date appear in the reviewer's markdown. |

### persistence/

| Item | Kind | Evidence |
|---|---|---|
| `persistence/test_schema.py::test_version_8_rejects_program_authorization_on_a_rejected_review` | 3 | Reads the constraint's DDL text. `::test_version_8_preserves_a_previously_legal_rejected_authorization` proves both halves by behavior: a new row is rejected (`CheckViolationError`) and the old row stays (`NOT VALID`). |
| `persistence/test_schema.py::test_version_6_cascades_a_parity_verdict_with_its_lean_side` | 3 | Reads the catalog's `confdeltype`. `backtest_runs/test_repository.py::test_deleting_the_lean_side_takes_its_parity_verdict_with_it` deletes a row and proves the cascade. |

### recency/

| Item | Kind | Evidence |
|---|---|---|
| `recency/test_stats.py::TestTradeDollarPnl::test_zero_commission_matches_the_gross_default` | 1 | Asserts that the parameter's default is `0.0`. |

### runs/

| Item | Kind | Evidence |
|---|---|---|
| `runs/test_endpoint.py::test_post_then_get_round_trips` | 3 | `runs/test_ema_acceptance.py::test_persisted_run_round_trips_via_get` checks both the ledger and the result over HTTP, using the canonical spec. |
| `runs/test_endpoint.py::test_post_repeat_runs_share_result_hash` | 3 | Has the same six asserts as `runs/test_ema_acceptance.py::test_repeat_runs_produce_identical_content_hashes`. |
| `runs/test_endpoint.py::test_post_response_timestamps_are_int64_ms_utc` | 1 | `isinstance(..., int)` on fields typed `int` (e.g. `app/research/runs/ledger.py:279`) and pinned in the OpenAPI snapshot. |
| `runs/test_endpoint.py::test_list_filter_by_spec_hash` | 3 | `runs/test_ema_acceptance.py::test_list_filter_by_spec_hash_isolates_runs`. |
| `runs/test_endpoint.py::test_list_filter_by_status` | 3 | `runs/test_ema_acceptance.py::test_list_filter_by_status_separates_completed_and_failed`. |
| `runs/test_runner_inmemory.py::test_repeat_runs_produce_identical_hashes` | 3 | `runs/test_ema_acceptance.py::test_repeat_runs_produce_identical_content_hashes`. Equal `result_hash` already implies the equal trade count and final equity it also checks. |
| `runs/test_runner_inmemory.py::test_changing_spec_param_changes_spec_and_result_hash` | 3 | `runs/test_ema_acceptance.py::test_changing_ema5_period_changes_spec_and_result_hash` (same three asserts, run over HTTP). |
| `runs/test_runner_inmemory.py::test_changing_data_window_changes_data_snapshot_id` | 3 | `runs/test_ema_acceptance.py::test_shifting_start_date_changes_data_snapshot_id`. |
| `runs/test_runner_inmemory.py::test_next_session_open_is_a_valid_fill_mode`, `::test_parse_fill_mode_returns_next_session_open_enum_value` | 3 | `::test_normalize_fill_mode_handles_dash_and_case_variants_for_next_session_open` parses the mode to `FillMode.NEXT_SESSION_OPEN`. |
| `runs/test_storage.py::test_save_then_load_restores_ledger_and_result` | 3 | `runs/test_ema_acceptance.py::test_persisted_run_round_trips_via_get`. |
| `runs/test_storage.py::test_save_writes_canonical_json_files` | 3 | `artifact/test_runs_byte_equivalence.py::test_runs_save_matches_pre_pr_golden_bytes`. |
| `runs/test_storage.py::test_load_missing_run_raises` | 3 | `runs/test_endpoint.py::test_get_missing_run_returns_404`. |
| `runs/test_storage.py::test_save_refuses_to_overwrite_existing_run` | 3 | `runs/test_endpoint.py::test_post_run_id_collision_returns_409` proves the mapped 409. |
| `runs/test_storage.py::test_save_replace_overwrites` | 3 | `artifact/test_store.py::test_save_replace_clobbers`. |
| `runs/test_storage.py::test_list_empty_root_returns_empty` | 3 | `runs/test_endpoint.py::test_list_empty_returns_empty_array`. |
| `runs/test_storage.py::test_list_returns_recent_first` | 3 | `artifact/test_store.py::test_list_ids_orders_newest_first`. `list_runs` keeps the store's id order (`app/research/runs/storage.py:106-179`). |
| `runs/test_storage.py::test_list_filter_by_spec_hash`, `::test_list_filter_by_status` | 3 | The two `runs/test_ema_acceptance.py` list-filter tests. |
| `runs/test_storage.py::test_list_filter_by_parent_run_id` | 3 | `runs/test_endpoint.py::test_list_filter_by_parent_run_id`. |
| `runs/test_storage.py::test_list_filter_by_since_ms` | 3 | `artifact/test_store.py::test_list_ids_filter_by_since_ms`. |
| `runs/test_storage.py::test_list_limit_truncates` | 3 | `runs/test_endpoint.py::test_list_limit_truncates`. |
| `runs/test_window.py::test_excluded_day_serializes_date_as_iso_string`, `::test_window_summary_serializes_dates_as_iso_strings` | 1 | These test Pydantic's default `date` JSON encoding. The wire round trip is proven by `runs/test_endpoint.py::test_window_summary_round_trips_through_persist_load`. The end-inclusive assert is proven by `::test_summarize_window_same_day_trading_day_returns_one_session`. |
| `runs/test_hashing.py::test_hash_payload_is_64_hex_chars` | 1 | `hash_payload` returns `hashlib.sha256(...).hexdigest()` (`app/research/runs/hashing.py:42`), so this tests the standard library. |

### sweep/

| Item | Kind | Evidence |
|---|---|---|
| `sweep/test_grid.py::TestExpandGrid::test_expansion_is_lazy` | 3 | Asserts `inspect.isgenerator`. `recency/test_runner.py::TestRunRecencyLazyGridExecution::test_does_not_materialize_the_full_grid_before_execution_starts` proves the laziness as an outcome: cells are pulled one by one as the runs execute. |

### walk_forward/

| Item | Kind | Evidence |
|---|---|---|
| `walk_forward/test_endpoint.py::test_response_timestamps_are_int64_ms` | 1 | Fields typed `int` (`app/research/walk_forward/result.py:169,208`) and pinned in the OpenAPI snapshot. |
| `walk_forward/test_storage.py::test_save_writes_canonical_json` | 3 | The runs golden-bytes test. |
| `walk_forward/test_storage.py::test_load_missing_walk_forward_raises` | 3 | `walk_forward/test_endpoint.py::test_get_missing_walk_forward_returns_404`. |
| `walk_forward/test_storage.py::test_save_replace_overwrites` | 3 | `artifact/test_store.py::test_save_replace_clobbers`. |
| `walk_forward/test_storage.py::test_list_empty_returns_empty` | 3 | `walk_forward/test_endpoint.py::test_list_empty_returns_empty`. |
| `walk_forward/test_storage.py::test_list_orders_by_created_at_desc` | 3 | `artifact/test_store.py::test_list_ids_orders_newest_first`. |
| `walk_forward/test_storage.py::test_list_filter_by_parent_run_id` | 3 | `walk_forward/test_endpoint.py::test_list_filter_by_parent_run_id`. |

### Top-level files

| Item | Kind | Evidence |
|---|---|---|
| `test_indicator_reliability.py::TestFormatDisplayName` (4 tests) | 2 | Pins display labels (`"MACD (12, 26, 9)"`, …) that the router sends to the page (`app/routers/indicator_reliability.py:380`). |
| `test_indicator_reliability.py::TestNextSteps` (5 tests) | 2 | Substring and count checks on advisory prose from `generate_next_steps` (`app/routers/indicator_reliability.py:370`). |
| `test_indicator_reliability.py::TestInfoFootnotes` (3 tests) | 2 | Substring checks on footnote prose. |
| `test_indicator_reliability.py::TestGetIndicatorCategory` (3 tests) | 1 | Pins rows of a lookup table (`rsi` → `momentum`). |
| `test_indicator_reliability.py::TestRandomBaselineReturnsDistribution::test_baseline_default_simulations_is_1000` | 1 | Asserts that `RANDOM_SIMULATIONS == 1000` (`app/research/indicator_reliability.py:26`). |
| `test_quantile.py::TestQuantileAnalysis::test_bins_have_correct_fields` | 1 | `isinstance` checks on typed fields. |

## What the cuts orphan

- **Whole files go:** the three byte-equivalence files under `artifact/` and `ml/test_regression.py`. Their directories keep other tests, and none has a fixture of its own.
- **Golden fixture:** `tests/fixtures/golden/qc-aapl-phase3/qc_equity.json` loses its only reader (`test_equity_fixture_parses`). **Leave it:** it is part of the attributed fixture (`attribution.md:45`). Whether to keep it belongs to the fixture owners (#2737), not to this list.
- **Helpers inside surviving files:**
  - `test_qc_fixture_smoke.py`: `_orders_payload`, `_EQUITY` and the `json` import go unused. Only `test_fixture_first_and_last_minute_timestamps_match_window` remains.
  - `test_indicator_reliability.py`: imports of `format_indicator_display_name`, `get_indicator_category`, `generate_next_steps`, `generate_info_footnotes` and `RANDOM_SIMULATIONS` go unused. The app functions stay, because the router uses them.
  - `options/test_iv_builder.py`: the `MIN_IV`, `MAX_IV` and `MIN_OPTION_PRICE` imports go unused.
  - `runs/test_runner_inmemory.py`: the `_VALID_FILL_MODES` import goes unused.
  - `runs/test_window.py`: the `ExcludedDay` import probably goes unused.
  - The `documentation/` files: several imported catalog symbols go unused.
  - Run `ruff check PythonDataService/app/ PythonDataService/tests/` to catch the rest.
- **Conftest:** `tests/research/conftest.py`, `parity/conftest.py` and `backtest_runs/payloads.py` stay; every one still has users.
- **App code:** nothing becomes test-only. Every function above that loses a test still has a live caller or a surviving test.

## Hazards the cutting PR must carry

1. **The metric catalog cites test node ids.** `documentation/test_analytical_metric_catalog_entries.py::test_all_native_entries_carry_pinned_provenance_and_existing_evidence_receipts` fails if a cited `validating_tests` node disappears. The nodes cited in this area are `backtest_runs/test_repository.py::test_an_engine_run_survives_a_write_then_read_round_trip_with_every_field`, `::test_the_report_carries_the_newest_five_hundred_trades_in_entry_order_and_says_so` and `documentation/test_analytical_metric_catalog_entries.py::test_lean_trade_sentinels_match_the_canonical_reproducer` (`app/research/documentation/analytical_metric_catalog_results_entries.py:133,141`, `analytical_metric_catalog_entries.py:32`). All three stay. Re-run that test after the cut anyway.
2. **Docs and docstrings name the cut tests.**
   - `app/research/parity/qc_reconciler.py:36` calls `test_qc_fixture_smoke.py` the "Branch-A/B fee-presence gate". After the cut, that gate is the parity test's own assert.
   - `docs/ml-predictions-authority.md:326,328` describe the smoke and commission files.
   - `docs/references/qc-aapl-phase3-capture-runbook.md:274` runs the smoke file. It still exists, with one test.
   - `app/research/parity/ibkr_commission.py:24` and `app/engine/execution/commission.py:14` cite `test_ibkr_commission.py`. That file survives with six tests.
   - The byte-equivalence docstrings cite the "Per-PR acceptance bar" in `docs/architecture/research-artifact-seam.md`.
   - `runs/test_ema_acceptance.py`'s docstring says its mechanics are "exhaustively covered" elsewhere. After this cut, it is the main proof. A one-line docstring fix is optional.
   - Run `pytest tests/contracts` (the docs link contract) after any docs edit.
3. **Some tests skip silently without Postgres.** `backtest_runs/test_service_db.py`, `backtest_runs/test_repository.py`, `backtest_runs/test_parity.py` and `persistence/test_schema.py` need `POSTGRES_URL` plus `POSTGRES_URL_IS_EPHEMERAL=1`. Without them they show green while skipped. Run the survivors against an ephemeral database before calling the cut clean.
4. **CI names two files directly.** The scientific-proof step names `tests/research/parity/test_qc_reconciler.py` and `test_cross_engine_study.py` (`.github/workflows/ci.yml:371-372`). Both stay. `tests/research/ml` is a `FAST_TEST_PATHS` directory (`scripts/run_fast_tests.py:52`); deleting one file under it needs no edit there.
5. **No contract regeneration.** Only tests go. No OpenAPI or GraphQL snapshot changes.
6. **Kill lists age.** Re-check each row's survivor at the cutting PR's own SHA. A duplicate row is valid only while its named survivor still exists and still asserts what this list says.

## Pointers outside this area

- **#2706 / #2710 (route cascade).** #2710 says `/api/research/run-feature`, `run-signal`, `run-options-feature`, `run-batch-options` and `build-iv-history` lost their Backend callers. `test_endpoint.py::test_run_feature_success`, `::test_run_feature_too_few_bars` and `::test_run_feature_invalid_body` drive `run-feature`. If those routes are cut, the three tests go with them as kind 5. The engines behind the routes stay live through `/api/jobs`, so `test_runner.py`, `test_options_runner.py`, `test_signal_engine.py` and `options/*` are unaffected.
- **#2730 (router tests).** `tests/routers/test_backtest_runs_endpoints.py`, `test_golden_validation_endpoints.py` and `test_recency_endpoints.py` sit one layer above `backtest_runs/`, `golden_validation/` and `recency/` here. I did not cross-check them (see Not reviewed).
- **#2742 (comment citations, app code).** The docstrings in hazard 2: `qc_reconciler.py:36`, `ibkr_commission.py:24`, `commission.py:14`.
- **#2740 / #2741 (second docs pass).** `docs/ml-predictions-authority.md:326,328`, `docs/references/qc-aapl-phase3-capture-runbook.md:274`, and the finished-migration "Per-PR acceptance bar" in `docs/architecture/research-artifact-seam.md`.
- **#2737 (golden-fixture suite).** The orphaned `qc-aapl-phase3/qc_equity.json`. Also, `test_qc_fixture_smoke.py` still carries a module-level `skipif` for a fixture that has landed.

## Not reviewed

- **Depth.** I judged every test from its name, docstring and assert lines. I opened the body only for candidates and for the tests they would duplicate. A test whose asserts look like outcomes but rest on a hollow setup would be missed. For example, in `golden_validation/test_service.py`, `grid_search/test_service.py`, `sweep/test_warmup.py` and `walk_forward/test_runner.py` I read the mock-flagged tests by their asserts, not their full bodies.
- **Cross-suite duplicates.** I did not check against tests outside `tests/research/`, except where a row names one. That leaves out `tests/routers/` (#2730), `tests/services/`, `tests/engine/` and `tests/fixtures/`.
- **Parametrize cases.** Not judged one by one; a parametrized test counts once.
- **`artifact/test_store.py::test_hash_callback_invoked_when_present` / `::test_hash_callback_not_invoked_when_absent`.** These record calls in a module-level list. I did not decide whether the outcome they track (`hash_payload` being called) is caller-visible.
- **Weak-but-kept tests.** I left these in place: smoke checks that assert only `is not None`, a non-empty list or `> 0` on a populated report section (`test_options_runner.py::TestOptionsRunner::test_report_has_*`, `options/test_diagnostics.py::TestIvDiagnostics::test_distribution_stats_computed` / `::test_date_coverage`, `test_robustness.py::TestMonthlyICBreakdown::test_each_month_has_stats`), and the per-phase malformed-id storage tests (the endpoint traversal tests accept `{400, 404}`, so they are weaker). A stricter pass may cut some of them.
- **Conftest fixtures and `backtest_runs/payloads.py`.** Not judged as items. They stay while tests use them.
