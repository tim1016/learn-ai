# Kill list — service tests f–z (#2725)

Part of map #2700. Read at **`6a4d7d396108ef16471d8df888b9ded74d3c2892`** (`origin/master` on 2026-09-30, when the branch was cut). Paths are relative to `PythonDataService/tests/services/` unless they say otherwise.

Area: `test_[f-z]*.py`. At this SHA that is 69 files (the ticket says 68). Three of them belong to the dead-code list #2703, so I skipped them (see **Pointers**). That leaves 66 files and 734 tests.

## How this was judged

- An AST scan listed every test with its docstring, its assert count, and flags for: no assert, mock or call assertions, assertions that only compare strings, reads of source or docs, and single `is not None` / `isinstance` checks. I judged every test from that listing against the five kinds. I opened the body of each flagged test, and of each test whose name suggested a pin, a restated constant or a duplicate (about 50 bodies).
- For duplicates I looked one layer up and one layer down: route ↔ service, service ↔ pure function, unit ↔ end-to-end. Each duplicate row names the survivor.
- Rulings applied: ★ sacred is the behaviour, not the file; ☆ test-only helpers follow their tests; and the **2026-09-30 volatility ruling**: validated volatility and IV math stays even when nothing uses it, with its tests. Two rows that would have been kind 4 are kept under that ruling (see **Considered and kept**).
- Kind 5 (retired features) belongs to the dead-code tickets. I skipped everything #2703, #2706 and #2704 already list.

## Kill list

| # | Item | Kind | Evidence |
|---|---|---|---|
| 1 | `test_gallery_hub.py::test_gallery_hub_reuses_canonical_markers_projection` | 1 | Its whole body is `assert gallery_hub.markers_in_window is markers_in_window`, an import-identity check. `test_build_snapshot_populates_markers_from_todays_fills` proves the marker outcome. |
| 2 | `test_ibkr_history_settle_fixture.py::test_the_fixture_is_the_sample_the_reference_note_cites` | 2 | Asserts only that the frozen probe file has 16 records and 3 revisions, the counts the reference note quotes. It runs no code. The fixture stays, because `test_the_default_settle_time_is_at_least_twice_the_slowest_revision_observed` reads it against `Settings`. |
| 3 | `test_iv_recorder.py::TestInMemoryStore::test_round_trip` | 4 | Round-trips `InMemoryIvSnapshotStore`, a test fake that lives in app code. The real store's round trip is `TestJsonlStore::test_round_trip_persists_to_disk`. This is store plumbing, not IV math, so the volatility ruling does not cover it. |
| 4 | `test_iv_recorder.py::TestInMemoryStore::test_filter_by_window` | 4 | Tests the fake's own `read_series` window filter. Production reads through `JsonlIvSnapshotStore` (`app/routers/edge.py:284`), which this test never touches. Not IV math. |
| 5 | `test_iv_recorder.py::TestRecorderService::test_invalid_slot_rejected` | 3 | The same refusal, one layer up, is what the caller sees: `TestRecorderRoutes::test_snapshot_route_invalid_slot_returns_400`. |
| 6 | `test_iv_recorder.py::TestSlotChoicesContract::test_default_slots` | 1 | Restates the `SLOT_CHOICES` tuple literal. It does not check the Backend job's slots, so it is not a cross-stack contract. |
| 7 | `test_lean_sidecar_template_registry.py::test_ema_crossover_is_in_source_registry` | 1 | `X in TRUSTED_TEMPLATE_DEFINITIONS` plus `.source is CONSTANT` restate the definitions literal. `test_every_declared_lean_twin_resolves_to_a_bundled_template` proves that each twin template resolves. |
| 8 | `…::test_ema_crossover_brokerage_policy_is_algorithm_default` | 1 | Restates one field of the same literal. A change detector: it cannot tell a right value from a wrong one. |
| 9 | `…::test_ema_crossover_signal_is_in_source_registry` | 1 | As row 7, plus `EMA_CROSSOVER_SIGNAL_SOURCE is EMA_CROSSOVER_SOURCE`, an identity of two module constants. |
| 10 | `…::test_ema_crossover_signal_brokerage_policy_is_interactive_brokers` | 1 | As row 8. |
| 11 | `…::test_ema_crossover_two_bps_is_in_source_registry` | 1 | As rows 7 and 8 together. |
| 12 | `…::test_deployment_validation_is_in_source_registry` | 1 | As row 7. |
| 13 | `…::test_deployment_validation_brokerage_policy_is_algorithm_default` | 1 | As row 8. |
| 14 | `…::test_existing_templates_still_registered` | 1 | Two dict-membership asserts. |
| 15 | `…::test_rsi_mean_reversion_is_in_source_registry` | 1 | As rows 7 and 8 together. |
| 16 | `test_polygon_notice_classifier.py::test_missing_polygon_api_key_notice_names_its_own_surface` | 2 | Parametrized subject → exact message string; the docstring calls it a pin on shipped copy. The shared code is proven by `test_missing_polygon_api_key_notice_is_distinct_from_auth_error`. |
| 17 | `test_polygon_notice_classifier.py::test_coordinator_unavailable_notice_names_its_own_surface_and_leaks_nothing` | 2 | Compares the message to a static sentence. The "no `://`" check can only fail if someone edits the literal. `test_coordinator_unavailable_notice_is_not_a_polygon_code` proves the code split. |
| 18 | `test_previous_close.py::test_fetch_rth_closes_passes_buffer_and_adjusted_to_polygon` | 4 | Every assert reads `polygon.fetch_aggregates.call_args.kwargs` on a `Mock`. No closes map or PC value is checked. |
| 19 | `test_run_replay_stop_trigger.py::test_stop_locked_schedules_the_replay_receipt` | 4 | An AST walk that asserts a call *expression* appears in six method bodies; the docstring calls it an "AST pin". It runs nothing. The end-of-bot stop is proven behaviourally by `bot_runner/test_bot_end.py::test_a_stop_at_its_end_proven_after_a_later_run_began_is_recorded_as_the_stopped_runs_alone` (`:862`). |
| 20 | `test_run_replay_stop_trigger.py::test_supervise_schedules_the_replay_receipt_on_every_terminal_branch` | 4 | The same AST idiom, applied per `except` branch of `_supervise`. |
| 21 | `test_run_replay_stop_trigger.py::test_run_boot_recovery_resumes_pending_replay_receipts` | 4 | AST: asserts that `run_boot_recovery` contains a call to `_resume_pending_replay_receipts`. What the scan schedules is proven by `test_resume_pending_replay_receipts_schedules_terminal_runs_lacking_evidence` and `…_skips_runs_with_final_receipts`. |
| 22 | `test_run_verdict_parity.py::test_a_high_psr_note_names_the_missing_selection_adjustment` | 2 | It pins note wording (a marker phrase must be present, banned phrases absent). Its score half, 0.97→20 and 0.999→18, duplicates `test_the_frozen_v2_psr_thresholds_and_scores_are_unchanged` (0.95→20, 0.99→18). The golden-fixture tests in this file stay. |
| 23 | `test_strategy_lean_source.py::test_resolve_strategy_lean_source_returns_rsi_mean_reversion_twin` | 3 | `test_lean_sidecar_template_registry.py::test_every_declared_lean_twin_resolves_to_a_bundled_template` already covers template, source and `class MyAlgorithm(QCAlgorithm)` for every twin. The sha256 check is proven by `test_resolve_strategy_lean_source_returns_registered_qc_algorithm`. |
| 24 | `test_strategy_lean_source.py::test_rsi_mean_reversion_twin_pins_its_thresholds_as_constants` | 2 | Substring checks on the LEAN twin's source text (`"OVERSOLD = 30" in source`). Whether the twin behaves the same is the parity companion's job, not a text match. This is not a golden fixture and not a tolerance-pinned parity test. |
| 25 | `test_strategy_lean_source.py::test_rsi_mean_reversion_twin_mirrors_the_canonical_decision_branches` | 2 | Substring checks (`"if rsi > self.OVERBOUGHT:" in source`, …). As row 24. |
| 26 | `test_strategy_validation_manifest.py::test_default_runtime_flag_event_path_uses_ignored_service_artifacts` | 1 | Restates a path constant (`endswith("…/flag_events.json")`). |
| 27 | `test_strategy_validation_manifest.py::test_flag_events_ledger_path_resolves_outside_the_real_artifacts_tree` | 1 | Asserts that the `tests/conftest.py` autouse fixture patched a module constant. It tests the test harness and proves no outcome of the product. Its sibling, `test_bare_load_strategy_validation_entries_call_tracks_the_patched_ledger_path`, proves the real behaviour (the default is resolved at call time) and stays. |
| 28 | `test_surface_hub.py::test_surface_hub_declares_latest_wins_client_queue_bound` | 1 | Restates `resource_limits` constants and `queue.maxsize == 1`. `test_snapshot_watchers_are_latest_wins_queue_one` proves the latest-wins outcome. |

Nothing in the money-path files goes: run admission, the stale-decision gate, lane quiesce, go-live hold and release, the clerk transaction projection, the source-bar ledger, Signal Program admission, crash replay and retention, strategy-validation admission, the today statement, sovereign equity snapshots, startup join, the retained-tail join, the stranded warmup bucket, trade-bot source bars, market liveness, and session authority (apart from #2703's four tests). I found no wording pins, trivial checks or call-order checks in them. Their mock-free "no order reached the clerk" assertions (`test_stale_decision_gate.py:261,377`) are outcomes.

## What the cuts orphan

- **Imports that ruff F401 will flag** once the rows go:
  - `test_lean_sidecar_template_registry.py`: `EMA_CROSSOVER_SOURCE`, `EMA_CROSSOVER_SIGNAL_SOURCE`, `EMA_CROSSOVER_2_BPS_SOURCE`, `DEPLOYMENT_VALIDATION_SOURCE`, `RSI_MEAN_REVERSION_SOURCE`.
  - `test_strategy_lean_source.py`: `RSI_MEAN_REVERSION_SOURCE`.
  - `test_gallery_hub.py`: `markers_in_window`.
  - `test_iv_recorder.py`: `SLOT_CHOICES`.
- **Test-file helpers:**
  - `test_run_replay_stop_trigger.py`: `_method_calls`, `_body_calls` and `_supervise_terminal_branches` (`:22-65`), with the `ast`, `inspect` and `textwrap` imports they use.
  - `test_run_verdict_parity.py`: `_SELECTION_MARKER` and `_BANNED_PSR_PHRASES`.
  - `test_polygon_notice_classifier.py`: the parametrize table of row 16.
- **Empty classes:** `test_iv_recorder.py::TestInMemoryStore` and `::TestSlotChoicesContract`.
- **App code: none.** `InMemoryIvSnapshotStore` stays, because `TestRecorderService` (provenance and error-row tests) reads through it (☆). `SLOT_CHOICES` and `markers_in_window` are live app symbols.
- **Fixtures: none.** The settle-probe golden fixture is still read by the surviving test in its file.
- **conftest: none.** No whole file goes.
- **`scripts/pr_shard_durations.json`:** the node IDs of every row.

## Hazards the cutting PR must carry

- **Re-check at your own SHA.** In particular, confirm that each row's named survivor still exists and still asserts the same outcome.
- **The replay-receipt question (needs the map).** #2706 cuts `app/routers/run_replay.py`. That router is the only reader of a finished replay receipt: `registry.run_replay_receipt(…)` at `:53` and `generate_run_replay_receipt` at `:74`. Every Stop still schedules generation (`app/services/bot_runner.py:1319,2267,2337`). After #2706 the receipts are write-only. If write-only evidence counts as dead, the receipt half of `app/services/run_replay_proof.py` goes as kind 5, and so do most tests in `test_run_replay_{engine_parity,fidelity,live_capture,live_evidence,proof_assembly,proof_service,receipt_end_to_end,receipt_store,stop_trigger}.py`. That is a dead-code call, so this list leaves them in place. Rows 19–21 are independent of the answer.
- **Rows 19–21 leave two wirings unproven.** After the cut, no test proves that an owner Stop (`_stop_locked`) or a feed-death or crash ending (`_supervise`) schedules a receipt. Only the end-of-bot stop is proven, in `test_bot_end.py`. If the receipts stay live, a behavioural test there is a new-test decision, outside this list.
- **Rows 8, 10, 11, 13 and 15 pin brokerage policies.** These values choose LEAN's fee model in reconciliation. The tests only restate the table, so they cannot catch a wrong value. The same-style pins in `tests/lean_sidecar/test_template_selection.py:63,71` belong to #2728 and should get the same treatment in the same pass.
- **Rows 16, 17 and 22 cut wording tests only.** The strings stay in the code. Nothing that renders them changes.
- **Rows 24–25.** The owner's 2026-09-30 volatility ruling keeps unused vol math. It does not cover LEAN twin source text. If the map extends "proven math" to the twin oracles, keep these two.

## Considered and kept

- `test_options_companion_service.py::TestProcessContractWarmStartIv::test_solver_called_with_advancing_vol_guess` asserts the `vol_guess` arguments a faked IV solver received, which would be kind 4. It is **kept by the 2026-09-30 volatility ruling** (IV solver, with its tests).
- `test_rate_dividend_service.py::TestRateAndDividendFacade::test_passes_dte_to_fred` asserts the arguments a fake rate function received, which would be kind 4. It is **kept by the same ruling**: the rate tenor feeds the IV30 inputs. On math, when unsure, keep.
- `test_run_replay_fidelity.py::test_run_fidelity_over_bars_full_parity_on_an_unblocked_run` and `::test_run_fidelity_over_bars_classifies_a_blocked_enter_as_expected` near-duplicate `test_run_replay_receipt_end_to_end.py::test_end_to_end_faithful_run_yields_full_parity` / `::test_end_to_end_blocked_enter_is_classified_not_reported_as_drift`. But that file is `pytestmark = pytest.mark.slow`, which means daily only. Cutting the fast pair would leave PR CI without the outcome. That is a CI-shape question (#2738).
- `test_ibkr_history_equivalence_fixture.py` (3 tests) asserts properties of frozen vendor data (history minutes == live minutes in RTH, 20 of 69 differ outside RTH) and runs no app code. These are the receipt under a golden fixture with an `attribution.md`, so I kept them as sacred. See the map question below.
- `test_live_chart_window.py::test_polygon_overlay_missing_key_notice_matches_shipped_live_copy` pins copy, but it is the only test that drives the missing-key branch through `resolve_chart_window`. It is not wording only, so it stays.
- `test_parity_companion.py::test_mark_parity_failed_swallows_database_errors` has no assert. It proves that the best-effort mark does not raise into its caller, which is an outcome.
- `test_parity_companion.py::test_only_templates_that_honor_a_cadence_declare_it_policy_backed` and the five escape-hatch tests in `test_lean_sidecar_template_registry.py` (`:146-226`) scan source and registry text. They stay because each guards a registry claim against a real failure: a companion that would raise inside LEAN, or a phantom exemption.
- `test_indicator_warmup_policy.py::test_configured_warmup_covers_the_largest_catalog_recipe` (`== 2_500`) is a borderline change-detector. It stays because it is the one check run against the real catalog.
- `test_spec_strategy_runner.py` (14 tests). #2704 pointed `run_spec_against_bars[_and_persist]` and `pair_engine_fills` to #2703 as test-only, and #2703 did not list them. Under ☆ they stay: the LEAN-vs-spec parity test `tests/integration/parity/test_ema_crossover_lean_vs_spec.py` reads through `run_spec_against_bars`. Their tests stay with them. See **Proven math, unused**.

## Proven math, unused

Kept, not cut, pending the owner's answer on whether the volatility keep extends to other proven math:

- `app/services/spec_strategy_runner.py`: `run_spec_against_bars`, `run_spec_against_bars_and_persist`, `pair_engine_fills`. Only tests call them. They are proven by the slow LEAN-vs-spec parity test (`tests/integration/parity/test_ema_crossover_lean_vs_spec.py`, which #2729 owns) and unit-tested in `test_spec_strategy_runner.py`. If #2729 cuts that parity test and the keep does not extend, they and the 14 tests become kind 5.

No other unused math in this area is proven against a golden fixture or a parity test. The verdict, portfolio-scenario and LEAN compare and persistence math is all reachable from live routes or services.

## For CI shape (#2738)

- No kept *math* test in this area is slow-marked.
- Two slow-marked files here: `test_run_replay_receipt_end_to_end.py` (whole file; the strongest replay-receipt proof runs only daily) and `test_live_bar_aggregator_soft_loss.py::test_c_baseline_no_gallery_bot_recovers` / `::test_c_with_gallery_bot_still_recovers` (the IBKR feed, which is sacred).

## Pointers outside this list (not rows)

- **#2703** already lists the following, and I skipped them:
  - `test_paper_live_comparison.py`, `test_paper_live_evidence_reader.py` and `test_paper_live_evidence_store.py` (whole files).
  - `test_session_authority.py::{test_order_mechanism_stays_rth_only_while_extended_placement_disabled, test_order_mechanism_without_capability_is_rth_only, test_order_mechanism_admits_only_proven_extended_sessions_when_enabled, test_evaluate_session_submit_branch_table}`.
  - `test_signal_program_admission.py::test_historical_clone_lineage_remains_readable_without_a_clone_writer`.
- **#2706** already lists `test_iv_recorder.py::TestRecorderRoutes::test_series_window_filters` and the series read-back trim of `::test_snapshot_then_read_back`. It also owns the replay-receipt route behind the hazard above.
- **#2728**: `tests/lean_sidecar/test_template_selection.py:63,71`, the same table-restating policy pins as rows 8–15.
- **#2729**: `tests/integration/parity/test_ema_crossover_lean_vs_spec.py` keeps the spec-runner helpers alive (☆).
- **#2724**: `tests/services/bot_runner/test_bot_end.py::test_a_stop_at_its_end_proven_after_a_later_run_began_is_recorded_as_the_stopped_runs_alone` is the behavioural survivor for rows 19–21. Do not cut it as a duplicate of the AST pins.
- **#2738**: the slow files above.
- **#2704**: its pointer on `spec_strategy_runner` is answered above.

## Not reviewed

- **Most bodies were not opened.** I read about 50 of the 734 test bodies. I judged the rest from name, docstring and an assert scan. A test whose name and docstring describe an outcome its asserts do not actually prove would be missed.
- **Cross-layer duplicates were checked only for:** the rows above, the run-replay unit vs end-to-end tests, LEAN source resolution vs the template registry, the two session-authority files, and the IV-recorder route vs service.
- **Cross-layer duplicates not checked:**
  - `test_gallery_hub.py` vs `tests/routers/test_broker_v2_gallery.py`
  - `test_market_liveness.py` vs `test_run_admission.py` and the broker tests
  - `test_options_companion_service.py`, `test_filter_session.py` and `test_forward_fill_gaps.py` vs the dataset router tests
  - `test_lean_sidecar_persistence.py` vs `tests/research/backtest_runs/`
  - `test_source_bar_ledger.py` / `test_startup_join.py` vs the bot-runner tests
  - `test_lean_sidecar_compare_service.py` vs `app/research/parity/qc_reconciler.py`'s tests
- **Parametrize tables** were not expanded case by case. A parametrized test with one trivial case among real ones is judged as a whole.
