# Kill list — clerk SQLite tests, files r–z (#2719)

Part of map #2700. Area: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_[r-z]*.py` (23 files at this SHA, about 20.6K lines, about 570 tests).

**Read at `6a4d7d39`** (`origin/master` on 2026-09-30, the same SHA as the a–e list). Plan, don't cut.

Paths are relative to `PythonDataService/tests/broker/alpaca/clerk/sqlite/` unless they start with `PythonDataService/`, `tests/` (which means `PythonDataService/tests/`) or `docs/`.

## How this was judged

1. An AST pass over every file listed each test: name, line, size, docstring, and its first `assert` / `pytest.raises` lines. Every test was judged against the five kinds from that summary.
2. Each candidate was then read in full, beside the test it might duplicate. Duplicate searches went one layer up and one layer down: executor against facade (`execute_safe_flatten_plan` against the recovery dispatcher), schema against behavior (DDL lists against the constraint tests), constant against the code that derives it (`order_evidence.py:1306`).
3. Skipped: everything #2701 already lists in this area (below), and the raw `runs/stop` route tests #2706 owns.
4. Most of this area is money path: reconciliation verdicts, uncertainty fences, safe flatten, residue discharge, scheduled-end sales, leases, crash recovery, backup and restore. Those tests prove outcomes and stay. The cuts below prove a constant, a constructor argument, a hand copy of the schema or of copy text, or repeat a stronger test.

Kinds used: **1** trivial (asserts what the code literally declares: a constructor storing its argument, a field bound, a fresh store reading empty, a constant derived from the value it is compared to); **2** copy and doc pinning (UI wording, a doc copy of the DDL, a hand-kept list of tables, columns or parameters); **3** duplicate (survivor named); **4** mock theater; **5** retired feature (owned by the dead-code and routes tickets).

## Kill list

| Path | Kind | Evidence |
|---|---|---|
| `test_runtime_live_invariant.py::test_a_version_one_live_facade_needs_no_arming_gate` (25) | 1 | Asserts `budget_authority_version() == 1`, `facade.live_envelope is envelope` and `facade.account_mode == "live"`: the constructor kept its arguments. The docstring's claim (every ENTER refuses `BUDGETS_NOT_SWITCHED_ON`) is not asserted. The refusal is proven by `test_envelope_admission.py::test_an_account_not_switched_to_budgets_refuses_every_enter` (227, `:241`). |
| `test_runtime_live_invariant.py::test_a_paper_facade_is_untouched` (34) | 1 | Asserts a paper facade's `live_envelope is None` and `account_mode == "paper"`: the constructor defaults. The live guard beside it (`test_a_live_facade_refuses_to_exist_without_its_envelope`) stays. |
| `test_v14_simulated_execution_evidence.py::test_a_fresh_authority_admits_simulated_execution_evidence` (197) | 3 | Hand-inserts one `fills` row with foreign keys off to show the CHECK admits `simulated_execution`. Survivor: `test_runtime_shadow.py::test_a_shadow_fill_retains_its_exact_simulated_execution_identity` writes the same value on a fresh authority through the production submission and fold path (`:194` asserts `evidence_source == "simulated_execution"`). |
| `test_schema_parity.py::test_schema_ddl_matches_pinned_contracts_doc` (57) | 2 | Asserts `schema.SCHEMA_DDL` equals the SQL block copied into `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`. It pins a doc copy of code ("code is the documentation"). It proves no custody outcome: the migration and constraint tests in the same file prove what the DDL does. |
| `test_schema_parity.py::test_schema_creates_all_pinned_tables` (62) | 2 | A hand-kept set of every table name. A missing table fails every repository test that reads it first. Its docstring cites `test_holds_is_a_read_only_view_over_the_two_hold_causes`, which does not exist at this SHA. |
| `test_schema_parity.py::test_v9_execution_provenance_and_custody_subject_schema` (106) | 2 | Restates column names, defaults, NOT NULL flags and index names from `PRAGMA table_info`. What those constraints do is proven by `::test_idempotency_indexes_reject_duplicate_rows_at_the_sql_boundary` (642) and `::test_v9_subject_ownership_invariants_reject_counterfeit_and_cross_wired_rows` (361). |
| `test_v12_holds_to_uncertainties.py::test_a_fresh_authority_has_no_holds_table_only_the_view` (88) | 3 | Asserts `sqlite_master` lists `holds` as a view. Survivor: `::test_the_holds_view_refuses_every_write` (99) builds the same fresh schema and expects SQLite's `cannot modify holds` error, which SQLite raises only for a view, so it fails if `holds` were a table. |
| `test_stream_health_sync.py::test_the_loop_waits_its_own_cadence_between_ticks` (314) | 3 | Asserts `waits == [INTERVAL_S] * 3` with healthy ticks. Survivor: `::test_failing_ticks_never_slow_the_cadence` (337) asserts the same fixed interval for four ticks, under the case that matters (every tick failing). |
| `test_stream_health_sync.py::test_the_sync_cannot_observe_the_reconciler_at_all` (369) | 2 | Asserts the exact parameter-name set of `StreamHealthHoldSync.__init__`. That pins a signature, not behavior. The independence it guards is proven by `::test_failing_ticks_never_slow_the_cadence` (337). |
| `test_residue_discharge.py::test_a_blank_operator_reason_is_recorded_as_absent` (200, 3 params) | 1 | Its one assert: `None`, `""` and `"   "` are stored as an absent `operator_reason`, an audit-text normalization. The discharge itself is proven by `::test_flat_broker_discharges_the_residue_and_resolves_the_exit_fences` (103) and `::test_the_panel_dispatcher_discharges_a_drifted_residue_end_to_end` (295). |
| `test_simulated_account.py::test_simulation_baseline_rejects_out_of_domain_timestamps` (50, 3 params) | 1 | Feeds `MAX_TIMESTAMP_MS + 1` into a Pydantic model and expects `ValidationError`. It checks the declared `Field` bound, not behavior. |
| `test_uncertainty.py::test_raise_uncertainty_account_clerk_scope` (167) | 3 | Asserts only `created == "raised"` and that the row exists. Survivor: `::test_admit_new_exposure_blocked_by_account_clerk_uncertainty_blocks_every_bot` (263) raises the same account-scoped episode through the same `_raise` helper and proves it blocks every bot. |
| `test_uncertainty.py::test_raise_uncertainty_bot_scope` (176) | 3 | Same, bot scope. Survivor: `::test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot` (274). |
| `test_uncertainty.py::test_admit_new_exposure_allows_when_nothing_active` (258) | 3 | `allowed is True` on an empty store. Survivor: `::test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot` (274) asserts `unaffected.allowed is True` beside an active episode, which is the stronger case. |
| `test_uncertainty.py::test_require_admission_is_silent_when_allowed` (433) | 1 | No assert: it calls `require_admission` on an empty store. Every ENTER test that submits runs through it. |
| `test_uncertainty.py::test_reason_policy_age_field_is_required_with_no_default` (535) | 1 | Asserts that a dataclass field's `default` and `default_factory` are `MISSING`. It pins a declaration. The registry's real guarantee stays in `::test_every_registered_reason_declares_a_closed_age_policy` (542). |
| `test_uncertainty.py::test_age_policy_is_a_closed_three_shape_sum` (588) | 1 | Asserts `typing.get_args(AgePolicy)` is the three classes: it reads back the type alias. The accessor's named refusal of an undeclared shape stays in `::test_reason_age_policy_rejects_a_shape_the_caller_did_not_declare` (595). |
| `test_uncertainty.py::test_submit_absence_receipt_code_is_the_declared_summary_code` (607) | 1 | A tautology. `order_evidence.SUBMIT_ABSENCE_SUMMARY_CODE` is derived from the policy (`app/broker/alpaca/clerk/sqlite/order_evidence.py:1306`, `= reason_age_policy(...)`), and the test compares it with that same policy's `summary_code`. The receipt itself is proven by `test_crash_restart.py::test_recorded_but_unsent_entry_is_voided_never_sent` (`:237`). |
| `test_recovery_policy.py::test_stop_asks_by_the_bots_own_name_and_says_its_unused_cash_goes_back` (105) | 2 | Asserts the confirmation's `title`, full `explanation` sentence and `confirm_label` strings. |
| `test_recovery_policy.py::test_a_dry_runs_stop_never_says_the_accounts_money_moves` (118) | 2 | Asserts the Dry Run confirmation's `title` and full `explanation` sentence. |
| `test_recovery_policy.py::test_bot_uncertainty_authors_scope_impact_and_next_step` (742) | 2 | Builds an uncertainty with `operator_impact` and `next_step` text, then asserts the guidance echoes that text back, plus `may_create_exposure is False`. That flag is a display field (`app/schemas/alpaca_clerk_sqlite.py:339`); admission does not read it. The blocking itself is proven by `test_uncertainty.py::test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot`. |
| `test_runtime_program_leg.py::test_the_facade_publishes_its_leg_policy` (510, 2 params) | 3 | Asserts the facade's published policy, `exit_bps` stripped. Survivor: `::test_the_one_published_leg_policy_carries_the_envelope_allowances_for_both_sides` (266) pins the same accessor, the same stripping, with a real envelope. The leg each policy produces is proven end to end by `::test_an_extended_decision_outside_the_regular_session_submits_a_marketable_day_limit` (426) and `::test_a_regular_only_authority_refuses_an_extended_decision` (493). |
| `test_safe_flatten_execution.py::test_execute_safe_flatten_plan_reduces_with_the_confirmed_extended_limit` (799) | 3 | Calls the executor directly and asserts a `SELL 10 LIMIT 99.95` extended leg. Survivor, one layer up: `::test_a_pre_market_flatten_sends_the_operators_confirmed_limit` (945) drives the same confirmed `99.95` limit through the facade and asserts the same `LIMIT`/`DAY`/extended leg, plus the durable reference price and slippage. The exact quantity is proven by `::test_execute_safe_flatten_plan_reduces_attributed_exposure_exactly` (288). |
| `test_repository.py::test_active_hold_returns_none_when_no_hold_of_that_reason_exists` (784) | 1 | A fresh store's `active_hold(...)` is `None`. Every raise or resolve test reads the same accessor both ways, e.g. `test_v12_holds_to_uncertainties.py::test_a_live_authority_raises_and_resolves_a_hold_through_the_view`. |
| `test_repository.py::test_uncertain_orders_is_empty_on_a_fresh_repository` (843) | 1 | A fresh store's `uncertain_orders() == []`. `test_two_bots_one_symbol.py::test_an_enter_refused_as_a_wash_trade_fails_and_the_bot_may_enter_again` asserts the same empty list after a real refusal. |
| `test_repository.py::test_active_uncertainty_returns_none_when_none_exists` (937) | 1 | A fresh store's `active_uncertainty(...)` is `None`. `::test_uncertainty_resolved_fold_closes_the_active_row` (964) asserts it non-empty, then `None` after the resolve. |
| `test_reconcile.py::test_plan_is_clean_when_no_foreign_orders_and_positions_match` (402) | 3 | `plan.verdict == "clean"` with exactly matching positions. Survivor: `::test_plan_drift_tolerance_ignores_float_residue_within_epsilon` (514) asserts `clean` for the same pure plan with float residue added, which is the harder case. |

**Owner-ruling check.** None of these rows proves an order, custody, flatten, budget, kill-switch, lease or fencing outcome that its named survivor does not also prove. Every survivor is a test that stays. No row is in `SMOKE_ADVERSARIAL_TESTS` (`app/broker/alpaca/clerk/sqlite/qualification_performance.py:34`).

## Already listed by #2701 (skipped here)

- `test_repository.py::test_process_repository_shutdown_closes_every_cached_handle_after_one_failure` (61) and `::test_raise_uncertainty_if_none_active_is_idempotent` (943).
- `test_reconcile.py::test_external_order_reader_paginates_durable_observations_with_account_scoped_cursor` (865), `::test_external_order_cursor_survives_a_later_broker_snapshot_update` (888), `::test_external_order_reader_filters_review_state_without_cross_filter_cursor_reuse` (936), `::test_raise_uncertainty_if_none_active_serializes_two_concurrent_callers` (1401).
- `test_repository_writer_boundary.py::test_writer_census_detects_decision_final_outcome_updates` (224) and the `update_final_outcome` clause of `_writer_call_name` (134).

None of the five tests #2701's H2 names (a correction used only as setup) is in this area.

## The raw `runs/stop` tests: does a live Stop already prove the end is cancelled?

#2706 cuts the dead `custody_runs_stop` route and lists "5 of 31" `test_scheduled_end.py` tests with it. At this SHA, **12** tests drive the raw route: `test_the_raw_stop_route_*` at 609, 651, 681, 770, 798, 852, 887, 922, 952, 980, plus `test_a_retry_of_the_raw_stop_whose_process_stop_failed_stops_the_process` (1022) and `test_a_raw_stop_naming_the_account_as_the_fleet_does_finishes_a_panel_stop_whose_process_stop_failed` (1053). `test_a_retry_of_the_panels_stop_of_an_earlier_run_leaves_the_running_process_alone` (1101) posts to the **live** `recovery-actions/execute` route (`stop_bot_decisions`), so it stays.

**Headline: yes.** Both live stop paths are proven to cancel the scheduled end:

- Panel Stop (`stop_bot_decisions` → `recovery_execution.operator_stop_run` → `_cancel_bot_end`): `tests/services/bot_runner/test_bot_end.py::test_the_panels_stop_as_the_end_comes_cancels_the_end_before_its_stop_commits` (362). It asserts `pending_ends == []`, a `STOPPED`/`OPERATOR_STOP` duty outcome, and that a restart does not revive the end.
- Lane-wide Stop (`lane_stop_all_bots` → `stop_all_bots_on_lane` → `BotTaskRegistry.stop_every_running_bot`): `tests/services/bot_runner/test_bot_end.py::test_the_lane_wide_stop_cancels_the_end_a_crash_kept` (295) and `::test_the_lane_wide_stop_cancels_the_end_of_an_idle_bot_that_never_stopped` (315).
- The runner's own Stop: `::test_an_operators_stop_ends_the_bot_and_its_end` (253).

So the raw tests at 609 and 770 duplicate test 362, and the one at 980 duplicates `test_scheduled_end.py:1101`. The tests at 651, 681, 798 and 887 check route-only behavior (404s, the route's spelling of the account) and go with the route.

**Edge cases: no.** The panel and the raw route share one function, `operator_stop_run` (`app/broker/alpaca/clerk/sqlite/recovery_execution.py:313`). Four of its behaviors are proven **only** through the raw route:

| Raw-route test | Live behavior of `operator_stop_run` it alone proves |
|---|---|
| 852 (2 params) | A Stop of a crashed current run, already stopped by the sweep or a restart, still cancels the end that run kept. |
| 922 | A Stop landing after the Clerk's own end-time STOP still cancels the end. |
| 952 | A Stop leaves an end sale the Clerk already accepted to go out (owner decision 2026-09-30, #2666). |
| 1022, 1053 | A retry after a failed process stop finishes the process stop, including one that finishes a panel Stop. |

Under ☆ "test-only helpers follow their tests", app code that only tests call stays while a surviving test proves live behavior through it. The raw route is now exactly that. So these five tests, and the route under them, should stay until each one has a panel-path twin. Writing that twin is a rewrite, which is out of scope for the map. **This conflicts with #2706's row for `custody_runs_stop`; see "For the map".**

## What the cuts orphan

- `PythonDataService/app/broker/alpaca/clerk/sqlite/schema.py::load_pinned_ddl` (1069–1086). Its only caller is the cut DDL test (`test_schema_parity.py:58`), so it follows its test (☆). Also `REPO_ROOT` in `test_schema_parity.py:16`.
- The SQL block in `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` §3 loses its only drift check. Drop the block in the same PR, or it goes stale silently. #2712 kept the doc for its code references (`schema.py:4,58,793`, `facts.py:14`, `hashchain.py:6`, `repository.py:5`). See the pointers.
- `test_recovery_policy.py::_stop_confirmation_of` (99): only the two cut wording tests use it.
- Imports left unused (project-scope ruff finds them): `LiveEnvelopeGate` and `TEST_ENVELOPE_VALUES` in `test_runtime_live_invariant.py`; `ValidationError` and `MAX_TIMESTAMP_MS` in `test_simulated_account.py`; `typing`, the `order_evidence` module import and probably `AgePolicy` in `test_uncertainty.py`.
- `PythonDataService/scripts/pr_shard_durations.json` keeps stale ids. That is harmless: `scripts/pytest_shard.py:1-11` falls back to hashing for ids it lacks, and fails only when the file matches no collected test.
- If #2706's raw-stop cut goes ahead anyway: `test_scheduled_end.py::_runner_keeping_the_end` (839) and `::_the_disk_fails_the_first_stopped_write` (1007) become orphans. `_the_raw_route` (741), `_deployed_in_the_runner` (723) and the `runner_duty_clerk` fixture (715) stay, because test 1101 uses them.

## Hazards the cutting PR must carry

- **H1: the raw-stop edge cases.** Do not delete `test_scheduled_end.py` tests 852, 922, 952, 1022 or 1053 with #2706's route cut unless the map rules otherwise (see above). They are the only proof of four live `operator_stop_run` behaviors.
- **H2: doc and test in one commit.** The DDL parity test and the pinned-contracts SQL block go together. `scripts/check_documentation_contract.py` and the docs link-contract tests (`pytest tests/contracts`) must still pass.
- **H3: money-path run.** Every file here is money path. Run `tests/broker/alpaca/clerk/sqlite/` and `tests/services/bot_runner/test_bot_end.py` at the cutting SHA. Re-check each survivor named above still exists there, because kill lists age.

## Kept on purpose

- **`test_repository_writer_boundary.py`** (its three remaining tests, plus `app/.../sqlite/repository_boundary.py`, which #2701 deferred to this ticket). `docs/known-gaps.md:215-233` says plainly that the AST census is a "convention nudge, not a sound safety gate": it matches on spelling, and five kinds of false negative are known. It proves no outcome. It still guards how custody gets written (an unclassified external writer fails CI), and the gates rule keeps checks that guard the money path. When unsure on the money path, keep. Whether AST convention censuses count as that kind of check is a rule-level question; see "For the map".
- **`test_uncertainty.py::test_order_outcome_unknown_declares_the_original_submit_absence_grace` (564) and `::test_exit_not_flat_declares_the_original_redrive_then_escalate` (572).** They pin literal values (a 30 s absence grace; a 120 s re-drive repeated 3 times, then escalate). The behavior tests (`test_reconcile.py:557`, `:568`, `:2618`, `:3286`) read those values from the registry, so these two are the only guard on the values themselves. A shorter grace could void an order that did reach Alpaca. Kept as money path, unsure.
- **`test_uncertainty.py:559` and `:580`** pin that `POSITION_DRIFT`-class causes and `EXIT_STUCK` never auto-void. A clock there would silently lift a fence.
- **`test_runtime_program_leg.py::test_a_facade_built_without_a_policy_is_regular_only` (530):** the only proof that a facade built with no policy sends no extended-hours legs.
- **`test_residue_discharge.py::test_every_registered_reason_declares_its_residue_discharge_role` (369):** a registry pin, but moving a cause into `admits` would let a discharge zero custody under it.
- **`test_stream_health_sync.py::test_a_steady_healthy_account_never_touches_the_ledger` (257):** a call-count spy, but on a real past fleet failure (write-lock contention). Its partner at 271 proves the restart release.
- **Migration and replay tests** in `test_schema_parity.py`, `test_v12_holds_to_uncertainties.py` and `test_v14_simulated_execution_evidence.py`. #2701 keeps every migration step, and #2736 has not yet counted store versions.
- **Every test in** `test_reconcile.py` (except row 402), `test_recovery_tooling.py`, `test_run_ownership.py`, `test_runtime.py`, `test_runtime_shadow.py`, `test_stopped_run_entries.py`, `test_two_bots_one_symbol.py` and `test_unfilled_extended_orders.py`. Each proves a reconciliation, fence, lease, cancel, flatten, restore or custody outcome. Close pairs that test different routes (REST against websocket, virtiofs against fuseblk, `@162` against `@933` in unfilled orders) were left alone.

## For the map

1. **Conflict with #2706.** Under ☆ "test-only helpers follow their tests", the raw `runs/stop` route is app code only tests call, and five of its tests prove live `operator_stop_run` behavior no other test proves. Recommendation: keep `custody_runs_stop` and those five tests (852, 922, 952, 1022, 1053) until panel-path twins exist. Cut the other seven raw-route tests with the route's OpenAPI and catalog entries only once those five are re-homed. Also, #2706's "5 of 31" count is really 12.
2. **Rule-level owner question.** Does a structural test that enforces a coding convention (the AST writer census; similar censuses elsewhere) count as a gate that "guards money-path" and stays, or as a test that must prove an outcome? Options: (a) keep it as a money-path guard (recommended: it is cheap, and its false positives fail loudly); (b) cut it, because known-gaps says it proves nothing sound.
3. **Pointer to #2712 / #2741.** If the DDL parity test goes, `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` should lose its SQL block in the same PR, and the `schema.py` comments citing "§3" get re-pointed or dropped (#2742).

## Pointers: cuttable things outside this area

- **#2706 (routes):** see "For the map" item 1.
- **#2741 / #2712 (architecture docs):** the pinned-contracts doc's SQL block (above). `docs/known-gaps.md:215-233` describes the writer census; it stays true while the census stays.
- **#2742 (comment citations):** `app/broker/alpaca/clerk/sqlite/schema.py:4,58,793` cite the pinned-contracts doc §3 for the DDL.
- **#2724 (bot runner tests):** the live-path survivors named above are in `tests/services/bot_runner/test_bot_end.py`. That ticket must not cut 253, 295, 315 or 362 without re-homing the "a Stop cancels the end" proof.

## Not reviewed

- **Full bodies of the long scenario tests.** Tests longer than about 40 lines in `test_reconcile.py`, `test_two_bots_one_symbol.py`, `test_unfilled_extended_orders.py`, `test_runtime.py`, `test_safe_flatten_execution.py` and `test_scheduled_end.py` were judged from their name, docstring and first two or three asserts. Only the candidates above were read in full. Two such scenario tests that repeat each other's setup and outcome could be missed.
- **Wording asserts inside kept tests**, for example the `next_step` string in `test_recovery_policy.py:167` or the explanation substring in `test_scheduled_end.py:398`. The rule keeps a test whose outcome is real and allows no rewrites, so those lines were not listed.
- **`conftest.py` and shared helpers** (`tests/broker/alpaca/clerk/live_envelope_fixtures.py`, `test_live_envelope_sync._Read`): the a–e ticket owns `conftest.py`. Nothing here was checked for fixtures that only cut tests use, beyond the orphans listed.
- **Coverage below the sqlite directory.** Survivors were looked for in this directory, in `tests/services/bot_runner/test_bot_end.py` and in `test_crash_restart.py`. Other directories (`tests/broker/v2panel/`, `tests/routers/`) were not searched for stronger duplicates, so this list may under-cut.
