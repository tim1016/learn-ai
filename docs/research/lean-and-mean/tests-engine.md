# Kill list — engine, indicator and volatility tests (#2727)

Part of map #2700. Plan, don't cut: nothing here has been deleted.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30). The map was charted at `87b8e261`; the blocking dead-code list (#2704) was read at `87b8e261`.
- **Area:** `PythonDataService/tests/engine/`, `tests/indicators/`, `tests/volatility/` (~22.7K lines, 110 files).
- **Kinds:** 1 trivial · 2 copy, doc or source-text pinning · 3 duplicate (the stronger survivor is named) · 4 mock theater · 5 retired feature.

## How this was checked

1. **Every test read against the bar.** An AST script pulled each test's name, docstring and assert lines. Every candidate was then read in full, along with its survivor.
2. **Duplicates were checked one layer up and one layer down**: fill model ↔ engine ↔ research runner, strategy ↔ registry ↔ program factory, schema ↔ evaluator.
3. **Retired-feature claims rest on caller searches.** `grep -rn` was run over `PythonDataService/app`, `scripts/`, `Backend/` and `Frontend/src`. Each search is quoted in its row.
4. **The area's fast lane was run** on the host venv (`pytest tests/engine tests/indicators tests/volatility -m "not slow" -rs`) to find tests that skip on every run, and slow ones.
5. **#2704 skips.** Everything #2704 (`research/lean-dead-engine:docs/research/lean-and-mean/dead-engine.md`) lists is skipped, tests included. Its owner addendum was applied: the IBKR-era readers stay cut, and test-only helpers follow their tests. The exceptions are the volatility rows, which the ruling below reverses.

## Rulings applied

- **Validated volatility math stays even when nothing uses it (owner, 2026-09-30).** This covers:
  - the IV solver;
  - surface fitting;
  - VIX-style IV30 replication, including the legacy `vix_style_iv30` path;
  - realized vol;
  - IV basis conversion;
  - put-call-parity forwards;
  - Black-Scholes vega;
  - `iv30_health`;
  - the `iv30_constructor` features.

  Every volatility math test in this area is therefore **KEEP**. That includes golden fixture IV-003 and its generators (`scripts/build_iv30_golden.py`, `scripts/fixture_generators/volatility.py`). It also reverses #2704's test rows for:
  - `tests/volatility/test_solver.py` (`solve_iv_chain`);
  - `test_price_normalization.py` (`from_eod_close*`, `from_recorded_snapshot`, the tiered half-spread);
  - `test_vix_replication.py` (the legacy tests, and `test_matches_legacy_math_on_clean_opra_chain`);
  - `test_basis.py` (`convert_iv_trading252_to_act365` round trips).

  The earlier worry that the live `vix_style_iv30_with_provenance` would be left without a golden proof is moot: IV-003 stays.
- **☆ Test-only helpers follow their tests.** No helper is listed here while a surviving test proves live behaviour through it. See hazard H2 for one place where this conflicts with #2704.

## Kill list

### tests/engine (top level)

| Path | Kind | Evidence |
|---|---|---|
| `tests/engine/test_engine_fee_routing.py` (whole file, 2 tests) | 3 | Same three IBKR fee cases (150@662.50→1.00, 365@270→1.83, 221@450→1.11) and the same flat-default check. Survivors: `tests/engine/test_fill_model_fee.py::test_compute_fee_with_ibkr_model_returns_per_fill_fee` and `::test_compute_fee_default_returns_flat_commission`. That file also proves that the fee reaches the `OrderEvent`. |
| `tests/engine/test_commission_reexport.py::test_engine_reexport_produces_canonical_fees` | 3 | Implied by `::test_engine_reexport_is_the_canonical_class` (`EngineModel is CanonicalModel`): one class cannot charge two fees. Keep the identity test; `app/engine/execution/commission.py:15` and `docs/math-sources-of-truth.md:186` cite the file. |
| `tests/engine/test_order.py` (whole file, 7 tests) | 1 | Enum-value echoes (`Direction.LONG.value == 1`, `OrderType.MARKET.value == "market"`) and dataclass field and default echoes (`order.tag == ""`, `event.fee == Decimal("1.00")`). The two fill-mode string pins (`test_fill_mode_enum_values`, `test_next_session_open_is_a_known_fill_mode`) are **3**: `tests/research/runs/test_runner_inmemory.py::test_parse_fill_mode_returns_next_session_open_enum_value` proves that the wire string parses to the enum. `fill_mode` is a plain `str` in the OpenAPI schema. |
| `tests/engine/test_trade_bar.py::test_trade_bar_is_frozen` | 1 | Tests `@dataclass(frozen=True)`. |
| `tests/engine/test_trade_bar.py::test_prices_preserved_as_decimal` | 1 | Passes `Decimal`s in and asserts `isinstance(..., Decimal)` out. With #2704's `test_period_seconds_matches_end_minus_time`, this empties the file. |
| `tests/engine/test_portfolio.py::test_position_direction_reflects_sign_of_quantity`, `::test_position_market_value_scales_with_quantity`, `::test_portfolio_cash_initialized_from_initial_cash`, `::test_drain_pending_clears_pending_list`, `::test_clear_pending_drops_pending_list` | 1 | Each tests a sign, a single multiplication, a constructor field echo, or a list being emptied. The engine's pending-order handling (`engine.py:435,635,708`) is proven end to end by `tests/engine/test_engine_fill_modes.py`. The fill-application math tests in this file stay. |
| `tests/engine/test_fill_model.py::test_deferred_fill_modes_membership_invariant` | 1 | Restates the `DEFERRED_FILL_MODES` literal. Deferral is proven by `::test_next_bar_open_defers_when_next_bar_missing`, `::test_next_session_open_defers_when_candidate_same_trading_date`, `::test_decision_minute_open_defers_a_bar_that_opened_before_the_order_was_sent` and `test_engine_fill_modes.py`. See H4: `fill_model.py:57` names this test. |
| `tests/engine/test_decision_snapshot.py` (whole file, 7 tests) | 5 | Tests `Strategy.last_decision_snapshot`, the hook that a `DecisionWriter` read to fill `decisions.parquet`. That writer and the parquet were retired at #1679. Today the attribute is **write-only**: `grep -rn decision_snapshot app scripts Backend Frontend/src`, excluding the assignments, finds no reader. Two of its tests (`:135`, `:185`) also skip on every run ("synthetic price path didn't trigger entry"). #2704 missed the app code; see pointer P1. |
| `tests/engine/test_deployment_validation_strategy.py::test_signal_decision_split_emits_the_same_sequence_the_retired_kernel_did` | 3 | HOLD→ENTER→HOLD→HOLD→EXIT through the signal-only seam. Survivors: `::test_two_green_bars_from_0945_enter_next_bar_open_and_exit_cycle` (the same cycle through `BacktestEngine`, with fill times) and `tests/engine/strategy/test_deployment_validation_signal_program.py::test_full_entry_to_exit_cycle_completes_without_on_order_event`. |
| `tests/engine/test_deployment_validation_strategy.py::test_exposes_consolidator_period_for_indicator_hydration` | 1 | `assert strategy.CONSOLIDATOR_PERIOD_MIN == 1`. The constant's only reader is `indicator_state.hydrate` (`app/engine/live/indicator_state.py:325,595`), which #2704 cuts. See P3. |
| `tests/engine/test_deployment_validation_strategy.py::test_live_start_registry_class_name_resolves_strategy_class` | 5 | Its docstring says "the runner reads `StrategyRegistration.class_name`". No `.class_name` read survives in `app/` or `scripts/`: the field is declared at `registry.py:267` and only assigned. The live-start runner it describes is gone. See P2. |
| `tests/engine/test_run_gate.py::test_every_engine_run_is_gated_wherever_it_is_built` | 3 | Asserts that the source text of `BacktestEngine.run` contains `"with one_backtest_in_flight():"`. Survivor: `::test_an_engine_built_anywhere_still_runs_one_at_a_time`, which builds two engines directly and proves they never overlap. |
| `tests/engine/test_run_gate.py::test_only_the_engine_backtest_service_may_call_the_ungated_core` | 2 | Greps every `app/**/*.py` for the private name `_execute_engine_backtest_core`. It is a naming lint, not an outcome. A caller that reaches past the wrapper still runs inside `BacktestEngine.run`'s own gate, which `::test_an_engine_built_anywhere_still_runs_one_at_a_time` proves. The wider hold is proven by `::test_the_engine_entry_point_holds_the_gate_for_its_callers`. |
| `tests/engine/test_single_store_tombstone.py::test_the_data_lake_flag_is_not_a_setting`, `::test_no_module_reads_the_data_lake_flag`, `::test_policy_store_write_path_symbol_is_gone`, `::test_the_full_range_exporter_is_gone`, `::test_the_lean_export_route_is_not_registered`, `::test_the_range_ensurer_is_gone`, `::test_no_new_module_resolves_lean_roots_from_the_environment` | 5 | Tombstones for the policy store retired in #1893. Each asserts an absence (`not hasattr(...)`, a route not in the table, a source grep for `DATA_LAKE_ENABLED`, an env-root allowlist), not an outcome. `test_the_lean_export_route_is_not_registered` boots the app (1.2 s). |
| `tests/engine/test_single_store_tombstone.py::test_the_engine_read_seam_resolves_the_lake_and_nothing_else` | 3 | Survivors: `tests/engine/test_policy_store.py::test_resolve_data_roots_returns_the_lake_root_alone` and `::test_resolve_data_roots_serves_each_adjustment_mode_from_its_own_root`, which test the same `resolve_data_roots` and the same one-root answer per mode. With the row above, this empties the file. |
| `tests/engine/test_availability_calendar.py::test_a_year_long_gap_is_named_as_one_range` | 2 | Pins the full refusal string with `==`. The behaviour (a hole is compressed into ranges) is proven by `::test_missing_sessions_either_side_of_a_closure_are_one_span` (the spans) and `::test_gaps_past_the_shown_limit_are_counted_not_listed` (the message names ranges and counts the overflow). |
| `tests/engine/test_policy_store.py::test_policy_key_encodes_source_and_adjustment` | 1 | Two f-string echoes (`"polygon-adjusted"`, `"polygon-raw"`). |
| `tests/engine/test_sizing.py::test_lean_free_portfolio_pct_is_lean_default` | 3 | Pins `0.0025`. The golden parity test `::test_lean_sizing_reproduces_every_golden_entry` (20 LEAN entries, atol=0) fails if the buffer moves. The parity test and `::test_fixture_present_and_nonempty` (which keeps the parity test from passing on zero entries) stay. |

### tests/engine/live

| Path | Kind | Evidence |
|---|---|---|
| `tests/engine/live/test_run_status.py::test_atomic_write_json_creates_file` | 3 | Survivor: `::test_atomic_write_json_content_correct` writes and reads back the same file. `_atomic_write_json` is live through `services/bot_binding_repository.py:26,334`. |
| `tests/engine/live/test_deployment_validation_deploy_artifacts.py::test_deployment_validation_qc_shadow_copy_is_parseable` | 3 | Asserts that the QC shadow copy declares `timedelta(minutes=15)` offsets. Survivor: `tests/engine/test_deployment_validation_session_window_parity.py::test_qc_shadow_copy_formula_matches_fixture_boundaries`. It extracts the same two offsets from the same class and checks them against the regular-day and half-day fixture boundaries. The spec-fixture test in the file stays, because `strategy_validation_manifest.json` names that spec. |
| `tests/engine/live/test_qc_python_parity_fixture.py` (whole file, 1 test) | 5 | The "§ 8.1 pre-paper-week" QC↔Python gate. It skips whenever `references/qc-shadow/backtests/lean-parity-fixture/trades.csv` is missing, and that file has never been committed: `references/qc-shadow/backtests/` holds only two other date-range folders. So the test has skipped on every run since it landed (2026-05-08). Python↔LEAN on the same fixture is proven by `app/engine/tests/test_spy_validation.py`. `references/qc-shadow/README.md:68` tells the operator to run it (see orphans). |

### tests/engine/strategy

| Path | Kind | Evidence |
|---|---|---|
| `tests/engine/strategy/algorithms/test_ema_crossover_signal_parameterized.py::test_gap_threshold_defaults_to_020_and_is_configurable` | 3 | Calls the private `_gap_is_sufficient`. Survivors: `::test_entry_check_honors_a_configured_gap_the_default_would_reject` and `::test_entry_check_honors_a_configured_gap_tighter_than_the_default` prove a configured gap through the real entry check. The 0.20 default is pinned by `::test_registration_exposes_gap_and_rsi_with_preserved_defaults` and by the LEAN golden in `app/engine/tests/test_spy_validation.py`. |
| `…/test_ema_crossover_signal_parameterized.py::test_rsi_band_defaults_to_50_70_and_is_configurable` | 3 | Calls the private `_rsi_gate_bounds`. Survivors: `::test_entry_check_honors_a_configured_rsi_band_the_default_would_reject` and `::test_registration_exposes_gap_and_rsi_with_preserved_defaults`. |
| `tests/engine/strategy/algorithms/test_spy_ema_crossover_emitters.py::test_constructor_accepts_output_dir`, `::test_constructor_defaults_output_dir_to_none` | 1 | `assert s._output_dir == tmp_path` / `is None`: constructor echoes into a private field. `::test_initialize_creates_csvs_with_correct_headers` proves that the dir is used. |
| `…/test_spy_ema_crossover_emitters.py::test_no_csvs_when_output_dir_is_none`, `::test_on_end_of_algorithm_closes_handles` | 1 | Each asserts only that private attributes (`_observations_writer`, `_observations_fp`, …) are `None`. The CSV outcome tests in the file stay (headers, one row per minute, warmup-gated state rows); the cross-engine study regenerator uses those emitters (`scripts/regenerate_cross_engine_study.py:263`). |
| `tests/engine/strategy/spec/test_schema.py::test_prediction_ref_lookup_accepts_next_after_bar_close` | 3 | Survivor: `tests/engine/strategy/spec/test_evaluator_predictions.py::test_evaluator_consumes_next_row_at_decision_time_under_next_after_lookup`. It validates a spec with `lookup="next_after_bar_close"` and proves the evaluator reads the next row. |
| `tests/engine/strategy/spec/test_schema.py::test_prediction_ref_lookup_rejects_unknown_value`, `::test_strategy_spec_bar_source_rejects_unknown_descriptor` | 1 | Each tests that a Pydantic `Literal` rejects a string outside the literal. That is Pydantic, not this code. |
| `tests/engine/strategy/spec/test_schema.py::test_strategy_spec_rejects_client_id` | 5 | IBKR client-id tombstone ("IBKR client IDs are runtime infrastructure"). It asserts that a retired field is rejected. The two default tests in the file stay. |
| `tests/engine/strategy/test_deployment_validation_signal_program.py::test_registry_factory_is_the_single_public_deployment_validation_program_construction_seam` | 3 | Asserts `strategy.signal_program is program` (the test helper's own wiring) and `build(...).signal_program is not None`. Survivors: `tests/engine/strategy/test_registry_signal_program_identity.py::test_every_factory_built_program_carries_its_registration_identity` and `::test_every_registered_strategy_decouples_signal_from_traded_asset`, which cover every registration. |
| `tests/engine/strategy/test_ema_signal_program.py::test_registry_factory_is_the_single_public_ema_program_construction_seam` | 3 | Same assertions and the same survivors as the row above. |
| `tests/engine/strategy/test_signal_decision_digest_closure.py::test_production_closure_helper_uses_the_real_service_root` | 1 | `assert (_SERVICE_ROOT / "app").is_dir()`. If the root were wrong, the qualification matrix's closure tests would fail on every program first. |
| `tests/engine/strategy/test_signal_program_session_boundaries.py::test_dst_transition_fixed_offset_would_have_been_wrong_by_one_hour` | 1 | Asserts zoneinfo's EST/EDT offsets, then that a fixed-offset helper defined in the test is off by an hour. It tests the tz database and a straw man. The DST outcome is proven by `::test_dst_transition_decision_clock_accepts_bars_from_both_sides` (every sealed program, across the transition). |

### tests/volatility — non-math constructor echoes inside kept math files

The ruling keeps the math. These rows test no math, only dataclass mechanics.

| Path | Kind | Evidence |
|---|---|---|
| `tests/volatility/test_iv_provenance.py::TestIvProvenanceContract::test_minimal_construction_succeeds`, `::test_per_strike_contributions_optional`, `::test_max_single_strike_share_default_is_zero` | 1 | Construct an `IvProvenance` and read back the fields or defaults passed in. The range validators and round-trip tests in the file stay, and so does `test_empty_price_source_mix_allowed` (the empty-mix edge the IV30 path can produce). |
| `tests/volatility/test_price_normalization.py::TestNormalizedOptionPriceContract::test_source_is_required_at_construction` | 1 | Asserts that Python raises `TypeError` for a missing dataclass field. |
| `tests/volatility/test_price_normalization.py::TestNormalizedOptionQuote::test_pair_construction`, `::test_mixed_sources_per_strike_allowed` | 1 | Construct a quote and echo `strike` and `source` back. |
| `tests/volatility/test_conventions.py::TestSurfaceConventionsDataclass::test_frozen_dataclass` | 1 | Tests `@dataclass(frozen=True)`. |

## Waits on the volatility-transport ruling

These tests cover the code around the math, which the owner is still ruling on: the unused `/api/volatility/surface/*` routes (#2706), the surface cache, the chain loader, the route-only models and the stub page. Neither cut nor keep yet.

| Path | What it tests | Why it waits |
|---|---|---|
| `tests/volatility/test_cache.py` (whole file, 21 tests) | `SurfaceCache` read and write | #2704 lists it. The router calls `_cache.load_surface`, which does not exist, so the cache never runs. |
| `tests/volatility/test_conventions.py::TestSurfaceConventionsDataclass::test_to_hash_dict_complete`, `::test_to_hash_dict_is_dict` | the surface-cache key | `to_hash_dict` is read only by `app/volatility/cache.py:72`. |
| `tests/volatility/test_surface.py::TestSurfaceQuery::test_grid_output` | `VolSurface.to_grid` column names | `to_grid` is called only by `routers/volatility.py:647,870` and the dead `example.py`. |
| `tests/volatility/test_analytics.py::TestHealthScore` (4 tests) | `compute_health_score` | Read only by `routers/volatility.py:219,278,947`. It scores route diagnostics; it is not one of the named math families. The skew-metric, delta-strike and put-call-parity-forward tests in the same file are **KEEP** under the ruling. |

No test in this area targets `data_loader.py`, the route-only models in `app/volatility/models.py` or the stub Volatility page.

## Proven math, unused

The owner is being asked whether the keep extends here. Nothing below is cut.

| Path | Proof | Why it is unused |
|---|---|---|
| `tests/engine/indicators/test_vwap_reversion_indicators.py` (2 tests) | Parity of `SessionAnchoredVwap` and `RollingDistanceSigma` against an independent numpy oracle (`atol=1e-9, rtol=0`) | This is the SPY VWAP reversion port. It is reachable only through the reflective resolver behind the `cross-reconcile` route, which #2706 cuts (#2704's conditional row). Its golden fixture `spy-vwap-reversion-qc` and that fixture's reconciliation test sit outside this area (`tests/integration/`, #2729). |
| `tests/engine/strategy/test_spy_vwap_reversion.py` (1 test) | No math. It proves through a monkeypatched counter that `_session_bounds_minutes_et` caches the calendar lookup. | It belongs to the same port. If the keep extends, this test still fails the bar on its own (4: it asserts a call count). |

Checked and **not** reference-proven, so they stay on #2704's list:

- the indicator warm-start persistence tests (`tests/indicators/*`, `tests/engine/live/test_indicator_state_*`, `test_spy_ema_persistence.py`) — self-consistency round trips;
- the divergence and replay classifiers (`tests/engine/live/divergence/`);
- `action_plan/test_parity.py` (non-blocking schema diagnostics);
- the limit-order fill path. Its "user-specified" penetration rule has no reference fixture (`app/engine/tests/test_limit_orders.py`, #2737's).

## Already on #2704's list (skipped)

These are skipped outright:

- `tests/indicators/` (all four files);
- `tests/engine/live/divergence/`;
- the whole-file `tests/engine/live/` rows;
- `action_plan/test_parity.py`;
- `strategy/spec/test_descriptors.py`.

The `rollback_blocked_*`, persistence and executor tests in the partial files are skipped too.

One test goes with #2704's rows, but its test list does not name it: `tests/engine/live/test_order_identity.py::test_validate_components_equality`. It is the only caller of `validate_order_ref_components` (#2704 row B), so it leaves with that row. (`test_deployment_validation_strategy.py`'s `test_is_not_warm_startable`, `test_spy_ema_remains_warm_startable` and `test_satisfies_live_persistence_contract` are already covered by #2704's "the `is_warm_startable` / `hydrate_policy` / persistence tests".) A second file #2704 does not name is hazard H2: it is broken by a #2704 row, not removed with one.

The survivors of #2704's partial files were read and kept. They are path-confinement, registry-read and lifecycle tests on the money path:

- in `test_account_artifacts.py`, the root and symlink tests;
- in `test_account_registry.py`, the read and index tests.

## Checked and kept (so the next reader does not re-derive them)

- **`test_signal_program_mode_parity.py::test_dry_run_and_paper_share_the_same_evaluation_stream`** inspects source, but it is the only thing tying the cross-mode trace-parity proof to the real `run_trade_bot` / `run_dry_run_bot`.
- **`test_registry_signal_program_identity.py::test_validated_against_only_names_evidence_that_actually_exists`** keeps the sealed contracts' `validated_against` honest, and no row above removes a file it names (see H3).
- **The money-path files: `test_desired_state.py`, `test_bot_lifecycle_state.py`, `test_order_identity.py` and `test_identity.py`.** Every test proves a custody, lifecycle, ownership or path-confinement outcome.
- **`test_sizing.py::test_fixture_present_and_nonempty`** stops the golden parity test from passing on zero entries.
- **`test_availability_calendar.py::test_a_first_check_over_500_sessions_schedules_once_per_quarter_not_per_day`** counts calls, but the outcome is the first-check latency the owner sees (its docstring profiles a ~5 s check, ~4 s of it per-day schedule calls).
- **The kept volatility math:**
  - `test_solver.py`, `test_solver_parity_pyvollib.py`, `test_fitting.py`, `test_basis.py`;
  - in `test_surface.py`, everything except `test_grid_output`;
  - in `test_analytics.py`, the skew, delta-strike and parity-forward tests;
  - in `test_conventions.py`, the forward, discount and day-count tests;
  - `test_vix_replication.py`;
  - in `test_price_normalization.py`, the normalizer and validator tests;
  - in `test_iv_provenance.py`, the validator and round-trip tests.

## What the cuts orphan

- **Fixtures:** none. `app/engine/tests/fixtures/spy_lean_trades.csv`, read by the cut QC parity test, is still read by `test_spy_validation.py`. The deployment-validation session-window fixture stays with its parity test.
- **Helpers and conftest:** none. Every cut uses only helpers local to its own file. `tests/engine/strategy/conftest.py::build_program` and `tests/_helpers/signal_program.py` keep their surviving users.
- **Files emptied:** `tests/engine/test_engine_fee_routing.py`, `test_order.py`, `test_trade_bar.py` (with #2704's row), `test_decision_snapshot.py`, `test_single_store_tombstone.py`, `tests/engine/live/test_qc_python_parity_fixture.py`.
- **App code left untested and unread** (pointers, not rows):
  - **P1** `DecisionSnapshot` (`strategy/base.py:35`), `Strategy.last_decision_snapshot` (`base.py:266`), `_DeploymentDecisionSnapshot` and its publication (`deployment_validation.py:71,108`), and the publication at `ema_crossover_signal.py:384` — write-only, no reader.
  - **P2** `StrategyRegistration.class_name` (`registry.py:267`): assigned, never read.
  - **P3** `CONSOLIDATOR_PERIOD_MIN` (`deployment_validation.py:81`, `ema_crossover_signal.py:109`): read only by the dead `indicator_state`. `spy_vwap_reversion.py:38,66` reads its own copy.
- **Stale mentions, not markdown links** (so `scripts/check_documentation_contract.py` does not fail):
  - `docs/architecture/engine-authority-map.md:53` names `tests/engine/test_single_store_tombstone.py`;
  - `references/qc-shadow/README.md:68` tells the operator to run `test_qc_python_parity_fixture.py`;
  - `app/engine/execution/fill_model.py:57` names `test_deferred_fill_modes_membership_invariant` in a comment.
- **`scripts/pr_shard_durations.json`** keeps node IDs for cut tests. `scripts/pytest_shard.py:7-11` treats missing tests as harmless, so the file can be refreshed at leisure.

## Hazards the cutting PR must carry

- **H1 — Signal Program build proofs.** The test cuts edit no hashed file. Pointers P1 and P3, if the dead-code follow-up takes them, edit `strategy/base.py`, `algorithms/ema_crossover_signal.py` and `algorithms/deployment_validation.py`. All three are in `DECLARED_PROGRAM_SOURCE_PATHS` (`program_sources.py:35,36,109`), so those edits batch into #2704's single re-qualification PR. P2 (`registry.py`) is not a hashed path at this SHA.
- **H2 — #2704's `rollback_blocked_*` cut breaks a surviving custody test (☆ conflict).** `tests/_helpers/signal_program.py:296,337-342` (`ROLLBACK_METHODS`, `custody_surface`) derives each program's custody surface by parsing the source of `rollback_blocked_entry` / `rollback_blocked_exit`. `tests/engine/strategy/test_signal_program_discard_safety.py::test_discarded_evaluation_leaves_position_custody_untouched` asserts that the surface is non-empty, then that a DISCARD leaves it untouched. That is a custody outcome on every sealed program. #2704 row D deletes the methods, and its test list does not name this file. The cut would turn the test red ("declares no custody surface"). `test_signal_program_session_boundaries.py::test_pause_observe_only_records_trace_without_commit_and_program_still_decides_after` has the same dependency. Under ☆ the methods are test-only helpers that a surviving money-path test reads through, so they stay.
- **H3 — `validated_against` names tests by path.** `test_registry_signal_program_identity.py::test_validated_against_only_names_evidence_that_actually_exists` fails when a sealed contract names a deleted file. This list deletes none of the named files: it only trims single tests from `test_deployment_validation_strategy.py` and `test_deployment_validation_signal_program.py`. A cutting PR that drifts from this list must re-check `registry.py`'s `validated_against` strings.
- **H4 — One comment in app code.** `fill_model.py:57` cites `test_deferred_fill_modes_membership_invariant`. Re-point it at the surviving defer tests, or drop the parenthesis. It is a comment-only edit in a file that is not hashed.
- **H5 — Kill lists age.** Re-run the caller searches at the cutting PR's SHA, especially the write-only claim for `last_decision_snapshot` and the unread `class_name`. That includes any serialized registry payload: no `.class_name` read exists today.

## For CI shape (#2738)

- No kept math test in this area is slow. Every volatility test runs in under 0.3 s, setup included.
- The area's two `@pytest.mark.slow` tests already run daily:
  - `test_signal_program_qualification_matrix.py::test_validated_settings_corpus_has_a_pinned_trace_root` (the sealed golden-corpus gate);
  - `test_signal_program_build_receipt_generation.py::test_generator_output_matches_committed_manifest_for_the_current_tree`.

  The build-proof admission gate therefore runs daily, not on every PR. That is #2738's call.

## Pointers — outside this area

- **#2704** (dead code): P1 `DecisionSnapshot` / `last_decision_snapshot` publication, P2 `StrategyRegistration.class_name`, P3 `CONSOLIDATOR_PERIOD_MIN`. Hazard H2 (`rollback_blocked_*`) also belongs here.
- **#2737** (tests inside `app/`): `app/engine/tests/` and `app/engine/strategy/spec/tests/` were not judged here.
- **#2729** (integration tests): `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py` and its fixture belong to the "proven math, unused" question.
- **#2741** / **#2713** (docs): the stale test mentions in `engine-authority-map.md:53` and `references/qc-shadow/README.md:68` (operator workflow "Test 1").
- **#2738** (CI shape): the two slow-marked build-proof tests above.

## Needs the map's attention

1. **#2704 and ☆ conflict on `rollback_blocked_*` (H2).** #2704 row D lists the ten methods as dead. The custody-surface helper that a surviving discard-safety test depends on parses them. Under ☆ they stay while that test survives; the alternative is a test rewrite, which the map puts out of scope.
2. **#2704 missed three dead symbols on hashed files (P1–P3).** They are write-only or unread today. They belong in #2704's single re-qualification PR, not a separate one.
3. **The ruling reverses part of #2704 and #2706.** #2704's volatility test rows (legacy IV30, `iv30_health`, `solve_iv_chain`, the basis round trip, `compute_put_call_parity_forward`, `discount_factor`, the `iv30_constructor` helpers) are now KEEP. #2706's pointer that `fitting.py` is reached "because the live `/api/edge/iv30/*` routes use them" is inaccurate: once the router goes, `surface.py` and `fitting.py` are reached only through the `app/volatility/__init__.py` re-exports and golden IV-002. The ruling keeps them anyway, so this needs no action. Note it, so that no later re-sweep cuts them as orphans.
4. **Kept math tests that never run.** `tests/volatility/test_analytics.py::TestPutCallParityForward::test_put_call_parity_forward_exists`, `::test_put_call_parity_forward_reasonable` and `::test_put_call_parity_forward_multiple_expiries` skip on every run ("No matched call/put pairs in test data"). Only `::test_put_call_parity_forward_matched_pairs` exercises the math. The ruling keeps them. The owner may want to know they prove nothing as written; fixing their data would be a test rewrite, which is out of scope.
5. **`test_qc_python_parity_fixture.py` is listed as kind 5, not kept as parity.** It has no fixture: the QC export it compares against was never committed, so it has never run. If the owner reads "math parity" to include planned-but-never-captured references, pull the row.

## Not reviewed

- `tests/engine/live/test_account_artifacts.py`, `test_account_registry.py`, `test_live_state_sidecar.py`, `test_durable_append_log.py` and `test_account_binding_ledger.py` were read only far enough to confirm that their #2704 survivors are money-path outcomes. They were not judged test by test beyond that.
- The emitter and CSV schemas in `test_spy_ema_crossover_emitters.py` were not cross-checked against the LEAN trusted sample's columns.
- Whether a Frontend or Backend reader consumes `StrategyRegistration.class_name` through a serialized registry listing was checked by `grep` only, not by tracing every registry-to-JSON path (H5).
