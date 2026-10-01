# Kill list: LEAN sidecar, market data, data lake, jobs and utils tests (#2728)

Part of map #2700. **Plan, don't cut.** Nothing here has been deleted.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30). The map was charted at `87b8e261`; the blocking kill lists were read at that SHA.
- **Area:** `PythonDataService/tests/lean_sidecar/`, `tests/marketdata/`, `tests/data_lake/`, `tests/jobs/`, `tests/utils/` (about 18K lines, 773 test functions).
- **Paths** are relative to `PythonDataService/`.
- **Kinds:** 1 trivial, 2 copy or doc pinning, 3 duplicate, 4 mock theater, 5 retired feature.

## How this was judged

1. **Inventory.** An AST pass listed every test with its docstring, its assert lines and some flags: mock use, `assert_called*`, reading source text, long string literals in asserts, isinstance/hasattr-only asserts, no asserts. Every test was triaged from that listing. Bodies were read for every suspect.
2. **Duplicates.** A duplicate needed a named survivor that proves the same outcome. I looked one layer up and one layer down: router model ↔ service dataclass, wrapper ↔ hashlib, port ↔ IBKR feed, source-text pin ↔ golden parity test. Each survivor was confirmed by AST to exist at this SHA.
3. **Sacred.** Money-path and golden-parity outcome tests stay. The IBKR feed tests (`tests/marketdata/`) also stay. Inside them only trivial, wording and duplicate tests go. That applies ★ ("sacred is the behavior, not the file") to the sacred IBKR feed the same way it applies to the money path.
4. **Skipped.** Everything the dead-code tickets already list is skipped (next section).

## Skipped: owned by the dead-code tickets

- **#2706 (routes).**
  - Whole files: `tests/lean_sidecar/test_compare_endpoint.py`, `test_determinism_gate.py`, `test_log_tail_endpoint.py`.
  - In `test_router_lean_sidecar.py`, the dead-route classes `TestCalendarBlockedDatesEndpoint`, `TestInspectionEndpoints`, `TestRunsIndex`, `TestPostReconcileEndpoint` and `TestPostCrossReconcileEndpoint` (see hazard H6).
  - The `observations`/`log` read in `test_router_lean_sidecar_e2e.py`.
  - `tests/marketdata/test_feed.py::test_health_endpoint_returns_feed_health` and `::test_health_endpoint_503_when_feed_not_installed`.
- **#2704 (engine, LEAN sidecar).**
  - `tests/lean_sidecar/test_cross_reconciler.py` and `test_reconciler.py`. Their modules are conditional rows, and their gate fires because #2706 cuts both routes.
  - The `BuyAndHoldStrategy` uses in `test_cross_runner.py` (but see H5).
  - Tests of `DataPolicyManifest`, `StagedRun` and `list_factor_map_files`, if any. I found none in this area.
  - `HISTORICAL_LEAN_IMAGE_DIGEST_ARM64` is also on #2704's list, but that row is a false positive (H4).
- **#2705 (research, data lake).** `tests/jobs/test_phases.py::TestFriendlyLabels::test_total_weight_sums_correctly`, `::test_total_weight_unknown_job_zero`, and the `renew_lease(...)` line at `tests/jobs/test_job_lease.py:61`.

## Kill list

### utils

| Path | Kind | Evidence |
|---|---|---|
| `tests/utils/test_timestamps.py::test_utc_epoch_zero` | 3 | Same `to_ms_utc(UTC datetime)` path, one literal apart, as `::test_known_utc_moment`. |

### jobs

| Path | Kind | Evidence |
|---|---|---|
| `tests/jobs/test_phases.py::TestFriendlyLabels::test_known_phase_returns_label` | 2 | Asserts three label strings equal the phase table's copy (`"Measuring information coefficient"`, …). The lookup logic it could prove is the fallback, which `::test_unknown_phase_falls_back_to_humanized_id` covers. |
| `tests/jobs/test_runner_callbacks.py::TestFeatureRunnerCallbacks::test_logs_include_friendly_messages` | 2 | Asserts that log text contains `"information coefficient"` and `"stationarity"`. The phases the UI shows are proven by `::TestFeatureRunnerCallbacks::test_emits_expected_phase_sequence`. |

### data lake

| Path | Kind | Evidence |
|---|---|---|
| `tests/data_lake/test_polygon_payload_status.py::test_payload_status_ok_is_success` | 3 | An `OK` payload passing is proven through the full fetch by `tests/unit/data_lake/test_polygon_fetcher.py::test_single_page_response`. The file's actual regression (`DELAYED`) and its error case stay. |

### market data (IBKR feed; behavior tests all stay)

| Path | Kind | Evidence |
|---|---|---|
| `tests/marketdata/test_feed.py::test_market_data_bar_timestamps_are_ints` | 1 | Builds a `MarketDataBar` from int literals and asserts they are ints. The type check on a translated bar is `::test_translate_produces_int64_ms_utc_timestamps`. |
| `tests/marketdata/test_feed.py::test_feed_health_timestamps_are_ints` | 1 | Same, for a hand-built `FeedHealth`. Real `health()` output is int-checked in `::test_health_connected_no_bars` (`:470`). |
| `tests/marketdata/test_feed.py::test_ibkr_feed_satisfies_market_data_feed_protocol` | 1 | `hasattr`/`callable` on `feed_id`, `stream_bars` and `health`, which every stream and health test calls. |
| `tests/marketdata/test_feed.py::test_market_data_bar_provenance_defaults_to_realtime` | 1 | Asserts dataclass field defaults. The provenance a real bar carries is proven by `::test_translate_maps_ibkr_provenance_to_port_provenance`. |
| `tests/marketdata/test_feed.py::test_market_data_feed_error_carries_a_typed_reason` | 2 | Pins the `str(error)` shape `"DECISION_BAR_MISSED: deadline passed"`, which only reaches logs (`bot_runner.py:2319`). Typed reasons are asserted on real refusals, for example `test_feed_continuity.py::test_deadline_passing_during_the_wait_is_decision_bar_missed`. `reason is None` is asserted by `::test_interruption_before_any_delivered_bar_is_fatal_as_today`. |
| `tests/marketdata/test_feed.py::test_continuity_policy_accepts_the_extended_session` | 3 | Builds an extended policy and reads its fields back. `test_feed_continuity.py::test_an_unresolvable_pre_market_minute_is_fatal_for_an_extended_run` streams under the same policy (`_extended_policy`, `:99`). |
| `tests/marketdata/test_feed.py::test_stream_bars_accepts_continuity_none_and_behaves_as_before` | 3 | `continuity` defaults to `None` (`app/marketdata/ibkr_feed.py:214`), so every legacy stream test already runs this path, for example `::test_two_consumers_receive_identical_bars`. |

### LEAN sidecar

| Path | Kind | Evidence |
|---|---|---|
| `tests/lean_sidecar/parity_matrix/test_manifest.py::test_sha256_of_text_stable` | 1 | Asserts that the wrapper equals `hashlib.sha256`. |
| `tests/lean_sidecar/parity_matrix/test_manifest.py::test_sha256_of_file` | 1 | Same. |
| `tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py` (whole file, 1 test) | 2 | Greps the regeneration script's source for brokerage and fee strings. The committed cell `manifest.json` files record the broker block. `tests/research/parity/test_cross_engine_study.py::test_cross_engine_cell` gates fees, because `assert_fees=True` is the default (`app/lean_sidecar/parity_matrix/cell_runner.py:63`). |
| `tests/lean_sidecar/test_cross_runner_fee_wiring.py` (whole file, 1 test) | 2 | Greps `cross_runner.py` source for `fee_model=IbkrEquityCommissionModel()`. Its own docstring names `test_cross_engine_study.py` as the end-to-end proof. A miswired fee fails Gate 3 there (`COMMISSION_DRIFT`), and the smoke cells run on every PR. |
| `tests/lean_sidecar/test_deployment_validation_template.py::test_source_is_non_empty_string` | 1 | `isinstance(str)` and `len > 100`. |
| `tests/lean_sidecar/test_deployment_validation_template.py::test_source_parses_as_valid_python` | 3 | `::test_source_contains_required_handlers` runs `ast.parse` on the same source. |
| `tests/lean_sidecar/test_ema_crossover_template.py::test_source_is_non_empty_string` | 1 | `isinstance(str)` and `len > 100`. |
| `tests/lean_sidecar/test_ema_crossover_template.py::test_source_parses_as_valid_python` | 3 | `::test_class_constants_match_spec` runs `ast.parse` on the same source. |
| `tests/lean_sidecar/test_ema_crossover_template.py::test_signal_template_reuses_the_single_lean_source_of_truth` | 3 | `::test_signal_template_emits_the_parameterized_base_source` makes the same `is` assertion plus more. |
| `tests/lean_sidecar/test_hardening_profile.py::test_run_limits_unused_import_silenced` | 1 | `assert RunLimits is not None`. Its job is to silence an unused import. |
| `tests/lean_sidecar/test_hardening_profile.py::test_profile_mapping_is_a_strict_subset_of_token_allow_list` | 3 | `tokens_for_profile` returns `HARDENING_PROFILE_TOKENS[profile]` (`app/lean_sidecar/runner.py:157`), so `::TestProfileMapping::test_every_profile_expands_only_to_allow_listed_tokens` (parametrized over every profile) makes the same check. |
| `tests/lean_sidecar/test_hardening_profile.py::TestLaunchRequestModel::test_hardening_profile_accepts_valid_enum_value` | 1 | Pydantic accepting an enum member. Validation wiring is proven by `::TestLaunchRequestModel::test_hardening_profile_rejects_unknown_value`. |
| `tests/lean_sidecar/test_hardening_profile.py::TestLaunchRequestModel::test_hardening_profile_accepts_applehv_dotnet_fix_value` | 1 | Same, for another enum member. |
| `tests/lean_sidecar/test_lake_mount.py::test_lake_artifacts_are_immutable` | 1 | Asserts that a frozen dataclass rejects assignment, which is Python's own behavior. |
| `tests/lean_sidecar/test_launcher_auth.py::test_token_file_path_lives_at_artifacts_root` | 3 | `::test_default_token_file_path_tracks_configured_artifacts_root` asserts the same `<root>/.launcher-token` path through the configured root. |
| `tests/lean_sidecar/test_launcher_service.py::TestWorkspaceSizeEnforcement::test_over_cap_detectable` | 3 | Asserts only `size > 2 MiB` on the same walk that `::TestWorkspaceSizeEnforcement::test_under_cap_passes` sums exactly. The over-cap rejection is `::TestWorkspacePollerIntegration::test_poller_fires_mid_run_returns_rejected`. |
| `tests/lean_sidecar/test_lean_sidecar_service.py::test_completed_run_warns_when_source_changes_after_launch` | 2 | Asserts two log phrases. The outcome, that the run still persists, is `::test_completed_run_persists_after_source_changes_mid_run`. |
| `tests/lean_sidecar/test_lean_sidecar_service.py::test_trusted_run_request_exposes_symbol_via_data_policy` | 3 | `::test_trusted_run_request_carries_data_policy` asserts the same `symbol`-from-`data_policy` accessor plus the removed legacy fields. |
| `tests/lean_sidecar/test_lean_sidecar_service.py::test_trusted_run_request_accepts_polygon_data_source` | 1 | Builds a dataclass and reads back the field it set. |
| `tests/lean_sidecar/test_lean_sidecar_service.py::test_runtime_polygon_adjustment_is_raw_for_adjusted_false` | 1 | The function is typed `Literal["raw"]` and returns `"raw"` unconditionally (`app/services/lean_sidecar_service.py:480`). The regression case is `::test_runtime_polygon_adjustment_is_always_raw_for_adjusted_true`. |
| `tests/lean_sidecar/test_manifest.py::TestHashing::test_sha256_bytes_matches_hashlib` | 1 | Asserts that the wrapper equals `hashlib`. (`::test_sha256_file_matches_bytes` stays: it exercises the chunked read.) |
| `tests/lean_sidecar/test_manifest.py::TestHashing::test_sha256_text_uses_utf8` | 1 | Asserts that the wrapper equals `hashlib`. |
| `tests/lean_sidecar/test_manifest.py::TestNowMsUtc::test_staged_data_file_is_json_serializable` | 1 | Asserts the field it just set and `json.dumps` of a placeholder. Its own comment says the real exercise is `::TestWriteManifest::test_roundtrip_is_sorted_pretty_json`. |
| `tests/lean_sidecar/test_manifest.py::test_data_policy_round_trips_synthetic_shape` | 1 | Builds a dataclass and reads its fields back. |
| `tests/lean_sidecar/test_manifest.py::test_manifest_schema_version_is_5` | 1 | `MANIFEST_SCHEMA_VERSION == 5`. |
| `tests/lean_sidecar/test_polygon_canonical.py::test_get_default_provider_returns_polygon_provider` | 1 | Checks the return type of a one-line factory. |
| `tests/lean_sidecar/test_quote_bar_staging.py::TestWriteLeanQuoteDayZip::test_ms_offset_from_et_midnight` | 3 | `::test_ms_encoding_does_not_drift_with_utc_offset` asserts the same 09:30 → `34_200_000` encoding on both a non-DST and a DST day. |
| `tests/lean_sidecar/test_runner.py::TestBuildCommand::test_is_rootless_podman_does_not_shell_out_to_podman` | 4 | Asserts only that `subprocess.run` is never called. The detection outcome is proven by `::TestBuildCommand::test_is_rootless_podman_true_when_euid_non_root` and `::test_is_rootless_podman_false_when_euid_root`. |
| `tests/lean_sidecar/test_runner.py::TestKillReason::test_enum_has_expected_members` | 3 | Pins the enum string values. The wire reason is asserted by `test_launcher_service.py::TestWorkspacePollerIntegration::test_poller_fires_mid_run_returns_rejected` (`workspace_max_mb_exceeded`), and the timeout reason by `::TestKillReason::test_timeout_path_threads_wall_clock_reason`. |
| `tests/lean_sidecar/test_staged_window.py::test_single_non_dst_day_is_exactly_24_hours` | 3 | `::TestStagedWindowFromDates::test_single_day_envelopes_one_et_day` asserts the same `86_400_000` plus the ET-midnight anchors. Only the anchors catch the UTC-offset bug its docstring names. |
| `tests/lean_sidecar/test_template_selection.py::test_trusted_default_template_is_the_dataclass_default` | 3 | `test_router_lean_sidecar.py::TestTemplateSelection::test_template_defaults_to_trusted_default` pins the same default at the API boundary callers hit. |
| `tests/lean_sidecar/test_template_selection.py::test_template_maps_default_to_algorithm_default_policy` | 1 | Restates one entry of `TRUSTED_TEMPLATE_DEFINITIONS`. It is not a parity setting: `trusted_default` is the non-reconciliation template. |
| `tests/lean_sidecar/test_template_selection.py::test_default_template_stages_legacy_buy_and_hold_source` | 1 | Restates one table entry (`.source == BUY_AND_HOLD_SOURCE`). |
| `tests/lean_sidecar/test_template_selection.py::test_request_accepts_known_templates` | 1 | A plain dataclass constructor stores what it was given. No validation runs. |
| `tests/lean_sidecar/test_router_lean_sidecar.py::TestTemplateSelection::test_template_accepts_reconciliation` | 1 | Pydantic accepting a `TrustedTemplate` member (`routers/lean_sidecar.py:305`). Rejection wiring is `::TestTemplateSelection::test_template_rejects_unknown_value`. |
| `tests/lean_sidecar/test_router_lean_sidecar.py::TestTemplateSelection::test_template_accepts_deployment_validation` | 1 | Same, for another enum member. |
| `tests/lean_sidecar/test_workspace_poller.py::test_default_poll_interval_constant_exists` | 1 | `_WORKSPACE_POLL_INTERVAL_S == 1.0` plus a check that a dataclass field is absent. |

## Kept on purpose (so the next reader does not re-derive them)

| What | Why it stays |
|---|---|
| Source-text pins of the LEAN reference algorithms: `test_ema_crossover_template.py` (constants, CSV headers, brokerage, no `SetWarmUp`), `test_deployment_validation_template.py` (constants, brokerage), `test_template_selection.py` reconciliation-source pins (`fillForward=False`, `DataNormalizationMode.Raw`, IB brokerage, `class MyAlgorithm`) | These are the LEAN side of the golden cross-engine fixtures. No CI job runs LEAN (`requires_lean_image`), so these pins are the only guard against the reference drifting between regenerations. I read them as math-parity plumbing, not copy (see "For the map"). |
| `tests/lean_sidecar/parity_matrix/test_matrix.py` (CELLS count, tickers, windows, dates) | `test_cross_engine_cell` *skips* when a cell directory is missing. An edit to `CELLS` would change every `cell_id` and turn the golden suite green by skipping. These pins are the only alarm. |
| `tests/lean_sidecar/test_image_allowlist.py` | Proves an outcome: the historical digest is refused for launcher runs but authorized for fixture regeneration (H4). |
| `tests/lean_sidecar/test_trading_calendar.py` | Tests of the canonical calendar module. Each test pins a distinct date or DST boundary. `blocked_dates_in_range` and `holiday_names_in_range` stay live through `app/research/runs/window.py:90`. |
| `tests/marketdata/test_feed.py`: the `issubclass` pair and `test_market_data_bar_carries_no_ibkr_types` | They guard the port boundary. The translation test would still pass if the exception hierarchy leaked. |
| `tests/marketdata/test_feed.py::test_ibkr_feed_exposes_its_capability_account_identity`, `::test_health_active_subscription_count_in_response` | The first is the only proof that the real feed exposes its account (liveness tests fake it). The second is the only non-zero check of the *aggregate* `health()` count. |
| `tests/jobs/test_cancellation_check.py::TestDefaults` | The default of 1 is the only guard that a mid-run cancel is seen at the next check (#2463). There is no behavioral twin. |
| `tests/lean_sidecar/test_runner.py::TestRunLimits::test_default_run_limits_validates` | The only proof that `DEFAULT_RUN_LIMITS` passes its own validation. |
| `tests/data_lake/test_factor_files*.py` | Golden factor-file parity and coverage-refusal outcomes. |

## What the cuts orphan

- **Whole files deleted:** `tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py` and `tests/lean_sidecar/test_cross_runner_fee_wiring.py`.
- **Unused imports and helpers left in surviving files** (ruff flags them; remove in the same PR):
  - `test_feed.py`: `FeedHealth`.
  - `parity_matrix/test_manifest.py`: `hashlib`, `Path`, `sha256_of_file`, `sha256_of_text`.
  - `test_hardening_profile.py`: `RunLimits`, `HARDENING_PROFILE_TOKENS`.
  - `test_lake_mount.py`: `LakeArtifacts`.
  - `test_manifest.py`: `sha256_bytes`, `sha256_text`.
  - `test_staged_window.py`: `pytest`.
  - `test_template_selection.py`: `pytest`, `BUY_AND_HOLD_SOURCE`, and the helpers `_request` and `_default_data_policy` with the imports only they use.
- **Nothing in `app/`, no fixture and no conftest is orphaned.** The hashing helpers are called by `app/lean_sidecar/manifest.py`, `app/services/lean_sidecar_service.py`, `parity_matrix/manifest.py` and `scripts/regenerate_cross_engine_study.py`. `get_default_provider`, `MANIFEST_SCHEMA_VERSION`, `KillReason` and `TRUSTED_TEMPLATE_DEFINITIONS` have production callers. `tests/lean_sidecar/fixtures/*.json` stays (read by `test_normalized_parser.py`). Both conftests stay.
- **`scripts/pr_shard_durations.json`:** stale entries are harmless (per #2707). Regenerate it after the cuts land.

## Hazards the cutting PR must carry

- **H1. Skip-green LEAN tests.** `tests/lean_sidecar/conftest.py:61-73` silently skips `requires_lean_image` and `requires_podman` tests when podman or the pinned image is missing. These include `test_runner_e2e.py`, `test_security_flags.py` and `test_router_lean_sidecar_e2e.py`. None is on this list. A skip is not a pass. Separately, `test_cross_engine_cell` skips any cell without a committed fixture: 6 of 16 cells have none (AAPL and TSLA W3mo, all four W24mo). That is why `test_matrix.py` stays.
- **H2. Postgres gating.** No test in this area is Postgres-gated (`tests/data_lake/` is file-based). Every survivor named above runs without a database.
- **H3. Answer to #2705's H3** ("covered window, zero provider calls" on a natively written lake):
  - Proven for **run materialization** by `tests/unit/data_lake/test_run_materialization.py::test_materialize_run_data_reuses_the_bytes_on_a_second_run`.
  - Proven for **backfill** by `tests/integration/data_lake/test_gate_chain_convergence.py::test_backfill_window_a_then_wider_window_b_converges_through_the_gate_chain`, which is Postgres-gated.
  - **Not proven** for the chart path or the engine-backtest path. `test_flag_flip_parity.py::test_chart_serves_a_covered_completed_window_with_zero_provider_calls` and `::test_engine_backtest_over_an_imported_window_makes_zero_provider_calls` have no native-lake twin anywhere I searched. Raise this before deleting that file; do not lose the proof silently.
- **H4. #2704 false positive: `HISTORICAL_LEAN_IMAGE_DIGEST_ARM64` is not test-only.** `app/lean_sidecar/config.py:152-154` builds `RECONCILIATION_FIXTURE_IMAGE_DIGESTS` from it, and #2704 itself keeps that set as fixture-regeneration plumbing. The cutting PR must keep the constant and `tests/lean_sidecar/test_image_allowlist.py`.
- **H5. #2704's conditional `BuyAndHoldStrategy` row runs into ☆.** `BuyAndHoldStrategy` is the vehicle for live `cross_runner` tests in `test_cross_runner.py`:
  - `test_buy_and_hold_emits_one_buy_event_against_staged_workspace`;
  - `test_subsequent_events_are_not_extra_buys`;
  - `test_missing_workspace_data_raises`;
  - `test_pinned_dates_override_strategy_defaults`;
  - the resolver tests at `:68` and `:79`.

  `cross_runner` stays, because the cross-engine fixture regenerator uses it. Under ☆ (test-only helpers follow their tests), `BuyAndHoldStrategy` stays even if #2706 cuts cross-reconcile. Cutting it would force rewrites of surviving tests.
- **H6. #2706's router count.** By my route mapping the five dead-route classes hold **56** tests, not 51. The extra five include the `_parse_categories_note` tests (`TestRunsIndex::test_lean_error_categories_filters_unknown_buckets`, `::test_lean_error_categories_malformed_note_falls_back_empty`) and `TestPostCrossReconcileEndpoint::test_response_model_exposed_in_openapi_schema`. Take the classes whole.
- **H7. LEAN determinism coverage.** When #2706 deletes `test_determinism_gate.py`, nothing in this area pins LEAN run determinism. It was already `requires_lean_image` + `slow`, so CI never ran it.
- **H8. Feed tests are sacred by behavior.** The `tests/marketdata/` cuts are trivial, wording or duplicate tests only. Re-check each survivor at the cutting SHA before deleting; the feed files change often (#2364, #2393, #2444).
- **H9. Kill lists age.** Re-run the survivor checks at the cutting PR's own SHA.

## Pointers: outside this area

- **#2706 (routes and schemas).** `TrustedRunRequestModel` still accepts a legacy top-level shape "for one deprecation cycle". The tests are at `tests/lean_sidecar/test_router_lean_sidecar.py:2123-2309` (`test_trusted_run_request_model_accepts_legacy_top_level_shape` and its siblings). The Frontend has a `data-policy` model. If no caller sends the legacy shape, that branch and its tests are kind 5. I did not check every caller.
- **#2726 / #2737 (golden-fixture suite).** `tests/research/parity/test_cross_engine_study.py::test_cross_engine_cell` permanently skips 6 cells with no fixture. Either regenerate them or drop them from `CELLS`. Dropping them means updating `test_matrix.py` in the same PR.
- **#2729 (unit and integration tests).** The H3 finding concerns `tests/integration/data_lake/test_flag_flip_parity.py` and its native-lake twins in `tests/unit/data_lake/`.
- **#2738 (CI shape).** `tests/lean_sidecar/test_workspace_poller.py::TestWorkspacePollerWalkCost::test_walk_10k_files_under_500ms` asserts a 500 ms wall-clock budget. It is not one of the five kinds, but it is a timing test that can flake under load.

## For the map

- **Reading, not a ruling: LEAN reference-algorithm source pins.** I treated substring and AST pins of the LEAN trusted-sample sources as math-parity plumbing (kept), not copy pinning (cut). CI never runs LEAN, so they are the only drift guard on the reference side of the golden cross-engine fixtures. The rule rewrite may want to say this outright, next to the existing reading that a fixture's regeneration script is sacred with it.
- **Conflicts with closed tickets:** H4 (#2704 `HISTORICAL_LEAN_IMAGE_DIGEST_ARM64` is live) and H5 (#2704's `BuyAndHold` conditional row against ☆). H3 answers #2705's open question.

## Not reviewed

- **Bodies of behavior-named tests.** Every test was triaged by name, docstring and assert lines; bodies were read only for suspects. A test whose name and asserts look like outcomes but whose setup mocks the subject away would be missed. This matters most for `test_lake_mount_service.py` (24 heavily mocked tests, 1,240 lines), `test_feed_continuity.py` and `test_feed_ibkr_data_loss_1101.py`.
- **Parametrize cases.** Individual cases were not split. For example, `test_result_classifier.py::TestClassifiedErrorsContract::test_empty_inputs_are_clean[CLEAN_LOG]` repeats `::TestClassifyLeanLog::test_clean_log_returns_clean`.
- **Tests owned by other tickets** (the "Skipped" section) were not re-judged.
- **Which suite runs where.** Only markers were checked, not the PR-versus-daily selection in `scripts/run_fast_tests`.
