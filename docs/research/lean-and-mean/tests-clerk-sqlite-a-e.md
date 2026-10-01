# Kill list — clerk SQLite tests, files a–e (#2717)

Part of map #2700. Area: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_[a-e]*.py` (34 files at this SHA, about 20.1K lines, about 510 tests) and the `conftest.py` / `cutover_test_support.py` they use.

**Read at `6a4d7d39`** (`origin/master` on 2026-09-30). Plan, don't cut.

Paths are relative to `PythonDataService/tests/broker/alpaca/clerk/sqlite/` unless they start with `PythonDataService/` or `tests/` (which means `PythonDataService/tests/`).

## How this was judged

1. An AST pass over every file listed each test: name, line, size, docstring, and its `assert` / `pytest.raises` lines. Every test was judged against the five kinds from that summary.
2. Each candidate was then read in full, with the test it might duplicate. Duplicate searches went one layer up and one layer down: the CLI against the service (`test_cutover_cli.py` against `test_dev_reset.py`), the route against the domain object (`tests/broker_configuration/test_account_risk_routes.py` against `test_account_risk_policy.py`), and production callers of each read (`app/`).
3. Most of this area is money path: ENTER/EXIT custody, budgets and reservations, leases, operation claims, crash recovery, flatten, cutover and reset fencing. Those tests prove outcomes and stay. The cuts below are the few that prove a constant, a call count or a query shape, or repeat a stronger test.

## Kill list

| Path | Kind | Evidence |
|---|---|---|
| `test_corrective_foundation.py::test_schema_version_includes_the_durable_cash_reservations` (67) | 1 trivial | Its only assert is `schema.SCHEMA_VERSION == 22`: a constant, edited by hand on every schema bump. If the version were wrong, the open and migration tests in the same file would fail first (`test_stale_schema_version_fails_closed_on_open`, `test_future_schema_version_fails_closed_on_open`). |
| `test_corrective_foundation.py::test_reserve_command_and_serialized_no_longer_exist` (338) | 1 trivial | It asserts `not hasattr(ClerkSqliteRepository, "reserve_command")` and the same for `"serialized"`. It proves two deleted names stay deleted and checks no behavior. The behavior it describes ("a command first becomes durable only via `commit_first_transition()`") is proven by `test_full_command_lifecycle_rebuild_parity` and `test_commands.py::test_concurrent_duplicate_starts_produce_exactly_one_effect`. |
| `test_envelope_reservations.py::test_a_fresh_authority_has_the_reservations_table_at_schema_v13` (207) | 1 trivial | It asserts `schema.SCHEMA_VERSION >= 20`, that the opened store reports that constant, and that `envelope_reservations` appears in `sqlite_master`. Every reservation test in the file reads that table and would fail first, e.g. `test_accepting_an_enter_records_its_price_and_fee_provision_in_the_same_commit`. The migration that adds the table is proven by `test_a_v12_authority_migrates_additively_to_v13`. |
| `test_commands.py::test_get_unknown_command_returns_none` (223) | 1 trivial | One line: `repo.get_command("cmd:does-not-exist") is None`. Where the None matters in production, the stuck-EXIT watchdog's re-drive count (`app/broker/alpaca/clerk/sqlite/exit_watchdog.py:203`), it is proven through the watchdog: `test_reconcile.py:2647` and `:2823` assert an unissued re-drive command reads `None`. |
| `test_bot_results.py::test_a_poll_at_an_unchanged_custody_revision_reuses_the_last_results` (101) | 4 mock theater | It wraps `fee_evidence.custody_fee_attribution` in a counter and asserts the counts 1, 2 and 3. Its only other assert, `again == first`, shows that the same input gives the same answer. That is a query-cost check, not an outcome. The result values are proven by `test_a_finished_bots_result_is_what_its_balance_gained_over_its_budget` and `test_results_read_on_their_own_snapshot_match_the_writers`. |
| `test_budget_commands.py::test_a_money_read_never_searches_the_journal_for_a_stops_release` (251) | 4 mock theater | It traces the SQL statements a budget read runs and asserts that none of them names `custody_transitions`: a query-shape check. Its one outcome assert, the stopped bot's `ReleaseAtStop`, is proven more strongly by `test_stop_retains_order_claim_and_mirror_rebuild_keeps_it`. That test checks `expected.deployments[0].release == ReleaseAtStop(39_999, 60_001)`, and checks it again after a mirror rebuild. |
| `test_account_risk_policy.py::test_policy_accepts_the_canonical_helper_s_binary_noise_tolerance` (34) | 3 duplicate | The same value, one layer up: `tests/broker_configuration/test_account_risk_routes.py::test_canonical_binary_noise_loss_cap_is_accepted_at_the_boundary` posts `loss_usd: 99.89999999999999` to the apply route. That route builds `AccountRiskPolicy` (`app/broker_configuration/account_risk.py`) and returns 200. The rejecting side stays in `test_policy_rejects_a_loss_cap_that_is_not_whole_cents`. |
| `test_cutover_live.py::test_an_unknown_mode_is_refused_by_name` (145) | 3 duplicate | Survivor: `test_cutover.py::test_initialize_refuses_unknown_broker_evidence_mode`. It makes the same `CutoverRefused, match="paper or live"` call on `account_mode="sandbox"`, and also asserts that no `clerk.db` was created. |
| `test_cutover_live.py::test_a_never_legacy_never_rehearsed_account_initializes` (70, both params) | 3 duplicate | Survivor: `test_cutover_live.py::test_a_never_legacy_account_graduates_end_to_end`. It runs the same `_initialize` on the same two evidence params (live and paper), then plan and apply, and asserts the activation. An empty legacy set and an empty roster at initialize are also proven by `test_cutover.py::test_initialize_succeeds_with_no_legacy_artifacts` and `::test_initialize_succeeds_with_empty_runner_roster`. |
| `test_cutover_cli.py::test_read_cutover_evidence_refuses_live_evidence_under_non_live_effective_mode` (259) | 3 duplicate | It patches `effective_alpaca_settings` by name. The next test's docstring says so itself: the patched version "pins the wiring but not what" the resolver returns. Survivor: `test_read_cutover_evidence_reads_the_real_effective_revision` proves the same refusal under paper and acceptance under live against a real profiles database. The patched test adds only a message-text assert (`"effective broker configuration"`). |
| `test_cutover_cli.py::test_dev_reset_cli_refuses_unactivated_legacy_authority` (446) | 3 duplicate | Survivor: `test_dev_reset.py::test_reset_refuses_unactivated_legacy_authority_without_moving_it`. Both raise the same `DeveloperCleanSlateResetRefused, match="requires an established…"`. The survivor checks that every legacy artifact stays (inbox, journal, receipts, `bots/`, runner state); the CLI test checks the journal and the quarantine directory only. |
| `test_dev_reset.py::test_reset_refuses_non_paper_account_without_moving_authority` (383) | 3 duplicate | Survivor: `test_cutover_cli.py::test_dev_reset_cli_refuses_live_mode_without_moving_authority`. It is the same `"only for paper"` refusal, through the operator's real entry point, and it asserts that the journal and runner-registry bytes are unchanged and that neither root has a quarantine directory. This test asserts only that the journal file exists. |

**Owner-ruling check.** None of these rows proves an order, custody, flatten, budget, kill-switch, lease or fencing outcome that its named survivor does not also prove. Every survivor is a test that stays.

## Hazard H2 from #2701, answered for this area

#2701 left five tests that use the dead `EXECUTION_CORRECTED` writer only as setup. It asked the test tickets whether the other live outcome each one proves is covered elsewhere. Two of the five are in this area, both in `test_budget_claims.py`:

| Test | Live outcome besides the correction | Covered elsewhere? | Recommendation |
|---|---|---|---|
| `::test_a_partly_filled_entry_keeps_claiming_its_whole_fee_provision` (453) | While part of an order is unfilled, its remainder claims the whole recorded fee provision. That is the #2553 owner decision, asserted before the correction (`own.pending_orders == Decimal("100.02")`, `own.fees == Decimal(".01")`, `available == 0`). | **No.** Nothing else in a–e asserts the partial-fill fee claim. `test_a_reservation_claims_its_recorded_fee_provision_after_a_fee_model_change` and `test_envelope_reservations.py::test_a_working_order_blends_actual_fill_cost_with_the_decision_price` cover the provision and the partial cost separately. | Keep the test. Drop only its last three statements (the `_append_correction` call and the `corrected` assert). |
| `::test_interleaved_same_symbol_deployments_keep_own_fifo_after_correction_and_stop` (519) | Two deployments on one symbol, interleaved in time, each keep their own FIFO money (`realized_gross == 10` each, `free` 410 and 310). That is asserted before the correction. The stop section after it depends on the corrected quantities. | **Only partly.** `test_enter.py::test_fills_fold_into_namespace_attributed_exposure_not_account_netting` proves separate *positions*, not separate budget FIFO money. Releasing free budget at a stop is proven by `::test_stopped_bot_still_holding_keeps_its_shares_on_the_bar_and_releases_its_free_budget`. | Keep the test. Drop everything from the `_append_correction` call to the end of the `try` block: the correction and the stop section built on it. |

Dropping trailing statements from a test that stays follows #2701's own precedent: its orphan list drops the `receipts.by_transaction("")` assert from `test_decision_receipts.py::test_write_rejects_missing_strategy_and_reader_rejects_invalid_bounds` and keeps the rest. If the map counts such a trim as a rewrite, which is out of scope, then both tests keep calling `append_execution_correction_or_raise`. Under ☆ "test-only helpers follow their tests", the correction writer would then stay.

## What the cuts orphan

- `test_cutover_cli.py`: the `live_settings` and `AlpacaSettings` imports appear to be used only by the cut test at 259 (2 and 3 occurrences in the file). Project-scope ruff will confirm.
- `test_bot_results.py`: the cut test imports `fee_evidence` locally, so nothing at module level is orphaned.
- `test_corrective_foundation.py`, `test_envelope_reservations.py`: `schema` stays imported; other tests in each file use it.
- `test_cutover_live.py`: `_initialize` and `_evidence` stay; the surviving tests use them.
- `PythonDataService/scripts/pr_shard_durations.json`: stale ids for the deleted tests (for example `test_commands.py::test_get_unknown_command_returns_none`, line 131). Regenerate the file with `scripts.update_pr_shard_durations`, or confirm the sharder ignores unknown ids.
- No `conftest.py` or `cutover_test_support.py` helper is used only by the cut tests (spot-checked; see Not reviewed).

## Hazards the cutting PR must carry

- **Survivors outside this area.** Row 7 depends on `tests/broker_configuration/test_account_risk_routes.py::test_canonical_binary_noise_loss_cap_is_accepted_at_the_boundary`, which #2722 judges. Row 4 depends on the watchdog asserts in `test_reconcile.py`, which #2719 judges. Cut a row only while its survivor still exists at the cutting SHA.
- **The two dev-reset rows point at each other's files.** Row 11 keeps `test_dev_reset.py::test_reset_refuses_unactivated_legacy_authority_without_moving_it`. Row 12 keeps `test_cutover_cli.py::test_dev_reset_cli_refuses_live_mode_without_moving_authority`. Never delete either survivor.
- **#2707's v9 cut changes a refusal message here.** `test_corrective_foundation.py::test_v8_authority_fails_closed_on_open_until_offline_upgrade` matches `"offline v8-to-v9 Clerk upgrade"`. The test stays, because a v8 store must still fail closed. If #2707's item 25 (`upgrade-v9` / `rollback-v9`) lands, the refusal text that names that upgrade changes, so the same PR updates the match.
- **The map's scheduled-end hazard.** Nothing in a–e proves that a live stop cancels a bot's scheduled end. `test_commands.py::test_run_stop_reason_is_the_reason_its_first_stop_committed_under` proves only that the stop reason is recorded. #2719 must find that proof in r–z before the raw-stop tests in `test_scheduled_end.py` go.
- **Kill lists age.** Re-read each row and its survivor at the cutting SHA. Then run `tests/broker/alpaca/clerk/sqlite/` and `tests/broker_configuration/test_account_risk_routes.py`.

## Kept on purpose: looked like cuts, are not

- **The seven colon-in-identity tests** (`test_commands.py` 327–352, `test_enter.py` 1107–1141). They share one validator (`app/broker/alpaca/clerk/sqlite/idempotency.py:27`), but each proves that a different entrance and field is validated. A colon would alias the content-addressed command and effect keys, which is a duplicate-order risk.
- **`test_corrective_foundation.py::test_first_mirror_write_fsyncs_parent_directory`.** A captured-call check, but the only proof that the custody mirror's parent directory is fsynced. That is crash durability, which no outcome test can observe.
- **`test_exit_send_session.py::test_the_live_touch_is_read_on_the_event_loop_and_only_to_re_price`.** It counts quote reads and their thread. The production quote source registers IBKR market-data demand, so an unneeded or off-loop read is a feed fault, not just a cost.
- **Byte-shape facts tests in `test_exit_reducing_shape.py`** (79–214). They pin the exact JSON of hashed custody facts, which keeps older rows replayable and verifiable. These are custody record formats, not wording.
- **`test_bot_history.py::test_only_the_owners_flattens_count_as_a_flatten`.** The only proof that a strategy's own EXIT or a watchdog re-drive never reads as the owner's flatten.
- **`test_exit_lost_submit_incident.py::test_the_committed_incident_ledger_shape_is_reproduced_by_the_clerk`.** It guards the fidelity of the incident fixture that the rest of that file's flatten regressions replay.
- **`test_execution_coverage_set_proof.py::test_s0_one_to_one_predicate_matches_canonical_set_proof_on_unambiguous_inputs`.** The parity test for a duplicate predicate (CLAUDE.md philosophy #5), and the golden one-to-one fixture test beside it.
- **`test_commands.py::test_get_command_returns_the_full_resource`.** It checks full equality of the resource, which is stronger than the state-only read in `test_state_machine_survives_restart`. The route `GET …/commands/{id}` (`app/routers/alpaca_clerk_sqlite.py:394`) and manual-order runtime read it.

## Pointers: items in this area another ticket owns (skipped here)

- **#2701 (clerk dead code) owns:**
  - `test_decision_receipts.py`: `::test_final_outcome_replaces_the_provisional_receipt_without_new_sequence`, `::test_new_evidence_walk_sees_an_older_receipts_final_outcome`, `::test_by_transaction_matches_intent_or_order_and_stays_bounded`.
  - `test_economic_projection_openers.py` (whole file).
  - `test_economic_projection.py`: `::test_pages_are_keyset_bounded_and_account_rows_preserve_effective_state`, `::test_late_correction_keeps_root_execution_time_for_fifo_and_session_window`, `::test_execution_page_cursor_is_rejected_after_effective_fill_revision`, `::test_a_correction_that_explains_the_difference_clears_the_conflict`, and `_append_correction`.
  - `test_envelope_reservations.py`: `::test_a_corrected_fill_reserves_at_its_restated_size`, `::test_a_legacy_remainder_a_correction_reopens_refuses_the_next_enter_under_its_own_code`, and `_append_correction`.
  - The `downward_correction` family and `_correction_transition` in `test_execution_authority_golden.py`.
  - H2 for this area is answered above.
- **#2707 (scripts) owns:** `test_activation_inventory.py` (whole file, if #2707 cuts `qualify_alpaca_activation_inventory.py`) and `test_cutover_cli.py::test_v9_upgrade_and_rollback_cli_require_account_bound_process_stop_evidence` (its item 26).
- **#2722 (broker configuration tests):** keep `test_account_risk_routes.py::test_canonical_binary_noise_loss_cap_is_accepted_at_the_boundary`, the survivor for row 7.
- **#2719 (clerk SQLite r–z):** the scheduled-end proof (see Hazards). `test_reconcile.py`'s watchdog `get_command(...) is None` asserts are the survivor for row 4.

## Not reviewed

- **Body-to-body comparison inside the big files.** `test_exit.py` (52 tests), `test_enter.py` (50), `test_economic_projection.py` (32), `test_exit_send_session.py` (30) and `test_cutover.py` (34) were judged from each test's name, docstring and asserts, with full reads only for candidates. Near-identical scenarios were not diffed body to body: the cutover `initialize_refuses_*` refusals, the send-session time matrix, and the exit cancel/poll uncertainty family. A duplicate among them may remain.
- **Cross-file duplicates beyond the spot checks.** Searches outside a–e covered only `test_reconcile.py`, `tests/broker_configuration/test_account_risk_routes.py`, `tests/broker/alpaca/clerk/test_account_money.py` and `test_trade_evidence.py`. Stronger tests in f–z, `tests/services/`, `tests/routers/` or `tests/broker/v2panel/` that could make a test here a duplicate were not searched for systematically.
- **`conftest.py` (687 lines) and `cutover_test_support.py`.** Their helpers were not each traced to a surviving caller; the cuts above use none of them alone, by spot check.
- **Owned elsewhere, not judged:** `test_activation_inventory.py`, `test_economic_projection_openers.py`, and the golden families in `test_execution_authority_golden.py` (sacred).
