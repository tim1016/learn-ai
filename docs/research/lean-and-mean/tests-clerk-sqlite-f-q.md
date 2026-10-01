# Kill list — clerk SQLite tests, files f–q (#2718)

Part of map #2700. Area: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_[f-q]*.py` (24 files, 15,237 lines).

**Read at `6a4d7d39`** (`origin/master` on 2026-09-30). The map was charted at `87b8e261`. No file in this area changed between the two SHAs (`git diff --stat 87b8e261 6a4d7d39` touches only `test_crash_restart.py`). Plan, don't cut.

Paths are relative to `PythonDataService/tests/broker/alpaca/clerk/sqlite/` unless they start with `PythonDataService/`, `docs/`, `scripts/` or `.github/`.

## How this was judged

1. A script listed every test in all 24 files with its docstring and every `assert` / `pytest.raises` line. Each test was judged against the bar from that listing. A second script flagged tests whose asserts compare against prose strings.
2. The full body was read for every candidate row, and for both sides of every duplicate. A duplicate row names the stronger test that survives.
3. Cross-checks: the blocking dead-code list (#2701) and its owner addendum; the scripts (#2707), CI-gate (#2716), routes (#2706) and services (#2703) kill lists, for anything that makes code this area tests dead; and every other test module that imports helpers from these files.
4. Sacred is the behavior. Most of this area proves custody, fencing, budget, loss-hold, flatten or "no order was sent" outcomes. Those tests stay even when they also pin copy, because cutting the copy assert out of a test would be a rewrite. A test only goes when everything it proves is copy, trivial, or already proven elsewhere.

**Skipped, owned by #2701** (dead `EXECUTION_CORRECTED` path, kind 5): `test_folds_execution.py::test_execution_correction_replaces_effective_quantity`, `::test_execution_correction_invalid_target_raises_uncertainty`, `::test_execution_correction_missing_target_symbol_raises_uncertainty`, and the `_correction_transition` helper; `test_fee_evidence.py::test_corrected_fill_reprices_original_fee_day`; `test_manual_orders.py::test_manual_execution_correction_replaces_only_manual_custody`; `test_historical_execution_recovery.py::test_timeline_execution_filter_includes_a_correction_of_that_execution`. The two H2 tests #2701 asked this ticket to check are answered under Hazards.

## Kill list

### Kind 1 — trivial

| Item | Kind | Evidence |
|---|---|---|
| `test_manual_order_ended_at_broker.py::test_the_owner_copy_covers_exactly_the_unfilled_terminal_states` (236) | 1 | It asserts `frozenset(_ENDING_COPY) == UNFILLED_TERMINAL_STATES`. The module already checks that exact equality when it loads (`app/broker/alpaca/clerk/sqlite/manual_order_completion.py:103`, which raises on anything missing or extra). So this test cannot fail while the module imports. The guard's failure path is proven by `::test_an_unfilled_state_with_no_owner_copy_stops_the_import_not_the_sweep` (240). |
| `test_projections.py::test_timeline_exposes_source_observation_and_record_clocks` (588) | 1 | It asserts that two clocks are `> 0`, that `source_event_at_ms is None` for a registration row, and that `operation_ref == f"transition:{sequence}"`. The Frontend shows that ref only as an opaque `<code>` token (`alpaca-sqlite-custody.component.html:215`). Nothing reads its format. |
| `test_qualification.py::test_full_profile_pins_required_scale_points` (58) | 1 | It restates the constant `PROFILE_SCALES["full"]` as a literal tuple. No CI job or script runs the `full` profile (`ci.yml:521` runs `--profile smoke`). The rest of this file goes as kind 5 below, but this row can go now on its own. |

### Kind 2 — copy and wording pins

| Item | Kind | Evidence |
|---|---|---|
| `test_loss_hold.py::test_the_operator_explanation_names_the_prior_close_cash_flow_basis` (99) | 2 | All three asserts are substrings of the hold's `explanation` prose. What the hold actually does (blocks entry, admits reduction, refuses a cause it cannot decode) is proven by `::test_a_raised_loss_hold_blocks_new_exposure_and_admits_every_reduction` and `::test_a_stored_cause_this_build_cannot_decode_refuses_both_capabilities`. |
| `test_live_envelope_sync.py::test_unavailable_evidence_logs_its_cause_beside_the_plain_message` (483) | 2 | It pins the `why` and `cause` strings of a log line. Its one behavior assert, `tick() == "unknown"` on a `BrokerEvidenceUnavailable` activity read, is proven more strongly by `::test_rejected_cash_transfer_evidence_withdraws_the_previous_observation` (466), which also proves the observation is withdrawn. |
| `test_live_envelope_sync.py::test_unavailable_evidence_without_a_chained_cause_logs_no_cause` (507) | 2 | It asserts only `record.cause is None` on the same log line. The behavior is the same as the row above. |
| `test_lane_quiet.py::test_the_log_that_the_broker_was_unreadable_says_why` (498) | 2 | It pins the broker's message text in a `lane_quiet_broker_unreadable` log record. The outcome, an unreadable broker giving no answer rather than a "not quiet" one, is proven by `::test_an_unreadable_broker_is_no_answer_not_a_not_quiet_one` (489). |
| `test_manual_order_ended_at_broker.py::test_a_gtc_limit_alpaca_expires_reads_as_expired_at_alpaca` (196) | 2 | It asserts only the GTC branch of the owner copy (`"Expired at Alpaca."`). That an Alpaca expiry ends the manual effect, releases other bots and records the ending is proven by the parametrized `::test_a_manual_limit_alpaca_ends_unfilled_ends_its_effect_and_bots_may_enter_again` (158, the `expired` case). |
| `test_manual_order_ended_at_broker.py::test_a_share_count_of_a_million_or_more_reads_in_plain_digits` (221) | 2 | It asserts only the sentence `"Cancelled at Alpaca with 1250000.5 of 2000000 shares filled."`, which is number formatting in owner copy. Partly-filled-then-cancelled custody is proven by `::test_a_partly_filled_manual_limit_cancelled_at_alpaca_keeps_its_fill_and_ends`. |
| `test_manual_order_replaced_at_alpaca.py::test_a_working_manual_order_shows_no_replacement_note` (1009) | 2 | It asserts only that a plain working order carries no replacement note. The note's real lifecycle (shown while replaced, cleared once the leg ends) is asserted at lines 280 and 301 of `::test_a_replaced_manual_order_ends_when_its_replacement_is_cancelled` (263). |

### Kind 3 — duplicates

| Item | Kind | Stronger survivor and evidence |
|---|---|---|
| `test_hashchain.py::test_canonicalize_is_deterministic_regardless_of_input_order` (42) | 3 | `test_hashchain.py::test_canonicalize_is_sorted_key_no_whitespace` (38) pins the exact sorted-key output. Exact sorted output already implies the same output for any input order. |
| `test_hashchain.py::test_compute_row_hash_matches_manual_sha256_string_concatenation` (52) | 3 | `test_hashchain.py::test_compute_row_hash_is_string_not_byte_concatenation` (58) pins the same `sha256(prev + payload)` formula with a real 64-hex `prev_hash`, which also tells it apart from byte concatenation. Its own docstring says GENESIS "can't discriminate this". |
| `test_manual_orders.py::test_manual_cancel_refuses_a_terminal_target_before_creating_a_cancel_effect` (1582) | 3 | `test_manual_order_ended_at_broker.py::test_a_clerk_cancel_after_alpaca_cancelled_the_order_is_refused_without_a_delete` (352). Both hit the same refusal branch (`manual_order_cancellation.py:136-141`, effect state terminal). Both assert `ManualOrderCancelTerminalError`, no DELETE sent and no cancellation recorded. The survivor reaches the terminal state through the real `canceled` frame fold, where this test hand-appends the transition, and it also asserts there is exactly one ending. |
| `test_projections.py::test_timeline_can_filter_by_effect_operation_identity` (608) | 3 | `test_historical_execution_recovery.py::test_timeline_filters_bind_one_historical_conflict_and_cursor_scope` (238) asserts the same `{entry.effect_operation_id} == {effect}` set for the effect filter. It covers the bot, order, uncertainty, execution, kind and sequence filters too, plus cursor scoping. |
| `test_qualification_shadow_trace.py::test_compare_canonical_traces_accepts_identical_sequences` (190) | 3 | `test_qualification_shadow_trace.py::test_shadow_trace_reproduces_the_first_ema_round_trip` (140) passes only if `compare_canonical_traces` accepts identical traces (`run_shadow_trace_evaluation` calls it at `qualification_shadow_trace.py:354`), and it does so on real EMA traces rather than one sample. This test has no assert. |

### Kind 5 — retired, once another ticket's cut lands

These test code that a closed dead-code or gates list cuts, but which no dead-code list carries as a row of its own (#2707 points these orphans at #2701, with the tests left to this ticket). Each row is cut **in the same PR** as the code it tests, never before.

| Item | Kind | Evidence |
|---|---|---|
| `test_offline_v9_upgrade.py` (whole file, 9 tests) | 5 | It tests only `app/broker/alpaca/clerk/sqlite/offline_v9_upgrade.py`, whose one entry point is the `upgrade-v9` / `rollback-v9` subcommands that #2707 row 25 cuts. Its condition is #2707 hazard 2: the cut waits until no store or restorable backup below v9 exists, which census #2736 settles. **Conflict:** #2701 lists `offline_v9_upgrade.py` under "Kept on purpose". See Map attention below. |
| `test_qualification_ui_campaign_contract.py` (whole file, 3 tests) | 5 | It tests only `qualification_ui_campaign_contract.py`, part of the synthetic-rehearsal island #2707 orphans with rows 21–23 (only `qualification_ui_evidence.py` and the island schema import it). The cross-stack JSON `contracts/alpaca-clerk-ui-correlation-campaign.v4.json` stays, because the Frontend e2e spec reads it. |
| `test_qualification.py`: the 13 rehearsal-island tests — `::test_synthetic_5s_composition_preserves_exact_minute_ohlcv`, `::test_polygon_replay_refuses_a_continuity_policy`, `::test_polygon_fixture_replays_through_live_feed_path` (slow), `::test_synthetic_rehearsal_uses_protected_generation_and_matches_recovery_heads` (slow), `::test_polygon_core_failure_is_classified_as_feed_abort`, `::test_storage_boundary_failure_is_classified_as_infrastructure_abort`, `::test_recovery_hash_failure_is_classified_as_custody_abort`, `::test_protected_generation_does_not_infer_custody_from_refusal_text`, `::test_protected_generation_promotes_typed_integrity_refusal_to_custody`, `::test_protected_generation_treats_invalid_backup_clock_as_infrastructure`, `::test_production_authority_identity_requires_the_claimed_established_account`, `::test_mount_topology_proves_separate_rw_qualification_and_ro_production`, `::test_mount_topology_rejects_writable_or_shared_production` | 5 | They exercise `qualification_polygon_replay.py`, `qualification_storage_recovery.py` and `qualification_synthetic_rehearsal.py`. Outside tests, those are imported only by `qualification.py` and by each other (import census at this SHA). Their one runner is `--synthetic-rehearsal`, started only by the compose `alpaca-clerk-qualification` service (`compose.yaml:337-338`), which #2707 rows 21–23 cut. Condition: #2707 rows 21–23. |
| `test_qualification.py::test_bounded_profile_records_pragmas_sizes_latencies_and_index_plans` (66) | 5 | It exercises the `--profile` path in `qualification_performance.py`. That path's only runner is the CI job `alpaca-sqlite-qualification-smoke` (`ci.yml:521-522`), which #2716 row 2 cuts. Condition: #2716 row 2. |

Once all three `test_qualification.py` rows go, the file is empty and goes whole.

## What the cuts orphan

- `test_folds_execution.py::_correction_uncertainty_transition` (205), a test helper. Its users are the three #2701 correction tests, the H2 test `::test_cumulative_recovery_fill_is_explicitly_tagged` (559, see H2 below), and `test_economic_projection.py:1563`, which is also a #2701 row. #2701 does not list it. It goes with them.
- `test_economic_projection.py:52-55` imports `_correction_transition` and `_correction_uncertainty_transition` from `test_folds_execution` **at module level**. When #2701 deletes them, that whole file stops collecting unless the import goes in the same PR. The file is in #2717's area (a–e).
- `ExecutionCorrectedFacts` and other imports left unused in `test_folds_execution.py`, `test_fee_evidence.py` and `test_manual_orders.py` after the #2701 rows go. Imports left unused by this list's rows, for example `manual_order_completion` / `order_projection` in `test_manual_order_ended_at_broker.py` if both copy tests go. Project-scope ruff finds them.
- `PythonDataService/tests/_helpers/ui_correlation.py`. Its importers are `test_qualification.py` (above), `tests/scripts/test_run_alpaca_sqlite_qualification.py` (#2707 row 24) and `tests/schemas/test_account_custody_synthetic_qualification.py` (#2730's area). It is dead once all three go.
- **Not orphaned:** the Polygon fixture `tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17/` (`tests/_helpers/parity_fixture.py` also reads it), `conftest.py::_hold_transition` (four other files use it), and `contracts/alpaca-clerk-ui-correlation-campaign.v4.json` (the Frontend e2e).
- `PythonDataService/scripts/pr_shard_durations.json`: stale ids for deleted tests are harmless (`scripts/pytest_shard.py:7-11`, per #2707). Regenerate after the cuts.
- No `SMOKE_ADVERSARIAL_TESTS` entry (`qualification_performance.py:34`) names a test in this list.

## Hazards the cutting PR must carry

- **These test modules double as helper libraries.** Other suites import module-level names from them: `test_manual_orders` (`LEG_ID`, `OPERATOR_ID`, `TICKET_ID`, `FakeTrade`, `filled_order` → `test_budget_claims`, `test_two_bots_one_symbol`, `test_manual_order_ended_at_broker`, `test_manual_order_replaced_at_alpaca`, `tests/broker/v2panel/test_budget_deploy`), `test_live_envelope_sync` (`_Read`, `_cash_flow` → `test_simulated_account`, `test_two_bots_one_symbol`, `test_risk_fee_evidence`, `test_account_risk_policy`, `tests/broker_configuration/test_account_risk_routes`, `tests/services/test_alpaca_live_verdict`), `test_fee_evidence` (`_activity`, `_seed` → `test_fee_attribution_view`, `test_risk_fee_evidence`, `test_account_risk_routes`), and `test_folds_execution` (`_repository_for_strategy`, `_simulated_aggregate` → `test_v14_simulated_execution_evidence`). Delete test functions only. Keep every module-level helper that something still imports.
- **H2 (from #2701), answered for this area:**
  - `test_folds_execution.py::test_cumulative_recovery_fill_is_explicitly_tagged` (559) **can go with the correction writer.** Its live outcome, a cumulative recovery fill that adds only the delta and is tagged `cumulative_recovery` with no execution id, is already proven without a correction. The delta is proven by `::test_one_exact_auto_supersedes_many_cumulative_recovery_rows` (recoveries 2 then 5, at lines 999–1009) and by `::test_many_to_many_coverage_supersession_preserves_exact_provenance_and_replay`. The tag is proven by `::test_a_real_rest_aggregate_never_gains_an_exact_execution_classification` (716).
  - `test_fee_evidence.py::test_simulated_fee_cutoff_uses_inclusive_economic_time` (1060) **can go with the correction writer.** Its first half proves the simulated fee cutoff is inclusive: fills at `close-1` and `close` count, `close+1` does not. It is the only test that passes `simulated_fill_cutoff_ms` directly. One layer up, though, the only production caller (`app/broker/alpaca/clerk/sqlite/simulated_account.py:112,210,256`) is proven on both sides of the boundary by `test_simulated_account.py::test_prior_close_baseline_uses_matching_fill_mark_and_fee_cutoffs` (157, parametrized `entry_at_close`). A fill at exactly the close puts its $0.01 modelled fee into the prior-close baseline (1009.99), and a fill one ms later keeps both the fill and the fee out (1000). `::test_after_close_sale_keeps_realized_change_and_fee_out_of_baseline` (183) adds the sell side. The rest of the test, a correction repricing through the cutoff, is the dead path.
  - `test_offline_v9_upgrade.py::_seed_production_v8` seeds an `EXECUTION_CORRECTED` row, which the v9 ceremony replays through `V9_FOLD_REGISTRY` (derived from the default registry). If the v9 file outlives the correction fold, because census #2736 finds a sub-v9 store, the seed must change in the fold-removal PR, or that PR waits.
- **Kind-5 cuts ride with their code.** `test_qualification.py` and `test_qualification_ui_campaign_contract.py` go in the same PR as #2707 rows 21–23 / #2716 row 2. `test_offline_v9_upgrade.py` goes in the #2707 row-25 PR, after #2736. Cutting them earlier leaves live-until-then code untested.
- **The scheduled-end hazard on the map** (raw-stop tests in `test_scheduled_end.py`): no file in f–q covers a scheduled end. `grep -l "scheduled_end\|SCHEDULED_END" test_[f-q]*.py` is empty, so the confirmation that a live stop cancels the scheduled end falls to #2719 (r–z) or another test ticket, not to this one.
- **Re-check at the cutting SHA.** Kill lists age. Each duplicate row's survivor must still exist and still assert what is quoted above.

## Kept on purpose (borderline, judged and kept)

- **Mixed tests that pin copy beside a money outcome stay**, because cutting the copy assert out of them would be a rewrite. Examples: `test_live_envelope_sync.py::test_a_hold_that_stands_over_an_unjudgeable_account_says_so`, the only test that the loss hold stands (`hold_stands`) when the account turns unjudgeable. Also `test_fee_attribution_view.py::test_today_says_why_when_the_clerk_is_offline` and `test_projections.py::test_an_escalated_exit_projects_no_next_attempt`.
- `test_loss_hold.py::test_the_explanation_dollars_the_sealed_floats_each_normalized_on_their_own`. Its assert is on copy, but it proves the operator sees the half-even dollar figure (#2612) **and** that rendering never rewrites the sealed cause bytes. That is custody evidence.
- `test_lane_quiet.py::test_every_registered_reason_declares_its_lane_quiet_answer`. It pins the whole reason-policy table, including codes that have no behavior test of their own. It decides which episodes block a draining lane's flat answer, so on the money path it stays.
- `test_lost_fill_slice_rederivation.py::test_stale_final_rest_ack_folded_last_does_not_govern`, a `xfail(strict=True)` probe of a known residual. Its marker says the fix is "owner decision pending" (#2385 review). It proves no live behavior, but none of the five kinds fits it. Its fate is that owner decision, not this list's.
- Perf guards from real incidents: `test_order_history_lookups_are_targeted.py` (#1942: one row per lookup, index short-circuit), `test_projections.py::test_hot_projection_queries_use_covering_fold_indexes`, and `test_lost_fill_slice_rederivation.py::test_reconcilable_worklist_read_probes_fills_through_its_indexes`.
- Hash-chain format pins that keep old rows' hashes valid: the rest of `test_hashchain.py`, `test_lost_fill_slice_rederivation.py::test_order_submit_acked_facts_omit_an_absent_cumulative`, and `test_manual_ticket_legs.py`.
- `test_qualification_shadow_trace.py` (the rest of it) stays. `qualification_shadow_trace.py` is live: `app/services/run_replay_proof.py:29` imports `run_shadow_trace_evaluation`, and `bot_runner`, `run_gate`, `decision_clock` and `bot_decision_quarantine` reach that module.
- `test_live_envelope_sync.py::test_only_a_change_of_verdict_is_logged` and `::test_a_changed_cause_of_unavailable_evidence_is_logged_again` stay. They prove when a 15 s loop logs, which is anti-spam behavior an operator sees, not wording.

Everything else in the 24 files proves an order, custody, fencing, budget, loss-hold, flatten or no-order-sent outcome that no other test in this area already proves, and stays.

## Map attention

- **A dead-code gap between two closed lists.** The qualification package is dead after #2707 rows 21–23 and #2716 row 2 together, but no dead-code ticket lists it, because each list assumed the other mode kept the runner alive (details under Pointers). It needs an owner before handoff slicing: a row on a dead-code handoff, or an addendum to #2707.
- **Two closed lists disagree on `offline_v9_upgrade.py`.** #2701 keeps it; #2707 row 25 cuts its only entry point. The map's decision line for #2707 already records the v9 cut. Census #2736 is the evidence that settles it, and `test_offline_v9_upgrade.py` follows.
- **No rule-level owner question.** Both H2 mixed tests in this area have their live outcome proven elsewhere, so neither forces a rewrite.

## Pointers: things outside this area

- **Gap between #2707 and #2716, needs a dead-code owner.** #2716 row 2 assumes `scripts/run_alpaca_sqlite_qualification.py` survives because compose runs its rehearsal mode. #2707 rows 21–23 assume it survives because CI runs its profile mode. Each cuts the other's reason. Once both land, nothing runs the script, so all of the following are reachable only from tests: the script, `app/broker/alpaca/clerk/sqlite/qualification.py`, `qualification_performance.py`, `qualification_polygon_replay.py`, `qualification_storage_recovery.py`, `qualification_synthetic_rehearsal.py`, `qualification_ui_evidence.py`, `qualification_ui_campaign_contract.py`, and the services #2703 found reachable only through that script (`app/services/account_custody_qualification.py`, `account_custody_synthetic_scenarios.py`, `alpaca_sqlite_synthetic_*.py`). `qualification_shadow_trace.py` is **not** in that set. With them go `tests/scripts/test_run_alpaca_sqlite_qualification.py::test_profile_arguments_remain_backward_compatible` (#2731) and `tests/schemas/test_account_custody_synthetic_qualification.py` (#2730).
- **#2701 vs #2707 on `offline_v9_upgrade.py`.** #2701 keeps it, for any v8 store. #2707 row 25 cuts its only entry point. Census #2736 is what decides it. The test row above follows that decision.
- **#2717 (a–e):** the module-level helper import at `test_economic_projection.py:52-55`, above.
- **#2719 (r–z):** the scheduled-end confirmation, above.
- **#2730 (router and schema tests):** `tests/schemas/test_account_custody_synthetic_qualification.py` goes with the rehearsal island.

## Not reviewed

- **Duplicates outside the area were not swept.** Each kept test was checked against its neighbours in f–q and the files named above, but no search was made of `tests/routers/`, `tests/broker/v2panel/`, `tests/services/` or the a–e and r–z files for stronger tests that would make a kept f–q test a duplicate. The fee and Today routes in `test_fee_attribution_view.py` and the projection reads in `test_projections.py` are the likeliest places for such overlap.
- **Assert-level trimming inside kept tests.** Copy asserts inside mixed tests are not rows, because removing them is a rewrite.
- **Bodies of kept tests.** These were read only where their name, docstring or asserts raised a question. A test whose setup quietly fails to reach the path its name claims would not have been caught.
- **Whether the five kept perf guards still fire.** Each checks a query plan or row count. Whether a plan regression would actually fail them on current SQLite was not run.
