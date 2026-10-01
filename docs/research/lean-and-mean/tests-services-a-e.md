# Kill list — service tests a–e and bot runner tests (#2724)

Part of map #2700. Read at **`6a4d7d396108ef16471d8df888b9ded74d3c2892`** (`origin/master` on 2026-09-30, when this branch was cut). The map was charted at `87b8e261`.

Paths in the tables are relative to `PythonDataService/tests/services/`. Other paths are relative to `PythonDataService/` unless they start with `docs/` or `Frontend/`.

**Area.** 48 files `test_[a-e]*.py` and the `bot_runner/` package (22 test modules plus `conftest.py` and `_support.py`), about 700 tests.

## How this was judged

- **Every test was read against the bar.** I extracted each test's name, docstring and assertions with an AST script and read them all. I opened the source of every candidate before listing it.
- **Duplicates.** I hashed normalized test bodies and assertion sets across the whole `tests/` tree and `app/engine/tests/` to find copy-paste twins. Then I looked one layer up (router or job) and one layer down (service or leaf function) for each candidate. Each kind 3 row names the test that survives.
- **Blocking lists skipped.** I skipped everything the dead-code lists already own:
  - #2703 (`dead-services.md`, with its owner-ruling addendum): `test_bar_persistence.py` compact, parquet and retention tests; `test_bar_timestamp_rigor.py::test_bar_provenance_fields_survive_persistence_round_trip` (parquet leg); `test_bs_greeks.py::TestBsEuropeanVega`.
  - #2706 (`dead-routes.md`): `test_broker_order_groups.py` (whole file); the session fee-reconciliation tests in `test_alpaca_fee_reconciliation.py`; `test_dataset_export_columns.py::test_generate_csv_unknown_column_is_422_without_fetching`.
  - #2704 (`dead-engine.md`): the `rollback_blocked_*` assertions in `test_bot_trade_strategy_discard.py`.
- **Rulings applied.**
  - The ★ and ☆ rulings in the map's Notes.
  - The owner's volatility-math ruling of 2026-09-30: validated volatility and options-Greeks math stays, with its tests, even when nothing uses it. `TestBsEuropeanVega` therefore **stays**. This overrides #2703 rows 17–18 (`bs_european_vega` and its 4 tests).
- **On the money path, when unsure, I kept the test.** Each such case is under **Considered and kept**.

## Kill list

Kinds: 1 trivial · 2 copy or doc pinning · 3 duplicate · 4 mock theater · 5 retired feature.

### `tests/services/test_[a-e]*.py`

| # | Item | Kind | Evidence |
|---|---|---|---|
| 1 | `test_alpaca_live_verdict.py::test_every_verdict_carries_server_authored_copy` (`:110`) | 3 | Each parametrized `final` is already pinned with full facts by `::test_unconfigured_settings_is_unknown`, `::test_paper_settings_is_paper_regardless_of_clerk_state` and `::test_live_with_refused_clerk_is_live_unarmed_and_names_the_account` (same `LIVE_ACCOUNT_REFUSED` runtime). The only extra assertion is `verdict.headline and verdict.detail`, which checks that the copy is non-empty. |
| 2 | `test_bar_timestamp_rigor.py::test_dual_field_window_is_exact_for_resolution` (`:48`) | 1 | The test's own `_bar` helper sets `end_ms = start_ms + window_ms`, and the test asserts exactly that difference. `IbkrMinuteBar` (`app/broker/ibkr/bar_models.py`) has no validator, so no product code runs. The round-trip tests in the same file (`:69`, `:123`) cover the real invariant. |
| 3 | `test_bar_timestamp_rigor.py::test_start_ms_is_aligned_to_window` (`:59`) | 1 | Asserts `start_ms % window_ms == 0` for a start the test chose aligned. No producer is exercised. `::test_monotonic_sequence_preserves_window_for_every_bar` asserts alignment after a real persistence round-trip. |
| 4 | `test_bot_decision_quarantine.py::test_a_journal_with_no_sink_still_counts_and_logs` (`:156`) | 1 | Contains no assertion. Its name promises "counts and logs", but it checks neither. It proves only that two `_record` calls do not raise. |
| 5 | `test_bot_end.py::test_bot_end_view_of_a_later_day_that_keeps` (`:320`) | 2 | Asserts only `view.headline` and `view.explanation` strings. The keep/sell rule itself is proven by the `resolve_bot_end` tests and by `bot_runner/test_bot_end.py`. |
| 6 | `test_bot_end.py::test_bot_end_view_of_a_dry_run_names_the_last_price` (`:331`) | 2 | Asserts only the headline and explanation sentences. |
| 7 | `test_bot_end.py::test_bot_end_view_of_an_ended_dry_run` (`:400`) | 2 | Asserts only the headline and explanation sentences. |
| 8 | `test_bot_end.py::test_bot_end_view_carries_the_clamp_notice` (`:409`) | 1 | Passes `notice="moved"` into `bot_end_view` and asserts `view.notice == "moved"`: a pass-through. The real clamp notice is proven by `::test_resolve_bot_end_clamps_a_clock_time_after_an_early_close_and_says_so`. |
| 9 | `test_bot_run_evidence_preserve_terminal.py::test_record_outcome_rejects_a_genuinely_different_receipt` (`:219`) | 3 | Records one outcome, then a conflicting one, and expects `RunOutcomeConflictError`. `test_bot_binding_repository.py::test_terminal_outcome_is_create_once_and_run_scoped` (`:319`) proves the same conflict, and also the idempotent re-record and the read-back. |
| 10 | `test_bot_start_admission.py::test_degraded_boot_report_names_a_few_bots_and_counts_the_rest` (`:995`) | 2 | Pins how the list of names is shortened in the explanation (`"7 bot(s)"`, `"and 2 more"`, `"bot-5" not in`). It is formatting only. The refusal itself is proven by `::test_degraded_boot_report_replaces_the_wait_remedy`. |
| 11 | `test_bot_trade_strategy_extended_bars.py` (whole file, 4 tests) | 3 | Despite its name, every test drives `RunDecisionSession` directly, with the same `04:00–20:00` window as `test_decision_session.py`. Survivors in `test_decision_session.py`: `::test_includes_compares_a_bars_open_against_the_declared_bounds` (same boundary table), `::test_resolve_returns_none_for_an_extended_run_with_no_declared_window` with `::test_an_incoherent_session_cannot_be_constructed`, `::test_includes_reads_the_feed_label_for_a_regular_session_run` (identical asserts), and `::test_includes_is_false_on_a_non_trading_day`. |
| 12 | `test_chart_range_presets.py::test_presets_cover_the_full_key_table` (`:50`) | 3 | Asserts that the output keys equal `RANGE_PRESETS`. `::test_session_counts_follow_the_preset_table` (`:81`) indexes every key of the same table and checks its computed `session_count = len(window)` (`app/services/chart_service.py:385`). |
| 13 | `test_dataset_export_columns.py::test_select_output_columns_none_keeps_full_projection` (`:80`) | 3 | `::test_zip_bundle_default_request_exports_every_projected_column` (`:272`) proves the same "no selection means every column" through the real bundle. |
| 14 | `test_dataset_export_columns.py::test_select_output_columns_unknown_name_fails_loudly` (`:92`) | 3 | Two layers down from `::test_dataset_zip_job_rejects_unknown_column_before_queueing` (`:216`). That test goes through the real `jobs.start_dataset_zip_job` → `prepare_generation_request` → `select_output_columns` (`app/routers/jobs.py:408`, `app/services/dataset_plan_service.py:336`) with the same `ema_20`, and also proves that nothing is queued. |
| 15 | `test_dataset_export_columns.py::test_prepare_generation_request_rejects_a_column_the_recipe_cannot_produce` (`:193`) | 3 | One layer down from the same job test (row 14), with the same input and the same `ValueError` text. |
| 16 | `test_dataset_export_columns.py::test_prepare_generation_request_accepts_a_planned_selection` (`:175`) | 3 | `::test_plan_lists_a_long_window_indicator_the_real_run_produces` (`:180`) ends with the same accept assertion on a harder recipe. `::test_zip_bundle_honors_column_selection_and_time_zone` proves a selection end to end. |
| 17 | `test_deploy_window.py::test_start_window_next_step_while_the_window_is_open` (`:60`) | 2 | Asserts one fixed sentence (`"Start is allowed in the current session."`). |
| 18 | `test_deploy_window.py::test_exit_steps_summary_names_each_step_in_the_owners_et_words` (`:66`) | 2 | Pins a five-sentence summary string verbatim. The exit behaviour it describes is owned by the overnight-exit tests. The ET wording comes from `app.utils.et_words`. |
| 19 | `test_dry_run_boot_isolation.py::test_a_lease_held_dry_run_panel_shows_only_the_owner_sentence` (`:623`) | 2 | Same setup and same `PanelUnavailableError` refusal as `::test_the_unrestored_dry_runs_panel_says_its_account_did_not_open` (`:281`), which also proves the 503. This test adds only phrase pins (`"Stop that copy"`, no `"sim:"`, no `"execution lease"`). |

### `tests/services/bot_runner/`

| # | Item | Kind | Evidence |
|---|---|---|---|
| 20 | `bot_runner/test_bot_end.py::test_a_stop_goes_on_when_the_end_it_cancels_cannot_be_read` (`:466`) | 3 | The only assertion is the log text `"clear its end when repairing the file"`. The outcome it names, "a stop goes on", is proven more strongly by `::test_a_stop_of_a_running_bot_whose_desired_state_cannot_be_read_still_stops_it` (`:483`): the run gate closes and the bot stops. |
| 21 | `bot_runner/test_deploy_only_lifecycle.py::test_registry_has_no_alternate_restart_or_pause_capability` (`:53`) | 1 | A tombstone: `not hasattr(BotTaskRegistry, …)` for five removed method names. None of the names exists anywhere in `app/`. A caller can't reach a removed method, and `::test_removed_action_cannot_cross_the_command_boundary` proves the API refuses it. |
| 22 | `bot_runner/test_deploy_only_lifecycle.py::test_registry_has_no_restart_intensity_state` (`:58`) | 1 | A tombstone over constructor parameters, private attribute names and a Literal member. Its own docstring calls the removed throttle "a check that cannot fire". |
| 23 | `bot_runner/test_registry_lifecycle.py::test_deployed_bot_consumes_bars_and_logs_decisions` (`:460`) | 3 | `test_trade_bot_ema_and_mechanics.py::test_log_only_bot_unchanged_after_trade_mode_added` (`:272`) runs the same feed and deploy (`mode` defaults to `"log_only"`, `app/services/bot_runner.py:623`). It asserts the same two `HOLD` decisions plus the persisted mode. |
| 24 | `bot_runner/test_registry_lifecycle.py::test_desired_state_reports_durable_intent` (`:598`) | 3 | `::test_deploy_produces_running_task_and_durable_on_duty_evidence` asserts `desired_state == "RUNNING"` in the view and in the raw file. `::test_stop_writes_durable_intent_and_off_duty_evidence` (`:540`) does the same for `STOPPED`. |
| 25 | `bot_runner/test_registry_lifecycle.py::test_runner_fixture_clock_advances_through_startup_settle_and_timeout` (`:1076`) | 3 | Drives `StartupDeadline.run` to its deadline under the package's patched clock. The deadline behaviour is owned by `tests/services/test_startup_join.py::test_an_attempt_still_in_flight_at_the_deadline_is_cancelled_not_waited_on` and `::test_the_deadline_is_fixed_once_and_retries_never_extend_it`. Nothing of the runner is exercised. |
| 26 | `bot_runner/test_registry_shutdown_ordering.py::test_runner_is_daemon_free_by_construction` (`:234`) | 1 | An AST import scan for `host_daemon`, `daemon_client`, `daemon_transport`, `subprocess` and `multiprocessing` in two modules. The host daemon was deleted (#2712 cuts its ADRs). No module with those daemon names exists in the repo (`find` is empty), so this guards a retired design rather than an outcome. |

## Retired-feature tests the dead-code lists missed (kind 5, for the owning ticket)

The dead-code tickets own kind 5, so these are pointers, not rows. Each needs its owner to confirm and to cut the app code with it.

- **`test_alpaca_fee_reconciliation.py` is all dead, not "6 of 24".** #2706 (`dead-routes.md:207`) counts 6 of 24 tests. In fact every one of the 24 calls `reconcile_session_fees`, either directly or through the helpers `_reconcile` (`:80`) and `_unpinned_2025` (`:228`), or calls `session_fee_reconciliation`. Both functions' only production caller is the route `GET /api/brokers/{broker}/fees/session-reconciliation` (`app/routers/brokers.py:409-436`), which #2706 cuts. The whole file goes with that path. **Owner: #2703 / #2706.**
- **`test_chart_split_read.py::test_the_adjusted_prices_notice_stays_in_the_contract_though_nothing_emits_it`** (`:857`). Its own docstring says nothing has produced `price_adjustment_unsupported` since #1839. The test hand-builds the span to keep the unproduced branch covered. The branch is the `price_adjustment_unsupported` → `adjusted_prices_provider_only` mapping in `app/services/chart_bar_source.py` and the matching case in `Frontend/src/app/components/data-lab/data-lab-chart/data-lab-chart.component.ts` (plus its spec). No list names either. **Owner: #2703 (Python mapping) and #2709 (Frontend case), in one cross-stack PR.**
- **`test_alpaca_sqlite_synthetic_drills.py`** (10 tests). `app/services/alpaca_sqlite_synthetic_drills.py` is imported only by `app/broker/alpaca/clerk/sqlite/qualification_synthetic_rehearsal.py` (and its sibling drill modules). #2707 lists that module as part of the dead synthetic-rehearsal island (`dead-scripts.md:62-65`). #2703 said these modules "join this list" if #2707 cuts the rehearsal path, and #2707 does. **Owner: #2703 / #2707.**
- **`test_alpaca_bot_identity.py`, the 9 historical-ledger tests** (`:81`–`:177`: `test_legacy_ibkr_run_ledger_conflicts_with_sqlite_authority` through `test_symlinked_historical_run_directory_fails_closed`). They exercise `_legacy_ibkr_run_dir` (`app/services/alpaca_bot_identity.py:19`) and `app/engine/live/historical_run_identity.py`. That code reads IBKR-era run ledgers under `artifacts/live_runs/`, which is what the ☆ ruling calls dead ("readers of old IBKR-era files on disk"). No dead-code list names either symbol. This is the money path (bot identity refusal), so the owner ticket must also check that no live host still has IBKR-era run directories it would mistake. The five SQLite-authority tests at `:57`–`:74` and `:179` stay. **Owner: #2703 (services) / #2704 (engine/live).**

## Considered and kept

- **Options-Greeks math (owner ruling, 2026-09-30).** These would be kind 3 but stay under the ruling:
  - `test_bs_greeks.py::TestDividendYield::test_put_call_parity_holds_with_nonzero_dividend` repeats the three `TestPutCallParity` identities, which already use `q = 0.02`.
  - `test_bs_greeks.py::TestBsEuropeanPrice::test_atm_call_positive` and `::test_atm_put_positive` are weaker than `::test_agrees_with_inline_reference`.
  - `TestBsEuropeanVega` also stays. #2703's row 18 must be dropped from its handoff.
- **`test_bs_cross_engine_parity.py::test_fixture_grid_size_is_pinned`.** It looks trivial, but it is the only thing stopping the golden-fixture parity test from passing vacuously on an emptied `cases.json` grid.
- **Ordering tests on the fencing path. These raise a rule question, below.** Each one asserts only the order a fake recorded. Under ★ ("checks mock call order still goes") they would be cut, but the order *is* the fencing property, and the end state would look the same either way. On the money path, when unsure, keep. The tests:
  - `bot_runner/test_registry_shutdown_ordering.py::test_trade_run_registration_precedes_order_capable_task_creation`
  - `bot_runner/test_registry_shutdown_ordering.py::test_stop_commits_clerk_stop_before_task_cancellation`
  - `bot_runner/test_registry_shutdown_ordering.py::test_stop_all_commits_each_trade_run_before_task_cancellation`
  - `test_boot_recovery.py::test_boot_recovers_sqlite_before_reading_file_projection`, which asserts the event list `recover, reconcile, authority_projection ×2`.
- **Remedy wording that is a safety behaviour.** `test_bot_start_admission.py::test_a_degraded_boot_report_while_the_account_reconnects_says_wait_not_restart`, `::test_no_boot_report_while_the_account_reconnects_still_says_the_reconnect_story` and `::test_a_failed_boot_sweep_says_restart_unless_a_reconnect_reruns_it`. The words tell the operator whether to restart. A restart in the wrong state crash-loops the Dry Run lease (#2582), so the sentence is the guard.
- **Refusal tests that pin their words.** In `test_bot_end.py` the `test_resolve_bot_end_refuses_*` tests, and in `bot_runner/test_bot_end.py` the refused edits. Each proves the refusal itself, and only also pins its words. Surviving tests are not rewritten.
- **`test_alpaca_fee_attribution.py::test_incomplete_activity_coverage_names_no_missing_control`.** Its assertions are sentences, but it is the only test of a real account's fee day read with `activities_complete=False`. The other call at `:275` is the simulation path.
- **`test_bot_binding_repository.py::test_the_frozen_replay_and_the_live_proof_offer_the_same_drift_remedy`.** A copy-identity check, but it is the only test that a frozen `DRIFTED` run shows a remedy at all.
- **`test_bot_decision_quarantine.py::test_the_sink_protocol_still_matches_the_journal_it_stands_for`.** Structural, but every other quarantine test uses a fake sink. This is the one check that ties the Protocol to the real `SqliteDecisionReceipts.append`.
- **`test_canary_admission.py::test_canary_pairing_admitted_is_false_against_the_empty_shipped_allowlist`.** It overlaps `::test_canary_allowlist_ships_empty` plus `::test_canary_pairing_admitted_matches_only_the_exact_tuple`, but it covers the default path of an order-admission gate. Kept as money path.
- **`test_dry_run_boot_isolation.py::test_an_unbound_dry_run_can_only_be_read_or_restored`.** It pins the orphan type's public surface. That surface is the fence that keeps an unbound Dry Run from being started.
- **Legacy on-disk readers on the money path**, kept as #2703 kept them:
  - `test_bot_binding_repository.py::test_legacy_binding_is_lifted_without_rewrite_then_migrated_on_resume`
  - `test_bot_binding_repository.py::test_read_lifts_legacy_binding_when_current_run_evidence_is_missing`
  - `bot_runner/test_dry_run_activity.py::test_dry_run_activity_journal_lifts_legacy_authority_fields`
  - `bot_runner/test_registry_admission.py::test_version_one_alpaca_binding_is_read_without_rewriting_audit_artifact`

  These read Alpaca-era files, not IBKR-era ones, as far as the code shows. If the owner ticket finds the `launch_reason="legacy"` binding files are IBKR-era, the ☆ ruling moves those tests to kind 5.
- **Everything else in the area** proves an order, custody, flatten, budget, lease or fencing outcome; a fail-closed refusal; a calendar or timestamp rule through real code; or a golden-fixture or reference parity. Examples: `test_boot_recovery.py`, `test_bot_lifecycle_projection.py`, `test_canary_*`, `test_candidate_uncaptured_at_crash.py`, `test_chart_split_read.py`, `test_data_lab_*`, `test_decision_clock.py`, `test_engine_*`, `test_alpaca_fee_attribution.py`, and most of `bot_runner/`.

## Proven math, unused

None in this area besides the volatility math the 2026-09-30 ruling already keeps (`bs_european_vega` and its tests).

I checked the reference-proven functions the area tests. Each has a live caller:

| Function | Live caller |
|---|---|
| `bs_european_price` | the golden-fixture options tests |
| `quantlib_pricer.price_option` | `app/routers/quantlib_options.py` |
| `apportion_cents` | `app/broker/alpaca/clerk/money.py` |
| `nist_r6_percentile` | `qualification_performance.py`, on the CI smoke qualification path |
| `floor_to_period_ms_et` | `decision_clock.py` |
| `reconcile_broker_curve_to_local_pnl` | the `/{broker}/portfolio-history-proof` route, which is in the fleet operation catalog |

The session fee-reconciliation math above is dead, but no reference proves it: there is no golden fixture and no parity test. So it is listed under kind 5, not here.

## For CI shape

From `scripts/pr_shard_durations.json`. None of these is a reason to cut a test.

| File | Recorded time | Note |
|---|---|---|
| `bot_runner/test_trade_bot_last_bar_enter.py` | 13.2 s (two cases, about 6.6 s each) | The slowest file in the area. |
| `test_data_lab_chart_indicator_warmup.py` | 11.9 s | Chart-vs-export warm-up parity, so it is math. |
| `test_dry_run_boot_isolation.py` | 8.2 s | It waits on leases at production cadence. |
| `bot_runner/test_dry_run_activity.py` | 7.3 s | |

The whole area records about 73 s.

## What the cuts orphan

- **Unused imports for ruff:**
  - `bot_runner/test_deploy_only_lifecycle.py`: `inspect`, `typing.get_args` and `StartRuntimeAdmissionFact` (`:3`, `:5`, `:15`), used only by rows 21–22.
  - `test_bot_run_evidence_preserve_terminal.py`: `RunOutcomeConflictError`, used only by row 9.
  - `bot_runner/test_registry_shutdown_ordering.py` (row 26): its imports are local to the test, so nothing is orphaned at module level.
- **No conftest fixture or `_support.py` helper loses its last user.** `BotTaskRegistry.desired_state` (`app/services/bot_runner.py:2799`) is called only from tests. It stays because surviving tests still read through it: `bot_runner/test_registry_lifecycle.py:594` and `bot_runner/test_duty_settle.py:183`. That is the ☆ rule that test-only helpers follow their tests.
- **A stale docstring:** `bot_runner/conftest.py:10` lists "daemon-free by construction" among the package's criteria. Drop that line with row 26.
- **`scripts/pr_shard_durations.json`:** the node IDs of every killed test.
- **No golden fixture or attribution file is involved** in any row.

## Hazards the cutting PR must carry

- **Re-check at your own SHA.** Re-run each row's survivor check. A duplicate row is valid only while its named survivor still exists and still asserts the same thing.
- **Do not rewrite surviving tests.** Row 11 removes a whole file. Rows 13–16 remove four of five early-validation tests in one file. The survivors are untouched.
- **Rows 23 and 24** live in a file the shared runner helpers also serve. Run all of `tests/services/bot_runner/` after the cut, not just the edited file.
- **Row 20 and `bot_runner/test_bot_end.py`.** The map's existing hazard about `test_scheduled_end.py` raw-stop tests is unrelated. This file's stop-cancels-end tests (`:253`–`:523`) are the live stop paths that hazard asks for, and they all stay.
- **Kind 5 pointers.** The fee-reconciliation file rides the #2706 route cut, so it merges with or after that PR. The adjusted-prices notice is a cross-stack cut: the Python mapping and the Frontend switch case go in one PR. The IBKR historical-ledger reader needs a live-host directory check first.

## Pointers outside this area (not rows)

- **#2725 (service tests f–z):** `tests/services/test_startup_join.py` is the survivor named in row 25. Keep it.
- **#2717–#2719 (clerk SQLite tests):** `tests/broker/alpaca/clerk/sqlite/test_runtime_program_leg.py::test_an_extended_decision_inside_the_regular_session_submits_a_market_day_leg` asserts the same leg as `bot_runner/test_trade_bot_decision_anchor.py::test_an_rth_decision_on_an_extended_run_submits_a_market_leg`. I kept the runner one: it is the regression for the anchor the runner failed to pass. Whether the clerk-level one is a duplicate is that ticket's call.
- **#2703 / #2706 / #2704 / #2707 / #2709:** the four kind 5 items above.
- **#2742 / #2743 (comment citations):** several module docstrings in this area cite docs. `test_bar_timestamp_rigor.py` cites `docs/audits/bar-timestamp-rigor-2026-06-12.md`, which #2711 cuts. `test_candidate_uncaptured_at_crash.py` cites `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`. Those tickets judge them.

## Rule question for the map

**Are ordering tests on the fencing path "call-order checks" (cut) or fencing outcomes (keep)?** The four tests under "Considered and kept" assert only the order of fake callbacks, for example "durable STOP committed before the task was cancelled". The order is the safety property, and the end state is the same either way. My recommendation: keep them, because a fence is an ordering. Options:

1. Keep them as fencing outcomes (recommended).
2. Cut them under ★.
3. Keep them only where no end-state test exists.

## Not reviewed

- **Fixture-usage check deferred.** I did not run the suite or a fixture-usage grep after simulated deletions. The orphan list above comes from reading the imports. The cutting PR runs `ruff check PythonDataService/app/ PythonDataService/tests/` and the edited suites.
- **Layer-up duplicates in other areas.** I compared against router and clerk tests only where a candidate pointed there. A test in this area might be fully covered by a router test in #2730's area without my noticing.
- **The 0-assert tests that delegate to a helper.** These are the warm-up coverage tests, `bot_runner/test_failed_enter_late_fill_2348.py` and three `test_data_lab_chart_indicator_warmup.py` tests. I confirmed that each helper asserts, but I did not judge the helpers themselves.
- **`bot_runner/_support.py` and `tests/_helpers/bot_runner/`.** I read them only for the orphan check, not as tests.
