# Kill list: unit, helper and integration tests (#2729)

Part of map #2700. **Plan, don't cut**: nothing here has been deleted.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30). That is newer than the map's charting SHA `87b8e261`. The blocking kill lists (#2704, #2705, #2706) were read at `87b8e261`.
- **Area:** `PythonDataService/tests/unit/`, `tests/_helpers/`, `tests/integration/` (95 files, 23.6K lines, about 640 tests).
- **Paths** are relative to `PythonDataService/tests/` unless they say otherwise.
- **Kinds:** 1 trivial · 2 copy or doc pinning · 3 duplicate · 4 mock theater · 5 retired feature (owned by the dead-code tickets).

## How this was checked

1. **Every test, by script.** An AST pass printed each test's name, docstring and assert lines. A second pass flagged tests with no assert, only `isinstance`/`is not None` asserts, prose-matching asserts, mock-call or `call_count` asserts, or reads of source and docs. Each flagged test was then read by hand.
2. **Duplicates, one layer up and one down.** Model ↔ route (`DataRunSpec` ↔ `POST /api/data-lake/ensure-data`), unit ↔ Postgres integration, `tests/unit/data_lake/` ↔ `tests/data_lake/` (owned by #2728), and `tests/unit/routers/` ↔ `tests/utils/test_timestamps.py`. Each pair was read side by side before a test was called the weaker one.
3. **Helpers.** Each `tests/_helpers/` module's importers were counted with `grep -rlE` across `tests/`, `app/` and `scripts/`. All 17 have live importers.
4. **Skipped**: everything #2704, #2705 and #2706 already list (see "Already owned" below).

## Kill list

| # | Item | Kind | Evidence |
|---|---|---|---|
| 1 | `unit/data_lake/test_types.py::TestArtifactIdentityDataRootId::test_explicit_value_is_preserved` | 1 | Asserts a Pydantic model returns the `data_root_id` it was given. The sibling default test is the one that proves behavior. |
| 2 | `unit/data_lake/test_types.py::TestArtifactRecordDataRootId::test_explicit_value_is_preserved` | 1 | Same as row 1, for `ArtifactRecord`. |
| 3 | `unit/data_lake/test_types.py::TestCalendarAnchorHelpers::test_anchor_hour_is_noon_utc` | 1 | `assert CALENDAR_ANCHOR_UTC_HOUR == 12` pins a constant. The anchor is proven by `::test_forward_produces_the_documented_epoch_ms_example` and the round-trip tests. |
| 4 | `unit/data_lake/test_types.py::TestDataRunSpec::test_lowercase_symbol_is_rejected` | 3 | Stronger: `integration/data_lake/test_ensure_data_route.py::test_post_ensure_data_422_on_bad_symbol`. It sends the same `"spy"` through the real router and model and asserts the 422 the caller sees. Neither needs Postgres. |
| 5 | `…::TestDataRunSpec::test_start_after_end_is_rejected` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_reversed_range_rejected`. |
| 6 | `…::TestDataRunSpec::test_5_year_range_cap` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_range_over_max_cap_rejected`. |
| 7 | `…::TestDataRunSpec::test_old_field_names_are_rejected` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_old_field_names_rejected`. |
| 8 | `…::TestDataRunSpec::test_iso_strings_on_the_ms_fields_are_rejected` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_iso_strings_on_ms_fields_rejected`. |
| 9 | `…::TestDataRunSpec::test_non_integer_values_are_rejected` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_non_integer_value_rejected`. |
| 10 | `…::TestDataRunSpec::test_values_outside_signed_int64_are_rejected` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_value_outside_signed_int64_rejected`. |
| 11 | `…::TestDataRunSpec::test_off_anchor_milliseconds_are_rejected` | 3 | Stronger: `test_ensure_data_route.py::test_ensure_data_off_anchor_milliseconds_rejected`. |
| 12 | `integration/data_lake/test_ensure_data_route.py::test_ensure_data_body_shape_accepted_for_boundary_dates` (4 params) | 3 | Its own docstring says it validates `DataRunSpec` in-process, not through the route. It repeats `unit/data_lake/test_types.py::TestDataRunSpec::test_{est_date_range,edt_date_range,weekend_boundary,holiday_boundary}_accepted` with the same dates. |
| 13 | `integration/data_lake/test_ensure_data_route.py::test_route_404_when_flag_off` | 1 | `DATA_LAKE_ENABLED` was retired in #1893 (`app/main.py:1418-1420`: "Registered unconditionally"). The test builds a FastAPI app with no router and checks that it returns 404. That proves FastAPI, not the product. |
| 14 | `integration/data_lake/test_observatory_endpoints.py::test_observatory_routes_404_when_flag_off` (3 params) | 1 | Same retired flag and the same router-less app as row 13. |
| 15 | `integration/data_lake/test_observatory_endpoints.py::test_coverage_finds_a_seeded_quote_artifact_as_complete` | 3 | Its docstring says it "pins the router's own provider-derivation wiring". That wiring is `::test_coverage_derives_provider_from_data_type`, parametrized over `quote`. The status mapping is `::test_coverage_reflects_catalog_status_per_day`. Both run without Postgres. |
| 16 | `integration/data_lake/test_observatory_endpoints.py::test_coverage_builds_one_schedule_for_the_whole_range` | 4 | It counts calls to `session_windows_ms_utc` and asserts exactly one. The latency it was guarding is not asserted. The response it checks (5 days) is already proven by `::test_coverage_keys_days_by_canonical_calendar_sessions_only`. |
| 17 | `integration/data_lake/test_ensure_data_real_polygon.py` (whole file, 2 tests) | 3 | Stronger: `integration/data_lake/test_ensure_data_all_kinds.py`, which uses the same harness (real Postgres, respx Polygon and launcher, tmp lake). Its `::test_ensure_data_all_kinds_complete` asserts `overall_status == "complete"`, all 15 artifacts, files on disk and real hashes. This file accepts `{"complete","partial"}`. `::test_ensure_data_second_call_is_cache_hit` asserts an identical hash and `fetched_artifact_count == 0`. This file asserts only `reused_artifact_count >= 1`. |
| 18 | `integration/test_engine_persistence_data_policy.py::test_engine_backtest_request_accepts_data_policy_block` | 1 | Builds `EngineBacktestRequest` with a `data_policy` dict and asserts the same fields come back. `::test_compatibility_profile_pins_exact_shared_bar_fixture` and the synthesize tests exercise real `data_policy` behavior. |
| 19 | `unit/lean_sidecar/test_data_policy.py::test_data_policy_canonical_import_path` | 1 | Only imports `BarsSpec, DataPolicy`. No assert. Every user of the module already imports them. |
| 20 | `unit/lean_sidecar/test_data_policy.py::test_data_policy_roundtrips_to_json_with_sorted_keys` | 1 | Runs `json.dumps(asdict(dp))` and reads fields back, which tests the standard library. The "sorted keys" in its name is never asserted. |
| 21 | `unit/lean_sidecar/test_bars_spec.py` (whole file, 3 tests) | 1 | Dataclass `==`/`!=` and `asdict` on a two-field frozen dataclass. The "no normalization" pin is default dataclass equality. Its docstring cites `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md`. |
| 22 | `unit/routers/test_engine_trade_ms_timestamp.py::test_to_ms_utc_rejects_naive_datetimes` | 3 | Stronger: `utils/test_timestamps.py::test_to_ms_utc_rejects_naive_datetime`, the identical assert (`match="timezone-aware"`) at the canonical module's own test. |
| 23 | `unit/routers/test_engine_trade_ms_timestamp.py::test_to_ms_utc_tz_aware_et_in_summer_uses_edt_offset` | 3 | Stronger: `utils/test_timestamps.py::test_ny_and_utc_agree_at_same_instant`, the same EDT conversion. The winter/EST test stays: nothing else proves EST. |
| 24 | `unit/routers/test_engine_trade_ms_timestamp.py::test_lean_trade_stats_response_preserves_nullable_int64_ms_utc_boundaries` | 1 | Constructs `LeanTradeStatsResponse` with two ints and asserts the same ints back. The "nullable" in its name is never exercised. |
| 25 | `unit/data_lake/test_factor_files.py::test_a_planned_dividend_without_its_reference_close_fails_loudly` | 3 | Stronger: `data_lake/test_factor_files_coverage.py::test_a_missing_reference_close_refuses_the_build`. Same `FactorFileReferenceError`, and the stronger test also asserts that the refusal names the session date. |
| 26 | `unit/data_lake/test_factor_files.py::test_actions_before_the_capture_are_not_planned` | 3 | Stronger: `data_lake/test_factor_files_coverage.py::test_only_actions_inside_a_span_are_kept_each_priced_on_its_prior_session`. It asserts the plan keeps only in-span actions. This one only counts output lines. |
| 27 | `unit/data_lake/test_ensure_data.py::test_metadata_bootstrap_launcher_unreachable_retries_and_completes_when_the_launcher_recovers` | 3 | Two tests already prove this together. "A failed metadata row is retried by the next `ensure_data` call" is `::test_metadata_bootstrap_retries_a_prior_failure_instead_of_jamming`, at the same seam. The unreachable-specific retry, with catalog `Status`/`AttemptCount`, is `unit/data_lake/test_metadata_bundle.py::test_launcher_unreachable_failure_is_retried_once_the_launcher_recovers`. |
| 28 | `unit/data_lake/test_lean_metadata.py::test_extract_lean_metadata_raises_launcher_unreachable_on_connect_error` | 3 | Stronger, one layer up: `unit/data_lake/test_metadata_bundle.py::test_launcher_unreachable_is_recorded_as_transient_and_names_the_launcher`. It drives a connection failure through the real extractor (respx `side_effect`, `launcher_route.call_count == 1`) and asserts the same `launcher_unreachable` reason and the same detail wording, plus the catalog row. |

**Checked and kept.**
- **Reconciliation and parity, all sacred:** every file under `integration/reconciliation/`, `integration/test_lean_engine_polygon_parity.py` and `integration/parity/test_ema_crossover_lean_vs_spec.py`. `test_spy_vwap_reversion_qc.py` and the LEAN-vs-spec gate are also listed under "Proven math, unused".
- **Fencing and lease tests:** the data-lake ones in `test_atomic.py`, `test_catalog_write_ops.py`, `test_catalog_root_scoping.py` and `test_reclaim_after_lost_claim.py`.
- **`unit/data_lake/test_catalog_truncation_guard.py`:** it guards the shared Postgres against truncation.
- **`unit/data_lake/test_no_lean_paths_outside_policy.py`:** a structural single-source guard.
- **`_helpers/test_parity_helpers.py`:** it proves the parity assertion helpers can fail, so a parity test cannot pass silently.
- **The "passes" tests with no assert** (in `test_bar_validation.py`, `test_main_root_identity_validation.py` and `test_corrupt_minute_bars_rejected.py`): each is paired with a raise test, or asserts inside a helper.
- **The flat-commission control tests** in `integration/test_paired_run_commission.py`. They are the negative half of the #2465 refusal.

## Already owned by the blocking dead-code tickets (not rows here)

- **#2705:**
  - whole files `unit/data_lake/test_cache_import.py`, `unit/data_lake/test_sweep.py` and `integration/data_lake/test_flag_flip_parity.py`;
  - in `unit/data_lake/test_catalog_write_ops.py`: `::test_mark_complete_artifact_failed_refuses_a_claimed_row`, `::test_refresh_lease_extends_expiry` and `::test_refresh_lease_rejects_wrong_owner`;
  - `unit/data_lake/test_path_policy.py::test_staging_path_isolation` and `::test_two_attempts_produce_distinct_paths`;
  - `unit/data_lake/test_root_identity.py::test_lake_container_matches_path_policy`;
  - the fake `mark_complete_artifact_failed` in `_helpers/fake_lake_catalog.py`.
- **#2704:**
  - `unit/lean_sidecar/test_data_policy.py::test_data_policy_manifest_alias_emits_deprecation_warning`, which goes with #2704 row F (the `DataPolicyManifest` alias; "one test does").
  - The trim of `_helpers/legacy_ibkr_artifacts.py` (below).
- **#2703:** the clerk-projection half of `integration/data_lake/test_schema_drift.py` (its rows 8 and 9).
- **#2706:** no rows in this area.

## Volatility math (owner ruling, 2026-09-30)

The owner ruled that validated volatility math stays even when nothing uses it. That covers the IV solver, surface fitting, VIX-style IV30 (legacy path included), realized volatility, IV basis conversion, put-call-parity forwards and Black-Scholes vega, with their tests and golden fixtures. **No test in this area touches that math**, so the ruling changes no row here.

## Proven math, unused (kept, not cut — awaiting the owner)

These are checked against a reference, and their app path has no live production caller. They stay off the kill list until the owner decides whether the volatility keep extends to them.

| Item | Reference | Why it counts as unused |
|---|---|---|
| `integration/reconciliation/test_spy_vwap_reversion_qc.py` (3 tests) with `fixtures/golden/spy-vwap-reversion-qc/` | Trade-by-trade reconciliation against QuantConnect. Decision parity, plus fill-price drift bounded by the documented $0.30 vendor floor. | #2704's conditional row: `SpyVwapReversionAlgorithm` is reached only by the reflective `resolve_strategy_class` behind `POST /api/lean-sidecar/runs/{run_id}/cross-reconcile`, and #2706 cuts that route. Moved here from "Already owned". |
| `integration/parity/test_ema_crossover_lean_vs_spec.py::test_ema_crossover_lean_matches_spec_on_real_spy_data` | Acceptance gate: LEAN `ema_crossover` ≡ spec `spy_ema_crossover`, with zero gating divergences in the reconciliation taxonomy. | Its engine side runs through `app.services.spec_strategy_runner.run_spec_against_bars_and_persist` (`:52`). #2704 found that function test-only, and no dead-code list rules on it (H4). Under ☆ "test-only helpers follow their tests", it stays while this test stays. |

## For CI shape (#2738)

These are kept math or lake tests that are slow or need infrastructure. Speed is not a reason to cut them.

- **`integration/parity/test_ema_crossover_lean_vs_spec.py`.** It is `@pytest.mark.slow` and needs Postgres, a reachable LEAN launcher, LEAN SPY minute zips and `PINNED_LEAN_IMAGE_DIGEST`. Each missing piece makes it `pytest.skip` (`:89-117`). It likely skips in every CI run today.
- **`integration/test_lean_engine_polygon_parity.py`.** It is gated by `skipif` on an environment variable and runs the LEAN container against the pinned Polygon fixture.
- **Daily-only, Postgres-gated data-lake integration tests.** These skip silently without `POSTGRES_URL`:
  - `test_ensure_data_all_kinds.py`, `test_both_price_adjustment_modes.py`, `test_gate_chain_convergence.py`, `test_daily_rollup_claim_miss_postgres.py`, `test_catalog_schema_not_ready.py` and `test_schema_drift.py`;
  - the Postgres halves of `unit/data_lake/test_catalog_write_ops.py`, `test_catalog_observatory_reads.py` and `test_reclaim_after_lost_claim.py`.

## What the cuts orphan

- **`unit/lean_sidecar/`.** Rows 19–21 and #2704's alias test empty the package. Delete `unit/lean_sidecar/__init__.py` with them.
- **The `make_data_lake_app` fixture in `integration/data_lake/conftest.py`.** Once rows 13–14 go, its `include_data_lake=False` branch has no caller. Every remaining caller passes `True`. Dropping the parameter means editing about 40 call sites, so leave it unless the cutting PR is already in that file.
- **`test_ensure_data_real_polygon.py`'s module-local helpers** go with the file (`_launcher_side_effect`, `_mock_corp_actions_and_events`, `_polygon_payload_for`). Nothing imports them.
- **`_helpers/legacy_ibkr_artifacts.py`: trim, don't delete** (the #2704 ruling).
  - The surviving cutover tests use only `write_historical_account_binding`: `broker/alpaca/clerk/sqlite/cutover_test_support.py`, `test_catalog_quarantine.py` and `test_dev_reset.py`. It needs `account_artifacts_root`, `binding_command_ledger_path`, `read_account_binding_commands`, `AccountBindingCommand`, `ACCOUNT_INSTANCE_REGISTRY_FILENAME`, `AccountInstanceBinding` and `_file_lock`.
  - Four writers have callers only in tests #2704 cuts: `write_historical_owner_generation`, `write_historical_clerk_generation`, `write_historical_clerk_lease` and `write_historical_clerk_journal`. Their callers are `engine/live/test_producer_operational_log.py` (whole file cut) and `engine/live/test_account_artifacts.py::test_account_clerk_generation_and_lease_are_account_rooted` / `::test_control_artifact_account_identity_mismatches_fail_closed`, which are inside #2704's partial cut. Those four go with their imports of `account_clerk_journal` and `AccountClerkJournalEntry`, plus the generation and lease names. `_write_jsonl` and `_write_model` go too, unless a surviving caller remains.
  - Re-check which `test_account_artifacts.py` tests survive at the cutting SHA (#2727 owns that file).
- **Docs:** row 21 removes a citation of `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md` (#2711's sediment list).

## Hazards the cutting PR must carry

- **H1. Postgres-gated tests skip silently.** `test_ensure_data_all_kinds.py`, the survivor of row 17, skips without `POSTGRES_URL` (`:53`). Run it against an ephemeral database (`POSTGRES_URL_IS_EPHEMERAL=1`) before calling row 17 clean. Do not treat a green PR run as proof. Rows 4–15 need no Postgres: their survivors are route tests that run on the PR path.
- **H2. Cross-ticket duplicate pairs.**
  - The survivors of rows 22, 23, 25 and 26 live in other tickets' areas: `tests/utils/` and `tests/data_lake/`, both #2728.
  - The cutting PRs must not delete both halves of a pair. Before cutting a row here, confirm #2728's list does not cut its named survivor. Merge whichever list lands second against the first.
- **H3. #2705's open question on `test_flag_flip_parity.py`, answered for the tests in this area.**
  - *Raw chart, covered window served from the lake:* still proven on a natively written lake by `services/test_chart_split_read.py::test_compose_chart_bars_stitches_lake_history_to_the_live_tail` (the provider sees only the live session).
  - *Engine, zero provider calls:* still proven by `unit/data_lake/test_run_materialization.py::test_materialize_run_data_reuses_the_bytes_on_a_second_run` (`polygon.call_count == 1` across two runs) and by `integration/data_lake/test_ensure_data_all_kinds.py::test_ensure_data_second_call_is_cache_hit`.
  - *Engine and sidecar reading the adjusted root:* still proven by `integration/data_lake/test_both_price_adjustment_modes.py`.
  - **Not proven anywhere else:** a *chart* request for `polygon_split_adjusted` served from a populated adjusted root. `test_compose_chart_bars_reads_the_adjusted_root_for_an_adjusted_request` proves only the empty-root miss. `test_flag_flip_parity.py::test_chart_serves_an_adjusted_request_from_its_own_imported_root` is the only positive proof. Deleting the file loses it (see "Needs the map's attention").
- **H4. `integration/parity/test_ema_crossover_lean_vs_spec.py` hangs on an unruled helper.** It imports `app.services.spec_strategy_runner.run_spec_against_bars_and_persist` (`:52`). #2704 pointed that function to #2703 as test-only, and #2703's list does not mention it. Under ☆ "test-only helpers follow their tests", it stays while this sacred parity gate stays. A later dead-code pass must not cut the runner without this test.
- **H5. Kill lists age.** This list was read at `6a4d7d39`. Re-check each row's named survivor at the cutting PR's own SHA.

## Pointers — cuttable things outside this area

- `tests/data_lake/test_factor_files_coverage.py` and `tests/utils/test_timestamps.py` are the survivors of rows 22–26. They belong to **#2728**, which should keep them.
- The data-lake router comment in `app/main.py:1418-1420` and the `make_data_lake_app` docstring (`integration/data_lake/conftest.py:24-26`, "flag-off 404 behavior … DATA_LAKE_ENABLED") describe a retired flag → **#2743** (comment citations, tests).

## Needs the map's attention

- **Adjusted-root chart proof (H3).** When #2705's `test_flag_flip_parity.py` cut lands, nothing proves that an adjusted chart request is served from a populated adjusted lake root. This is not money path. Options for the #2705 handoff:
  1. Accept the loss: the adjusted-root read path is shared with the engine, which `test_both_price_adjustment_modes.py` proves (recommended).
  2. Write one replacement test on a natively written lake in the same PR. Writing a new test is not "rewriting a surviving test", so this fits the rules.

## Not reviewed

- **Duplicate pass on the largest data-lake unit files**, which got only the flag pass (no-assert, trivial, wording, mock-call or source reads): `unit/data_lake/test_run_materialization.py` (55 tests), `test_metadata_bundle.py` (43, except the launcher-unreachable cluster in rows 27–28), `test_catalog_write_ops.py` (35), `test_backfill.py` (30), `test_root_identity.py` (32), `test_path_policy.py` (28) and `test_catalog_root_scoping.py` (20). Their tests were not compared one by one against `tests/data_lake/` or `tests/services/`. Duplicates there are likely but unconfirmed.
- **`integration/data_lake/test_observatory_endpoints.py` beyond rows 14–16** was not compared against `tests/routers/`, which #2730 owns.
- **`_helpers/` beyond importer counts and the `legacy_ibkr_artifacts` trim.** Unused functions *inside* used helper modules were not searched for. `fake_lake_catalog.py` (661 lines) and `bot_runner/doubles.py` (629 lines) are the likeliest places.
- **`integration/test_engine_persistence_quantity_pnl.py`** and the rest of `integration/test_engine_persistence_data_policy.py` were read at the assert level only, not compared against `tests/services/test_engine_backtest_service*` (#2725).
