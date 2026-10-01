# Kill list — tests inside `app/` and the golden-fixture suite's test files (#2737)

Part of map #2700. Plan, don't cut: nothing here has been deleted.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30). The map was charted at `87b8e261`; the blocking lists (#2704, #2707, #2716) were read at that SHA.
- **Area:** `PythonDataService/app/engine/tests/` (19 files), `PythonDataService/app/engine/strategy/spec/tests/` (10 files plus `_parity_helpers.py`), the 14 test files under `PythonDataService/tests/fixtures/` (and their `conftest.py` and `golden_support/` helpers, judged only as orphans), and `scripts/dev/fleet/test_fleet_tooling.py`.
- **Rules applied:** the map's five kinds — (1) trivial, (2) copy or doc pinning, (3) duplicate, (4) mock theater, (5) retired or dead. Golden-fixture parity tests and the manifest's real checks are sacred. Sacred is the behavior, not the file. Test-only helpers follow their tests (☆). **Owner ruling 2026-09-30 (relayed by the coordinator): validated volatility math stays even when nothing uses it** — see "Kept by the volatility ruling".

## How this was checked

1. Every test in the area was read. Each non-parity test was asked one question: does it prove something about *our* code that no other surviving test proves?
2. Collection was checked with `pytest --collect-only` on the host venv (160 tests in the two `app/` directories; five `test_*.py` files collect zero). The suites were run green: `app/engine/tests` + `spec/tests` (159 pass, `-m "not slow"`), `tests/fixtures` (167 pass in 3.4 s).
3. Fixture bytes: `tests/fixtures/golden/manifest.json` was read for each fixture's `content_sha256`, `file_sha256`, tolerance and `validated_by`. A test that only re-reads a committed fixture file proves nothing the manifest hash checks do not already prove.
4. Duplicates were checked one layer down (`tests/test_statistics.py`, `tests/engine/*`, `tests/services/test_bs_greeks.py`) and across fixtures (RP-001 and REL-001 share an input file).
5. Hash trees were checked (see hazard H1).

Two patterns cover most rows:

- **Oracle-only checks (kind 1).** The test reads only the committed reference output (or input) and never calls our code. For example, it checks "the oracle's delta is in (0, 1)". The bytes are already pinned by `tests/fixtures/test_golden_manifest.py::test_content_hashes_match_disk` and `::test_file_hashes_match_disk`, so the check is about the fixture, not about us.
- **Row and case counts (kind 3).** `assert len(inp) == N` on a committed `input.arrow`. The same manifest hash pins the row count exactly, and the parity test next to it loops over every row.

## Which CI job runs each file

| Files | PR-path runner | Daily |
|---|---|---|
| `app/engine/tests/**`, `app/engine/strategy/spec/tests/**` | `Python Test Shard` ×16 — the fast baseline `FAST_TEST_PATHS` (`PythonDataService/scripts/run_fast_tests.py:42,53`) | `Python Full Suite` (`daily-tests.yml:104`) |
| `tests/fixtures/**` except the manifest test | **Only** the scientific-proof step inside every `Python Test Shard` (`ci.yml:369-372`, `:485-488`); `tests/fixtures` is not in `FAST_TEST_PATHS` | `Python Full Suite` |
| `tests/fixtures/test_golden_manifest.py` | **Only** `Validate Golden Manifest` (`ci.yml:532-546`, `--noconftest`); the scientific-proof step `--ignore`s it (`:488`) | `Python Full Suite` |
| `app/engine/tests/test_spy_validation.py::test_spy_ema_crossover_matches_lean_reference_trades` | none — `@pytest.mark.slow` (`:173`), excluded by `-m "not slow"` | collected, but it skips when `LEAN_DATA_ROOT` has no SPY minute data (`:189-197`), which is the case on a GitHub runner |
| `scripts/dev/fleet/test_fleet_tooling.py` | **Only** `Fleet Tooling Tests` (`ci.yml:42-56`) | — (cut with its kit by #2707 row 19) |

## Kill list

### A. Golden-fixture suite (`PythonDataService/tests/fixtures/`)

Every `*_matches_oracle` / `*_matches_*` parity test in these files stays. Only the checks beside them are listed.

| # | Item | Kind | Evidence |
|---|---|---|---|
| A1 | `test_golden_manifest.py::test_manifest_file_exists` | 3 | `::test_manifest_conforms_to_json_schema` reads and parses the same file, so it fails first if the file is missing. |
| A2 | `test_golden_manifest.py::test_schema_file_exists` | 3 | Same: `::test_manifest_conforms_to_json_schema` reads `SCHEMA_PATH`. |
| A3 | `test_golden_manifest.py::test_manifest_is_valid_json` | 3 | Same: that test runs `json.loads` and validates against a schema whose root is an object. |
| A4 | `test_golden_manifest.py::test_manifest_pydantic_roundtrip` | 1 | It round-trips the test-only `golden_support.manifest.Manifest` model. The parse half runs inside every Registry-based test (`::test_active_fixture_files_exist`). Nothing writes the manifest through the model: `Manifest.save` has no caller. |
| A5 | `test_golden_manifest.py::test_active_version_in_versions` | 3 | The same check is the `Fixture._active_version_exists` model validator (`golden_support/manifest.py:97-104`), with the same `planned` skip. It runs on every Registry load, for example in `::test_active_fixture_files_exist`. |
| A6 | `test_golden_manifest.py::test_hash_fields_are_valid_hex` | 3 | The same 64-hex check is `FixtureFiles._hashes_are_hex` (`manifest.py:73-79`), which runs on Registry load for every version. `::test_file_hashes_match_disk` and `::test_content_hashes_match_disk` also compare each value to a freshly computed digest. |
| A7 | `test_golden_manifest.py::test_schema_version_is_1` | 1 | It pins a constant that nothing branches on. Its own message says to bump it on purpose. A manifest with a different shape fails `::test_manifest_conforms_to_json_schema`. |
| A8 | `test_indicator_fixtures.py::TestIND001EMA::test_row_count` | 3 | Row-count pattern (IND-001). |
| A9 | `test_indicator_fixtures.py::TestIND002SMA::test_row_count` | 3 | Row-count pattern (IND-002). |
| A10 | `test_indicator_fixtures.py::TestIND003RSI::test_row_count` | 3 | Row-count pattern (IND-003). |
| A11 | `test_indicator_fixtures.py::TestIND001EMA::test_ema_always_produces_value` | 3 | `::test_ema_matches_oracle_all_cases` requires a non-None value wherever the oracle has one (`_assert_sequence`, `:96-106`), and the oracle has one at every bar. |
| A12 | `test_indicator_fixtures.py::TestIND002SMA::test_sma_always_produces_value` | 3 | Same as A11, but proven by `::test_sma_matches_oracle_all_cases`. |
| A13 | `test_indicator_fixtures.py::TestIND003RSI::test_rsi_nan_before_ready` | 1 | Oracle-only: it reads `_oracle_vals(out, row)` and never runs RSI. Our warmup region is proven by `::test_rsi_matches_oracle_all_cases` (None where the oracle is None) and `::test_rsi_ready_at_period_plus_one`. |
| A14 | `test_indicator_fixtures.py::TestIND003RSI::test_rsi_bounded_zero_to_100` | 3 | On the same fixture rows, `::test_rsi_matches_oracle_all_cases` pins every value to 1e-9 of an oracle value inside [0, 100]. |
| A15 | `test_engine_stats_fixtures.py::TestENG001Sharpe::test_case_count` | 3 | Row-count pattern (ENG-001). |
| A16 | `test_engine_stats_fixtures.py::TestENG001Sharpe::test_sharpe_is_positive_for_positive_mean_cases` | 1 | Oracle-only: it asserts `oracle_sharpe > 0`. |
| A17 | `test_engine_stats_fixtures.py::TestENG001Sharpe::test_sharpe_none_for_zero_std` | 3 | `tests/test_statistics.py::TestSharpe::test_constant_returns` asserts the same zero-std → `None`. |
| A18 | `test_engine_stats_fixtures.py::TestENG001Sharpe::test_sharpe_none_for_single_return` | 3 | `tests/test_statistics.py::TestSharpe::test_insufficient_data` makes the identical call `_sharpe([0.01], 252)`. |
| A19 | `test_engine_stats_fixtures.py::TestENG001bSortino::test_case_count` | 3 | Row-count pattern (ENG-001b). |
| A20 | `test_engine_stats_fixtures.py::TestENG001bSortino::test_sortino_none_when_no_downside` | 3 | `tests/test_statistics.py::TestSortino::test_no_downside` asserts the same no-downside → `None`. |
| A21 | `test_engine_stats_fixtures.py::TestENG001bSortino::test_sortino_none_for_single_return` | 3 | `tests/test_statistics.py::TestSortino::test_insufficient_data` asserts the same single return → `None`. |
| A22 | `test_engine_stats_fixtures.py::TestENG001bSortino::test_sortino_greater_than_sharpe_for_low_downside` | 1 | Oracle-only: it compares two committed oracle values. |
| A23 | `test_engine_stats_extended_fixtures.py::TestENG002MaxDrawdown::test_row_count` | 3 | Row-count pattern (ENG-002). |
| A24 | `test_engine_stats_extended_fixtures.py::TestENG003TradeStats::test_row_count` | 3 | Row-count pattern (ENG-003). |
| A25 | `test_engine_stats_extended_fixtures.py::TestENG004CAGR::test_row_count` | 3 | Row-count pattern (ENG-004). |
| A26 | `test_engine_stats_extended_fixtures.py::TestENG005Calmar::test_row_count` | 3 | Row-count pattern (ENG-005). |
| A27 | `test_engine_stats_extended_fixtures.py::TestENG002MaxDrawdown::test_mdd_bounded_zero_one` | 3 | On the same rows, `::test_mdd_matches_oracle` pins every value with `assert_allclose(atol=1e-9, rtol=0)`. |
| A28 | `test_engine_stats_extended_fixtures.py::TestENG002MaxDrawdown::test_empty_curve_returns_zero` | 3 | `tests/test_statistics.py::TestMaxDrawdown::test_empty_curve` has the identical assertion. |
| A29 | `test_engine_stats_extended_fixtures.py::TestENG002MaxDrawdown::test_monotone_increasing_zero_mdd` | 3 | `tests/test_statistics.py::TestMaxDrawdown::test_monotonic_up` uses the same input `[100, 110, 120, 130]`. |
| A30 | `test_engine_stats_extended_fixtures.py::TestENG003TradeStats::test_empty_trades_returns_zeros` | 3 | `tests/test_statistics.py::TestTradeStatistics::test_empty` makes the same three assertions. |
| A31 | `test_engine_stats_extended_fixtures.py::TestENG004CAGR::test_cagr_sign_matches_performance` | 1 | Oracle-only: it checks the sign of `oracle_cagr` against the input. |
| A32 | `test_engine_stats_extended_fixtures.py::TestENG005Calmar::test_calmar_equals_cagr_over_mdd` | 1 | It re-derives the oracle's formula by hand and compares that to the oracle. Our Calmar is proven by `::test_calmar_matches_oracle`. |
| A33 | `test_options_pricing_fixtures.py::TestBS001CallPrice::test_case_count` | 3 | Row-count pattern (BS-001). |
| A34 | `test_options_pricing_fixtures.py::TestBS002PutPrice::test_case_count` | 3 | Row-count pattern (BS-002). |
| A35 | `test_options_pricing_fixtures.py::TestBS003CallDelta::test_case_count` | 3 | Row-count pattern (BS-003). |
| A36 | `test_options_pricing_fixtures.py::TestBS002PutPrice::test_put_call_parity` | 1 | Oracle-only, despite its name: it checks the lower bound `p >= K·e^(−rT) − S` on committed oracle prices. Our own put–call parity is proven by `tests/services/test_bs_greeks.py`, which has two put-call-parity tests (`:177`, `:206`). |
| A37 | `test_options_pricing_fixtures.py::TestBS003CallDelta::test_call_delta_in_range` | 1 | Oracle-only: it asserts `0 < oracle_delta < 1`. |
| A38 | `test_options_pricing_fixtures.py::TestBS003CallDelta::test_itm_delta_greater_than_otm` | 1 | Oracle-only: it compares oracle deltas between rows. |
| A39 | `test_options_pricing_fixtures_extended.py::TestBS004Gamma::test_row_count` | 3 | Row-count pattern (BS-004). |
| A40 | `test_options_pricing_fixtures_extended.py::TestBS005Theta::test_row_count` | 3 | Row-count pattern (BS-005). |
| A41 | `test_options_pricing_fixtures_extended.py::TestBS006Vega::test_row_count` | 3 | Row-count pattern (BS-006). |
| A42 | `test_options_pricing_fixtures_extended.py::TestBS007Rho::test_row_count` | 3 | Row-count pattern (BS-007). |
| A43 | `test_options_pricing_fixtures_extended.py::TestBS004Gamma::test_gamma_positive` | 3 | It checks the sign of the same 180 values that `::test_gamma_matches_oracle` pins at `atol=1e-10, rtol=0`. |
| A44 | `test_options_pricing_fixtures_extended.py::TestBS004Gamma::test_gamma_higher_near_atm` | 3 | It orders averages of the values `::test_gamma_matches_oracle` already pins. |
| A45 | `test_options_pricing_fixtures_extended.py::TestBS005Theta::test_theta_negative_for_calls` | 3 | Sign of values pinned by `::test_theta_matches_oracle`. |
| A46 | `test_options_pricing_fixtures_extended.py::TestBS005Theta::test_theta_larger_magnitude_near_atm` | 3 | Ordering of values pinned by `::test_theta_matches_oracle`. |
| A47 | `test_options_pricing_fixtures_extended.py::TestBS006Vega::test_vega_positive` | 3 | Sign of values pinned by `::test_vega_matches_oracle` (see "Needs the map's attention", item 1). |
| A48 | `test_options_pricing_fixtures_extended.py::TestBS006Vega::test_vega_higher_longer_ttm` | 3 | Ordering of values pinned by `::test_vega_matches_oracle` (same caveat as A47). |
| A49 | `test_options_pricing_fixtures_extended.py::TestBS007Rho::test_call_rho_positive` | 3 | Sign of values pinned by `::test_rho_matches_oracle`. |
| A50 | `test_options_pricing_fixtures_extended.py::TestBS007Rho::test_rho_higher_itm` | 3 | Ordering of values pinned by `::test_rho_matches_oracle`. |
| A51 | `test_research_fixtures.py::TestRP001InformationCoefficient::test_row_count` | 3 | Row-count pattern (RP-001). |
| A52 | `test_research_fixtures.py::TestRP001InformationCoefficient::test_n_days` | 1 | Oracle-only: it asserts `oracle_n_days == 4`. Our daily-IC count is proven by the `strict=True` zip in `::test_daily_ics_match_oracle`. |
| A53 | `test_research_fixtures.py::TestRP002QuantileMonotonicity::test_row_count` | 3 | Row-count pattern (RP-002). |
| A54 | `test_research_fixtures.py::TestRP002QuantileMonotonicity::test_monotonic_with_positive_signal` | 1 | Oracle-only: it reads `oracle_is_monotonic`. Our value is proven by `::test_is_monotonic_matches_oracle`. |
| A55 | `test_research_fixtures.py::TestRP003PhipsonSmyth::test_row_count` | 3 | Row-count pattern (RP-003). |
| A56 | `test_research_fixtures.py::TestRP003PhipsonSmyth::test_p_value_positive` | 1 | Oracle-only: it reads `oracle_p_value`. |
| A57 | `test_research_fixtures.py::TestRP003PhipsonSmyth::test_p_value_bounded` | 1 | Oracle-only: it reads `oracle_p_value`. |
| A58 | `test_research_fixtures.py::TestRP004SignalZscore::test_row_count` | 3 | Row-count pattern (RP-004). |
| A59 | `test_research_fixtures.py::TestRP004SignalZscore::test_flip_negates_zscore` | 1 | Oracle-only: it compares `oracle_z` with `oracle_z_flipped`. Our flip is proven by `::test_zscore_flipped_matches_oracle`. |
| A60 | `test_research_fixtures.py::TestREL001ICHitRate::test_row_count` | 3 | Row-count pattern (REL-001). |
| A61 | `test_research_fixtures.py::TestREL001ICHitRate::test_mean_ic_matches` | 3 | REL-001 and RP-001 have the same input bytes: `content_sha256` is `70aa7cd0…` for both in `manifest.json`. This test makes the same `compute_information_coefficient` call as `TestRP001InformationCoefficient::test_mean_ic_matches_oracle`. |
| A62 | `test_research_fixtures.py::TestREL001ICHitRate::test_hit_rate_bounded` | 1 | Oracle-only: it reads `oracle_hit_rate`. |
| A63 | `test_research_fixtures.py::TestREL004ICDecayCurve::test_row_count` | 3 | Row-count pattern (REL-004). |
| A64 | `test_research_fixtures.py::TestREL004ICDecayCurve::test_ic_values_finite` | 1 | Oracle-only: it reads `oracle_horizon_{h}_ic`. |
| A65 | `test_return_distribution_golden.py::test_rd001_bin_geometry_pinned` | 1 | It reads only the committed `output.arrow`, whose bytes the RD-001 manifest hash pins. `::test_rd001_production_pipeline_matches_oracle_and_committed_output` compares every bin count and stat. |
| A66 | `test_return_distribution_golden.py::test_rd001_input_spans_real_sessions_with_extended_hours` | 1 | It reads only the committed `input.arrow` (hash-pinned) and proves what the fixture covers, not what the code does. |
| A67 | `test_strategy_parity_fixtures.py::test_fixture_bar_series_is_deterministic_and_shared_across_strategies` | 1 | It reads only the committed `output.json` and checks its `trade_counts` against its own `trades` lists. Fresh-versus-pinned counts are asserted by `::test_strategy_trade_log_matches_pinned_pre_port_receipt`. |

### B. `PythonDataService/app/engine/tests/`

| # | Item | Kind | Evidence |
|---|---|---|---|
| B1 | `test_daily_sma_crossover_end_to_end.py` (whole) | 5 | Dead: it collects 0 tests, and its only function, `run_end_to_end()`, runs from `__main__`, which nothing invokes. It reads a hardcoded `/sessions/ecstatic-hopeful-volta/mnt/Lean/Data` (`:55`), a sandbox that no longer exists (`docs/audits/structural-integrity-2026-04-22.md:287`). `LeanDailyDataReader` is exercised by surviving tests, for example `tests/engine/test_adjustment_versions.py::test_split_between_captures_does_not_create_a_false_price_gap`. |
| B2 | `test_lean_daily_reader_parity.py` (whole) | 5 | Dead in the same way: 0 collected, `run_parity_test()` only from `__main__`, and the same dead sandbox path (`:45`). |
| B3 | `test_sma_crossover_parity.py` (whole) | 5 | Dead: 0 collected, `run_parity_test()` only from `__main__`. The SMA parity that does run is `app/engine/strategy/spec/tests/test_spec_sma_parity.py::test_sma_spec_matches_hand_coded`. |
| B4 | `test_rsi_mean_reversion_parity.py` (whole) | 5 | Dead: 0 collected, `run_parity_test()` only from `__main__`. The RSI proofs that do run are ENG-009 (`tests/integration/reconciliation/test_rsi_mean_reversion_lean_golden.py`) and `spec/tests/test_spec_rsi_mean_reversion_parity.py::test_rsi_mean_reversion_spec_matches_hand_coded`. |
| B5 | `test_spy_next_bar_open_validation.py` (whole) | 5 | Dead: 0 collected, `run_validation()` only from `__main__`, and the dead sandbox path (`:47`). NEXT_BAR_OPEN fills are proven by `tests/engine/test_engine_fill_modes.py::test_next_bar_open_keeps_existing_defer_behavior_on_same_stream`. |
| B6 | `test_spy_validation.py::run_validation` and its `__main__` block (`:206-255`) | 5 | A manual runner built on `print()`. Nothing runs `python -m app.engine.tests.test_spy_validation`. The pytest test in the file stays (sacred LEAN parity). |

### C. `PythonDataService/app/engine/strategy/spec/tests/`

| # | Item | Kind | Evidence |
|---|---|---|---|
| C1 | `test_spec_round_trip.py::test_json_schema_exports` | 1 | It asserts that Pydantic emits `$defs`, `properties` and `required`, plus three class names that exist by import. Its only consumer, `GET /api/spec-strategy/schema`, is cut by #2706. |
| C2 | `test_spec_round_trip.py::test_canonical_specs_load` | 3 | `::test_canonical_specs_round_trip` loads the same three specs, and the three spec parity tests run them. Its extra assertions only pin fixture field values. |
| C3 | `test_spec_predictions.py::test_prediction_ref_round_trip` | 1 | It validates a dict and reads the same fields back. |
| C4 | `test_spec_predictions.py::test_prediction_comparison_round_trip` | 1 | Same as C3. |
| C5 | `test_spec_predictions.py::test_prediction_ref_default_field_is_prediction` | 1 | It pins a Pydantic field default. |
| C6 | `test_spec_predictions.py::test_spec_with_no_predictions_still_loads` | 1 | It pins the `predictions == []` default. Every canonical spec has no predictions and loads in C2's survivor and the parity tests. |
| C7 | `test_spec_predictions_runtime.py::test_spec_algorithm_accepts_optional_prediction_set` | 1 | It asserts the private `_prediction_set is None`. Every spec parity test builds `SpecAlgorithm(spec)` with no set. |
| C8 | `test_spec_predictions_runtime.py::test_spec_algorithm_accepts_explicit_prediction_set` | 1 | It asserts the constructor stores its argument (`_prediction_set is pset`). |
| C9 | `test_spec_extra_primitives.py::test_eval_context_predictions_default_empty` | 1 | It pins a dataclass default. |
| C10 | `test_spec_extra_primitives.py::test_eval_context_predictions_can_be_supplied` | 1 | It asserts a dataclass stores a field. `::test_prediction_comparison_fires_when_above_threshold` builds the same context and uses it. |
| C11 | `test_spec_spy_ema_2_bps_parity.py::test_two_bps_spec_changes_only_identity_copy_and_gap_operand` | 2 | It compares two committed JSON fixtures after overwriting four fields, so it pins fixture text. A changed period or operator in the 2 bps spec would fail `::test_two_bps_spec_matches_hand_coded_strategy_trade_by_trade`. |
| C12 | `test_spec_manage_rules.py::run_all` and `__main__` (`:279-303`) | 5 | Script-mode runner that nothing invokes. pytest runs the tests directly. |
| C13 | `test_spec_extra_primitives.py::run_all` (`:293-311`) and `__main__` (`:343-344`) | 5 | The same, built on `print()`. |
| C14 | `test_spec_router.py::run_all` and `__main__` (`:244-270`) | 5 | The same, built on `print()`. |
| C15 | `test_spec_round_trip.py::run_all` and `__main__` (`:145-171`) | 5 | The same. |
| C16 | `test_spec_sma_parity.py::run_parity` and `__main__` (`:80-109`) | 5 | The same. `_run_parity` stays: the pytest test calls it. |
| C17 | `test_spec_rsi_mean_reversion_parity.py::run_parity` and `__main__` (`:65-96`) | 5 | The same; `_run_parity` stays. |
| C18 | `test_spec_spy_ema_parity.py::run_parity` and `__main__` (`:66-97`) | 5 | The same; `_run_parity` stays. |

### Skipped — already on a blocking or sibling list

- `app/engine/tests/test_limit_orders.py` (whole) → **#2704** (limit-order branch).
- `app/engine/tests/test_strategies_abc.py::test_rollback_blocked_entry_resets_lifecycle_and_re_arms` → **#2704** (`rollback_blocked_*`).
- `app/engine/tests/test_pine_generators.py` → **#2706**. It lists 4 of the 7 tests (the route tests). The other 3 test `app/engine/pine_generators.py`, which #2706 also cuts, so **the whole file goes** in that PR.
- `app/engine/strategy/spec/tests/test_spec_router.py::test_schema_endpoint` → **#2706**.
- `scripts/dev/fleet/test_fleet_tooling.py` → **#2707** row 19 (the kit), with its only runner, the `Fleet Tooling Tests` job (#2707 row 20, #2716 row 5).

## Kept by the volatility ruling (owner, 2026-09-30)

Validated volatility math stays even when nothing uses it. That covers the IV solver, surface fitting, VIX-style IV30 including the legacy `vix_style_iv30`, realized volatility, IV basis conversion, put-call-parity forwards, Black-Scholes vega and `iv30_health`. **KEEP:**

- `tests/fixtures/test_volatility_fixtures.py` — the **whole file**, including `TestIV003IV30`. Before the ruling, #2704 would have cut that class with the legacy path.
- Golden fixtures IV-001, IV-002, IV-003, IV-004 and RV-001 to RV-004, their attribution files and their `manifest.json` entries.
- Their generators: `scripts/fixture_generators/volatility.py` and `scripts/build_iv30_golden.py`.
- `tests/fixtures/test_ibkr_iv_fixtures.py` (OPT-IB-002, the IV solver against IBKR prices) — whole file. I read it as IV-solver math under the same ruling.

Both files contain the same patterns this list cuts elsewhere. In `test_volatility_fixtures.py`: eight `test_row_count` tests, the oracle-only checks (`test_oracle_formula_positive_for_all_rows`, `test_svi_smile_shape_left_skew`, `test_iv30_positive_all_cases`, `test_half_before_min_periods`, `test_rank_bounded_zero_to_one`, `test_nan_before_window`, `test_rv_positive_after_warmup`, `test_nan_before_window_days`, `test_conversion_produces_positive_finite`, `test_sigma_sq_positive_all_cases`), `TestIV001SolverRoundTrip::test_iv_positive_and_finite`, `TestRV003BasisConversion::test_pinned_trading_days_matches_calendar` (a second `mcal.get_calendar("NYSE")`, banned by `temporal-rigor.md`), and the unused helper `_svi_total_var` (`:166`). In `test_ibkr_iv_fixtures.py`: `test_row_count_nonzero`, `test_output_row_count_matches_input`, `test_ibkr_iv_range_plausible`, `test_ttm_positive` and `test_mid_positive`. They are **not listed**; see "Needs the map's attention", item 1.

## Proven math, unused

Golden fixtures in this area whose math nothing live uses, listed instead of cut while the owner decides whether the volatility keep extends to them:

- **None besides IV-003**, which the ruling already keeps. Every other `canonical_module` the area's tests validate is reached in production. `app/engine/indicators/{adx,macd,supertrend}.py` (the in-app golden fixtures `app/engine/tests/fixtures/golden/*`) are reached by Signal Programs A/B/C. `bs_greeks`, `statistics`, the `research/validation` primitives, `return_distribution`, `regulatory_fees`, `day_pnl` and the A/B/C algorithms (ENG-008) are reached too. The SPY VWAP-reversion QC fixture and the options pricer are outside this area (#2704's conditional rows; tests under `tests/integration` and `tests/engine`).
- **Related, but not "unused math":** B3, B4 and B5 look like parity proofs of **live** math, but pytest has never collected them. They are cut as dead scripts, not as unused math. The live math they claim to cover has running proofs, named in each row.

## What the cuts orphan

- **`app/engine/tests/fixtures/spy_engine_next_bar_open_baseline.csv`.** Its only reader is B5. It is a frozen copy of our own engine's output, not a reference fixture: no manifest entry, no attribution file. `spy_engine_next_bar_open_snapshot.json` in the same folder has **no reader at all**. Both go with B5.
- **`app/engine/strategy/spec/tests/_parity_helpers.py`: `logger` and `configure_script_logger` (`:53-75`).** Only the script-mode runners C12–C18 call them. They go with those rows; the rest of the helper stays.
- **`app/engine/tests/test_strategies_abc.py`: `_wire_live` (`:56`) and `import random` (`:16`).** Only #2704's rollback test uses them, so they go with that row.
- **`tests/fixtures/test_golden_manifest.py:28` `GOLDEN_SUPPORT`.** A constant the file never reads. Drop it with A1–A7.
- **Stale citations of the dead files B3/B4/B5** (fix them, or let them go stale; see hazard H1):
  - `app/engine/strategy/registry.py:689,854` (provenance strings, not hashed);
  - `app/engine/strategy/spec/tests/_parity_helpers.py:19` (docstring);
  - `app/engine/strategy/algorithms/rsi_mean_reversion.py:6,19` and `sma_crossover.py:26` (**hashed**, H1);
  - `docs/math-sources-of-truth.md:218` (#2713 / #2714);
  - `Frontend/src/app/components/lean-engine/lean-engine-docs/lean-engine-docs.component.ts:432,491` and `.html:46` (#2709);
  - `PythonDataService/_validation_report.html:394` and `generate_validation_report.py:417` (#2707 / #2711).
- **Nothing else.** `tests/fixtures/conftest.py` stays. Its `pytest_sessionfinish` writes `artifacts/fixture-validation/latest.json`, which the live `/api/golden-fixtures` route reads (`app/routers/golden_fixtures.py:48`). All `golden_support/` modules stay: surviving tests and the fixture generators import them. `PythonDataService/scripts/pr_shard_durations.json` keeps stale names harmlessly; regenerate it after the cuts (#2707's note).

## Hazards the cutting PR must carry

- **H1 — Hash trees: deleting these tests changes no digest. Editing two citing docstrings would.**
  - `DECLARED_PROGRAM_SOURCE_PATHS` is an explicit file list (`app/engine/strategy/program_sources.py:21-246`) with no test file in it.
  - The sweep `source_digest` walks `app/engine` but skips every `tests` directory (`app/research/sweep/identity.py:81-82,118-125`, digest scheme 2). Scheme-1 receipts, which did hash tests, are already non-comparable (`app/research/persistence/lifecycle.py:138-139`).
  - **But** `rsi_mean_reversion.py:6,19` and `sma_crossover.py:26` are in the hashed set and name B3/B4. Leave those docstrings stale, or fold the edit into the single batched strategy re-qualification PR the map already plans. Never edit them in a test-cut PR.
- **H2 — `validated_by` names test classes.** `manifest.json` names classes in `test_research_fixtures.py` (`TestRP001…` to `TestREL004…`) and one function in `test_alpaca_account_day_pnl_fixture.py`. `test_validated_by_test_files_exist` checks only the file part (`test_golden_manifest.py:452-460`), so a removed class would go unnoticed. Every class keeps its parity tests in this list; the cutting PR must not empty or rename a named class.
- **H3 — The duplicates rely on `tests/test_statistics.py` surviving.** A17, A18, A20, A21 and A28–A30 name `tests/test_statistics.py::TestSharpe`, `::TestSortino`, `::TestMaxDrawdown` and `::TestTradeStatistics` as the stronger copies. That file belongs to **#2731**. If #2731 cuts any of those tests and names the fixture copies as stronger, both copies disappear. Coordinate: the fixture copies go and the `test_statistics.py` copies stay.
- **H4 — Only runners.** After the cuts, the scientific-proof step is still the only PR-path runner of `tests/fixtures/**`, and `Validate Golden Manifest` the only one for `test_golden_manifest.py`. The CI-shape ticket (#2738) must keep both, or move them together.
- **H5 — `golden_support/strategy_replay.py:18` imports `InMemoryDataReader` from `app/services/spec_strategy_runner.py`.** ENG-008's parity test and its generator depend on it. The #2703 / #2704 cut of `run_spec_against_bars*` and `pair_engine_fills` in that module must keep `InMemoryDataReader`. #2704's conditional row on `spec/schema.py::load_spec_from_path` also **must not land**: surviving spec tests call it (`spec/tests/_parity_helpers.py:274`, `test_spec_round_trip.py:33,42`), and test-only helpers follow their tests (☆).
- **H6 — Kill lists age.** Re-run `pytest --collect-only` on both `app/` directories and re-check each duplicate's stronger test at the cutting PR's SHA.

## Checked and kept

- **Every golden parity test in the area:**
  - each `*_matches_oracle` in the fixture files;
  - `test_alpaca_regulatory_fees_fixture.py` (both tests) and `test_alpaca_account_day_pnl_fixture.py`;
  - `test_strategy_parity_fixtures.py::test_strategy_trade_log_matches_pinned_pre_port_receipt`;
  - `test_return_distribution_golden.py::test_rd001_production_pipeline_matches_oracle_and_committed_output`;
  - `test_strategy_metric_help_golden.py` (both tests). `test_v2_return_metrics_have_an_independent_decimal_oracle` checks the fixture, not our code, but it *is* that fixture's oracle derivation. The fixture lives in `contracts/fixtures/` with no manifest entry and no generator, so this test is its attribution;
  - the ADX/MACD/Supertrend golden regressions and pandas-ta cross-checks;
  - `test_spy_validation.py`'s LEAN test;
  - the four spec ↔ hand-coded parity tests.
- **The manifest's real checks:** schema conformance, unique IDs, files exist, file/content/attribution hashes, canonical modules exist, callables importable, `validated_by` exists, non-empty tolerance note.
- **Warmup and edge tests beside parity:** `test_ema_ready_at_period`, `test_sma_ready_at_period`, `test_rsi_ready_at_period_plus_one`, `test_ema_monotone_case_exact`, `test_sma_rolling_window`, `test_rsi_monotone_increasing_gives_100`. Once #2704 cuts the `tests/indicators/*_persistence.py` files, these are the only warmup tests for EMA/SMA/RSI. Also kept: `TestRP004SignalZscore::test_train_mean_is_zero_in_zscore` (not strictly implied by parity at 1e-9) and `TestREL004ICDecayCurve::test_horizons_are_sequential`.
- **`app/engine/tests`, all real outcomes and no mocks:** `test_bar_store_path_injection.py` (path-injection guards), `test_evaluation_boundary.py`, `test_session_wrapper.py`, `test_execution_config.py`, `test_lean_format_session_filter.py`, `test_intrabar_resolver.py`, `test_bracket_exits.py` (see attention item 3), and `test_strategies_abc.py`'s gate tests.
- **`spec/tests` behavior tests:** manage rules, trailing stop, bar filter, prediction primitive, validators, and the live routes in `test_spec_router.py`.
- **`app/engine/tests/extract_lean_fixture.py`** and the in-app `fixtures/golden/*/regenerate.py`. They are the regeneration scripts of live fixtures (`spy_lean_trades.csv`, ADX/MACD/Supertrend), so they are sacred with them. `extract_lean_fixture.py` still points at the dead sandbox path (`:5`).

## Pointers — cuttable things outside this area

- `PythonDataService/run_spy_partial_parity.py`, `generate_validation_report.py` and `_validation_report.html` sit at the root of the service and cite B5 and `spy_lean_trades.csv` → **#2707** (scripts) / **#2711** (one-off docs).
- The Frontend `lean-engine-docs` component cites B3 and B5 as the parity contract → **#2709**.
- `docs/math-sources-of-truth.md:218` names B4 → **#2713** / **#2714**.
- `tests/test_statistics.py` stays as the stronger copy (H3) → **#2731**.

## For CI shape (#2738)

- `app/engine/tests/test_spy_validation.py::test_spy_ema_crossover_matches_lean_reference_trades` is slow, and it skips in CI when the SPY minute data is absent (`:189-197`), so in practice **no CI job runs it**. It is the repo's only full two-year bit-exact LEAN trade-log proof for the EMA crossover. Speed is not a reason to cut it; whether it ever runs is the CI-shape question.
- The fixture suite is not slow: 167 tests in 3.4 s locally. It runs in all 16 PR shards (#2716's note).

## Needs the map's attention

1. **How far does the volatility ruling reach?** I kept `test_volatility_fixtures.py` and `test_ibkr_iv_fixtures.py` whole, as instructed, including the row counts and oracle-only checks this list cuts everywhere else (about 25 tests in total). The ruling reads as "unused volatility math is not dead", which is a dead-code question. If that is all it means, those trivial rows could be cut on the same evidence as A8–A67. If it means "every volatility test stays", then A41, A47 and A48 (Black-Scholes vega, named in the ruling) should come back too. A short owner answer settles both.
2. **Five never-collected parity scripts (B1–B5).** They are named like tests and cited as parity proofs by the registry, two hashed algorithm docstrings, the math sources-of-truth doc and a Frontend docs page, but pytest has never collected them. Three of them read a path that no longer exists. The rules make them dead code, so they are cut. If the owner wants any of them as real tests, that is a rewrite, which the map puts out of scope. The live math each one claims to prove already has a running proof (named in each row).
3. **The engine's TP/SL bracket path looks test-only.** Nothing in `app/` outside `engine.py`, `execution/portfolio.py`, `order.py` and `intrabar_resolver.py` passes `take_profit_price` or `stop_loss_price`. That is the same shape as the LIMIT-order branch #2704 cut, but #2704 did not list it. If it is dead, `test_bracket_exits.py` (5 tests) and `test_intrabar_resolver.py` (14 tests) go with it. This needs a dead-code call. #2704 is closed, so it would be a follow-up row for the engine cutting PR, not something this test list can decide.

## Not reviewed

- `test_evaluation_boundary.py`, `test_session_wrapper.py`, `test_execution_config.py`, `test_lean_format_session_filter.py` and `test_supertrend.py` were judged from test names, docstrings and a scan for mocks and assertion counts. I did not read each test body line by line.
- The `golden_support/` modules were judged only as orphans, not as code. `Manifest.save` has no caller, but `golden_support` is test-support code outside the test-file brief, so it is not listed.
- I ran no mutation or coverage tool. "Implied by parity" rows rest on reading the assertions and the manifest tolerances (1e-9 or 1e-10, with `rtol` 0).
