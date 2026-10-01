# Kill list — v2 panel tests (#2723)

Part of map #2700. **This plans the cuts and makes none.**

- **SHA:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` on 2026-09-30). The map was charted at `87b8e261`. Between the two SHAs the only change in this area is one added test in `test_panel_projection.py` and its adapter change. No row below depends on that change.
- **Area:** `PythonDataService/tests/broker/v2panel/`. That is 39 test files plus `conftest.py` and `fixtures.py`, about 22K lines and about 690 tests. Paths below are relative to that folder unless they start with `app/`, `scripts/` or `tests/`.
- **Kinds** follow the map: (1) trivial, (2) copy and doc pinning, (3) duplicate, (4) mock theater, (5) retired feature.

## How this was judged

- **Every test was read against the bar.** For each test I pulled its name, docstring and assert lines with an AST script. For the borderline ones I then read the full body. A test stays when it proves an outcome a user or caller sees, such as an order, custody, flatten, budget, lease, fencing, deploy-gate or feed-state outcome. "Nothing was deployed" (`registry.deploy_calls == []`) and "the broker port was never called" count as outcomes. In the money-path files I cut only wording pins, trivial checks and duplicates. When I was unsure, I kept the test.
- **Copy pins.** A test goes as kind 2 when every assertion compares owner-facing prose to a literal string, or checks that a word is present or absent. It stays when the prose sits beside a behavioural assertion, such as a status code, a refused deploy, a reason code, a state or a group. Rewriting a surviving test to drop its wording assertions is out of scope.
- **Duplicates.** I checked one layer up and one layer down: snapshot test ↔ snapshot generator, profile helper ↔ HTTP route, `select_primary_action` unit ↔ SQLite adapter, outcome-copy helper ↔ panel projection, and registry import-time validation ↔ the static check. Each kind-3 row names the test that survives.
- **Skipped as owned elsewhere.** #2703 (`dead-services`, and its owner addendum) lists nothing under `tests/broker/v2panel/`. #2706 (`dead-routes`) owns `test_panel_router.py::test_live_chart_accepts_five_second_resolution`, `::test_chart_contract_rejects_unknown_resolution_and_timeframe`, and the trims at `test_shadow_operator_surfaces.py:208`, `:309` and `:619`. #2701 (`dead-alpaca-clerk`) and #2704 (`dead-engine`) only name this folder as a test run to repeat, or as the arrange step of tests that stay. None of those items appears below.

## Kill list

| # | Item | Kind | Evidence |
|---|---|---|---|
| 1 | `test_outcome_copy.py::test_each_way_a_run_ends_has_its_own_plain_words` (18 params) | 2 | Each case asserts `outcome_headline(...) == "<literal headline>"`. The coverage tests in the same file stay: every kind has non-empty words (`test_every_kind_has_its_own_words_on_both_surfaces`), and a stop reads as its custody proof (`test_a_stop_reads_as_its_custody_proof_on_the_panel`). |
| 2 | `test_outcome_copy.py::test_a_failed_launch_keeps_its_own_words_and_the_proofs_next_step_on_the_panel` | 2 | `label == "Failed to launch"`, `explanation.startswith("The launch failed partway through")`, `"Use Flatten" in explanation`. Wording only. |
| 3 | `test_hold_copy_never_paused.py` (whole file, 2 tests) | 2 | Scans `OPERATOR_COPY` and the string literals under three source roots for the word "paused". It pins wording and proves no outcome. |
| 4 | `test_panel_profile.py::test_alpaca_profile_is_the_closed_descriptor` | 1 | Restates three constants of a static descriptor (`broker == "alpaca"`, `fee_fidelity == "none"`, `live_bars_supported is False`). |
| 5 | `test_panel_profile.py::test_alpaca_profile_covers_all_six_stations` | 3 | The same fact goes over HTTP in `test_panel_router.py::test_panel_profile_endpoint` (broker and six stations). |
| 6 | `test_panel_profile.py::test_unknown_broker_has_no_profile` | 1 | `panel_profile_for("ibkr") is None` and `panel_profile_for("nope") is None`: a dict lookup that misses. |
| 7 | `test_panel_profile.py::test_alpaca_profile_shape_is_frozen` | 3 | Pins the response model's field set. The committed OpenAPI snapshot already pins the `/panel-profile` response shape (`contracts/openapi/python-data-service.openapi.json:67130`, schema `PanelProfile` `:30805`, CI-gated). With rows 4–7 the whole file goes. |
| 8 | `test_vocabulary_snapshot.py::test_snapshot_file_exists` | 1 | The next tests open the file, so they fail on its absence anyway. |
| 9 | `test_vocabulary_snapshot.py::test_snapshot_copy_matches_live_operator_copy_exactly` | 3 | `build_snapshot()` (`scripts/regenerate_broker_v2_vocabulary_snapshot.py:85-102`) emits `copy` from `OPERATOR_COPY` for every code. So `::test_committed_snapshots_match_freshly_generated_output` (byte equality) already fails on any copy drift. |
| 10 | `test_vocabulary_snapshot.py::test_snapshot_matches_live_vocabulary` | 3 | `codes` is `sorted(ALL_VOCABULARY_CODES)` in the same generator, so a missing or extra code breaks `::test_committed_snapshots_match_freshly_generated_output`. |
| 11 | `test_vocabulary_snapshot.py::test_snapshot_codes_are_sorted_and_unique` | 3 | The generator sorts a set. Byte equality with its output (`::test_committed_snapshots_match_freshly_generated_output`) implies the committed codes are sorted and unique. |
| 12 | `test_vocabulary_snapshot.py::test_every_emitted_code_has_nontrivial_copy` | 3 | Presence: `build_snapshot()` indexes `OPERATOR_COPY[code]` for every code and raises `KeyError` on a missing one, inside `::test_committed_snapshots_match_freshly_generated_output`. The rest of the test (`len(explanation) > len(code)`, `explanation != code`) is a wording-quality rule, kind 2. |
| 13 | `test_vocabulary_snapshot.py::test_copy_map_has_no_orphan_entries` | 1 | Unused copy entries are invisible to every caller. This is hygiene, not an outcome. |
| 14 | `test_vocabulary_snapshot.py::test_missing_copy_raises_keyerror` | 1 | `copy_for` is `return OPERATOR_COPY[code]` (`app/broker/v2panel/vocabulary.py:401-408`). The test proves that a dict raises `KeyError`. |
| 15 | `test_vocabulary_snapshot.py::test_terminal_lifecycle_excludes_retired_controls` | 1 | Asserts four retired strings (`pause`, `continue`, `resume`, `PAUSED`) are absent from a set. Membership drift is already caught by `::test_literal_matches_runtime_collection` and the snapshot test. |
| 16 | `test_panel_deploy_live.py::test_the_live_view_names_real_money_and_deploy_consent` | 2 | Every assertion is a word check on prose: `"real-money"`/`"consent"` in, `"arm"` not in, `label == "Live account posture"`, `"paper"` not in headline. The live mode offer is proven by `::test_the_live_world_offers_live_and_dry_run_only`. |
| 17 | `test_panel_deploy_live.py::test_live_receipt_uses_budget_and_risk_without_a_second_action` | 2 | `receipt.message == f"{SID} is on duty in Alpaca live."`, plus word checks on `explanation` and `next_action`. |
| 18 | `test_panel_deploy_live.py::test_an_evidence_override_preserves_live_budget_and_risk_terms` | 2 | Word checks only (`"human override of evidence-only"`, `"budget"`, `"risk"`, `"stop the bot if behavior differs"`). |
| 19 | `test_panel_deploy_shadow.py::test_shadow_view_never_calls_the_live_account_a_paper_account` | 2 | Seven literal or word asserts on the posture row and the eligibility headline. The shadow mode offer and the shadow label are proven by `::test_shadow_world_authors_shadow_and_dry_run_only`. |
| 20 | `test_panel_deploy_shadow.py::test_real_paper_view_still_names_the_paper_account` | 2 | The docstring says "Characterization pin: the real-paper world's prose stays byte-identical." Six literal-string asserts. |
| 21 | `test_panel_deploy_shadow.py::test_shadow_receipt_names_the_shadow_world_not_paper` | 2 | Literal `message`, `explanation` and `next_action`, plus `"paper"` not in prose. |
| 22 | `test_panel_deploy_shadow.py::test_paper_receipt_copy_is_unchanged` | 2 | The docstring says "Characterization pin: … three sentences stay byte-identical." |
| 23 | `test_catalog_projection.py::test_status_label_maps_the_closed_vocabulary` | 2 | Three `status_label_for(...) == "Working"`/`"Off duty"`/… literals. The crash-vs-stop distinction that matters stays in `::test_an_unclean_exit_is_labelled_distinctly_from_a_deliberate_stop`. |
| 24 | `test_catalog_projection.py::test_every_world_is_worded_one_way` (param) | 2 | `world_label == "<literal>"` per world. The world grouping is proven by the group tests in the same file (`::test_a_dry_run_is_its_own_group_while_it_runs_or_holds` and others). |
| 25 | `test_chart_projection.py::test_seven_day_live_resolver_cap_unchanged` | 1 | `assert MAX_CHART_RANGE_MS == 7 * 86_400_000`, a constant restated. |
| 26 | `test_chart_projection.py::test_aggregator_bars_to_chart_bars_empty_input_returns_empty_list` | 1 | `f([]) == []` for a list comprehension. The mapping itself is proven by `::test_aggregator_bars_to_chart_bars_maps_fields_and_decimals`. |
| 27 | `test_sqlite_roster_source.py::test_panel_bot_not_found_maps_to_the_unknown_bot_status` | 1 | Asserts two class attributes (`http_status == 404` / `503`). The HTTP split is proven over the wire by `test_bot_clear.py::test_the_bot_page_of_an_unknown_bot_is_not_found` (404) and `test_panel_router.py::test_exhausted_panel_projection_refuses_and_says_so` (503). |
| 28 | `test_history_batch_client.py::test_fetch_batch_builds_its_client_with_the_inner_read_timeout` | 4 | Patches `build_internal_client` and asserts it received `HISTORY_BATCH_INNER_TIMEOUT_S`. That restates the call at `app/services/broker_v2_panel/history_batch_client.py:133`. The timeout's outcome is proven by `::test_timeout_degrades_to_coordinator_unavailable`. |
| 29 | `test_bot_end_routes.py::test_a_bots_end_is_read_with_its_panel_not_on_its_own` | 1 | Asserts `GET …/end` answers 405, which is the framework's answer for a route that does not exist. The route set is pinned by the OpenAPI snapshot. |
| 30 | `test_action_execution.py::test_live_panel_skips_resume_admission_reconciliation` | 4 | Every collaborator of `get_panel` is monkeypatched and `build_panel` returns a sentinel. The test asserts `panel is sentinel` and that a fake `preview_resume_admission` was called 0 times. Production has no `preview_resume_admission` since Resume was retired (#2540; `rg` over `app/` finds nothing), so the test cannot fail. |
| 31 | `test_action_execution.py::test_reason_left_optional_for_non_comment_actions` | 1 | `request.reason is None` is a Pydantic field default. |
| 32 | `test_deploy_submissions.py::test_every_catalog_strategy_has_its_own_code` | 3 | `app/engine/strategy/registry.py:1801` runs `validate_deploy_codes(_STRATEGY_REGISTRY)` at import, so a duplicate or empty code fails every test that imports the registry. The refusal is proven by `::test_catalog_load_refuses_a_missing_or_duplicate_code`. |
| 33 | `test_qualification_recorded_history.py::test_stable_unit_fraction_int_is_pure_and_non_negative` | 3 | Tests a private hash helper. Its determinism is pinned end to end by `::test_golden_batch_matches_the_committed_fixture` (fixture FQ-001). |
| 34 | `test_qualification_recorded_history.py::test_same_as_of_ms_and_symbol_produce_the_same_batch_across_calls` | 3 | Equality with the committed golden batch (`::test_golden_batch_matches_the_committed_fixture`) already implies call-to-call determinism. |
| 35 | `test_qualification_recorded_history.py::test_recorded_bar_source_matches_the_history_bar_source_protocol` | 1 | An `isinstance(bars[0], PolygonBar)` check. The protocol is exercised for real by `::test_build_qualification_recorded_history_batch_runs_the_real_walk`. |
| 36 | `test_panel_projection.py::test_stop_outcome_copy_distinguishes_approved_carryover` | 2 | `label == "Stopped with approved carryover"`, `"durable checkpoint" in explanation`. |
| 37 | `test_panel_projection.py::test_a_crash_the_feed_caused_says_the_feed_stopped` | 2 | A literal label and a literal explanation (`"Crashed: market data stopped"`, …), plus a word check. |
| 38 | `test_panel_projection.py::test_a_launch_that_failed_after_its_stop_reads_as_a_failed_launch_not_an_operator_stop` | 2 | A literal label and a literal explanation, plus `"operator" not in explanation`. |
| 39 | `test_panel_projection.py::test_crash_copy_is_source_neutral_and_not_a_market_data_verdict` (param) | 2 | `label == "Crashed"` and a literal two-sentence explanation. |
| 40 | `test_panel_projection.py::test_a_resume_hole_refusal_carries_its_own_duty_outcome_copy` (param) | 2 | `duty_outcome.label == <literal>` per reason. |
| 41 | `test_panel_projection.py::test_select_primary_action_stopped_bot_has_none` | 3 | The same outcome goes through the real SQLite adapter in `::test_sqlite_adapter_stopped_bot_without_a_cure_has_no_primary_action`. |
| 42 | `test_panel_projection.py::test_select_primary_action_running_sqlite_bot_stops_its_decisions` | 3 | Same as `::test_sqlite_adapter_running_bot_without_a_cure_primaries_its_stop` (`primary_action == "stop_bot_decisions"`), which goes through the real adapter. |
| 43 | `test_panel_projection.py::test_select_primary_action_recovery_cure_outranks_the_lifecycle_command` | 3 | Same as `::test_sqlite_adapter_recovery_cure_is_the_primary_action` (`== "resolve_execution_coverage"`), which goes through the real adapter. |

**Considered and kept.** These were looked at and kept on purpose:

- Prose sitting beside an outcome: the deploy refusals in `test_bot_end_routes.py`, `test_deploy_scoped_route.py` and `test_deploy_submissions.py`, where the status code, `submission_settled` and "nothing started" carry the test. Also the market-pulse headlines in `test_market_pulse.py`, where session and feed state carry it, and `test_refused_warmup_crash_carries_its_own_backend_copy`, where `reason_code == "WARMUP_HISTORY_UNAVAILABLE"` is the classification.
- The literal concurrency tokens in `test_archive_eligibility.py::test_the_archive_token_keys_only_on_the_facts_its_rule_reads` and `test_panel_projection.py::test_the_served_archive_is_the_one_the_registry_presented`. Both are fencing.
- `test_vocabulary_snapshot.py::test_literal_matches_runtime_collection`, which is ADR 0041 Decision 6, cited by `docs/doc-authority.md:127`. Also the hold-reason lockstep tests (fail-closed holds).
- The `hasattr(panel_data_source, "_bot_statuses")` lines in `test_sqlite_roster_source.py`. They sit inside tests whose result assertion, that a retired bot is not on Home, is live.
- The fixture-backed `test_golden_batch_matches_the_committed_fixture`, which is sacred.

## What the cuts orphan

- **conftest and `fixtures.py`:** nothing. Neither whole-file cut (rows 3, 4–7) imports `fixtures.py` or a conftest fixture, and every fixture is still used by surviving files.
- **Module-level helpers inside surviving files**, found by an AST reference scan. Delete them in the same PR, since ruff does not flag an unused module-level function:
  - `test_panel_deploy_live.py`: `_override`, `_receipt`.
  - `test_panel_deploy_shadow.py`: `_account_posture_row`, `_receipt`.
- **Imports left unused**, which `ruff check` will flag:
  - `test_outcome_copy.py`: `pytest`, used only by row 1's `parametrize`.
  - `test_vocabulary_snapshot.py`: `ALL_VOCABULARY_CODES`, `OPERATOR_COPY`, `copy_for`.
  - `test_chart_projection.py`: `MAX_CHART_RANGE_MS`.
  - `test_history_batch_client.py`: `HISTORY_BATCH_INNER_TIMEOUT_S`.
  - `test_qualification_recorded_history.py`: `PolygonBar`, `recorded_bar_source`.
- **Dead test-double code:** `test_panel_router.py:143` `_Registry.preview_resume_admission` stubs a method production no longer has (see row 30). Deleting an uncalled stub method is cleanup, not a rewrite. Leave it if the cutting PR wants zero edits to surviving files.
- **App code:** none becomes test-only. `panel_profile_for` and `alpaca_panel_profile` still serve `/panel-profile`. `copy_for` still serves every projection.
- **`scripts/pr_shard_durations.json`:** drop the node ids of every killed test (the file holds 332 `v2panel` entries; for example `:2878-2879` are row 3's).
- **No golden fixture is touched.** FQ-001 and its test stay.

## Hazards the cutting PR must carry

1. **#2716 depends on two tests here.** The gates kill list cuts the `broker-v2-vocabulary-contract` CI job only while `test_vocabulary_snapshot.py::test_python_and_frontend_snapshots_are_byte_identical` and `::test_committed_snapshots_match_freshly_generated_output` survive. Both are kept. Rows 8–15 lean on the second one. With them gone, a vocabulary drift fails one byte-equality assert with a diff instead of a named code. That is the same coverage with a coarser message.
2. **No test will pin which headline a run's end selects.** Rows 1, 2 and 36–40 are every test that compares a duty-outcome headline to its words. This is what the owner's copy-pin ruling asks for, and the cutting PR must not backfill them. Coverage that every kind has words and that a stop reads as its custody proof stays.
3. **Re-check at your own SHA.** Kill lists age. Re-run the duplicate pointers in rows 5, 7, 9–12, 27, 32 and 41–43: the stronger test must still exist and still assert the named outcome.
4. **Lint after deleting.** Run `ruff check PythonDataService/tests/` to catch the orphaned imports above. Then grep each orphaned helper name before deleting it.
5. **Docs naming the vocabulary test file.** `docs/broker-clerk-fleet-authority.md:487`, `docs/doc-authority.md:127` and ADR 0041 (`:15`, `:60`) name `test_vocabulary_snapshot.py`, which stays. No doc names a killed test, so the docs link contract (`tests/contracts`) is unaffected.

## Pointers outside this area (not rows)

- **#2706 (routes):** cutting the `bot_chart_live` route and its alias (`app/routers/broker_v2_panel.py:946/960`, router-local `_live_chart`) also orphans `app/services/broker_v2_panel/panel_data_source.py:596` `get_live_chart` and `panel_chart_data_source.py:140` `get_live_chart`, which #2706 does not list. Two tests here enter only through them: `test_chart_projection.py::test_live_chart_before_session_open_is_empty` (`:758`) and `::test_live_chart_forwards_selected_resolution` (`:795`). Both go with that cut as kind 5. The live-snapshot path (`get_live_snapshot_parts`) shares `_build_live_chart_from_fills`, so check its coverage, for example `test_panel_router.py::test_live_snapshot_bootstrap_and_sse_share_one_versioned_document`.
- **#2724 (bot-runner tests):** `tests/services/bot_runner/test_deploy_only_lifecycle.py:54` asserts the retired `resume_existing`/`pause`/`continue_paused`/`preview_resume_admission` methods are absent. It is the same retired-Resume guard as row 30.
- **#2732 (Frontend broker specs):** the Frontend copy of the vocabulary snapshot (`Frontend/src/app/components/broker/v2-panel/lib/broker-v2-vocabulary.snapshot.json`) and any spec that pins its prose.
- **#2721 (fleet tests):** row 6 cites `tests/broker/fleet/test_compatibility_reads.py::test_legacy_panel_profile_is_closed_to_the_existing_broker_set` only as context. Row 6 is kind 1 on its own evidence, so it holds whatever #2721 decides about that file.

## Not reviewed

- **Full bodies of most tests.** I judged most tests from their docstring and their first one to four assert lines, which the AST extract truncated at 150–230 characters. I read full bodies only for the borderline rows. A test whose truncated asserts look behavioural but whose remaining asserts are pure copy would be missed. That risk is highest in `test_panel_projection.py` (96 tests, read at one assert each), `test_budget_deploy.py` (47) and `test_deploy_submissions.py` (39).
- **Cross-file duplicates inside the area, beyond the pairs named in "How this was judged".** I did not systematically diff these overlapping clusters:
  - Deploy gating across `test_deploy_stale_proof_demotion.py`, `test_strategy_catalog.py`, `test_deploy_scoped_route.py` and `test_dry_run_eligibility.py`.
  - Home and roster rows across `test_catalog_projection.py`, `test_sqlite_roster_source.py` and `test_panel_router.py`.
  - Lease and fence behaviour across `test_action_execution.py`, `test_sqlite_action_fence.py`, `test_cohort_flatten.py` and `test_panel_router.py`.

  These are all money path, so they stay unless a later pass proves a duplicate.
- **Duplicates outside the folder.** I did not compare these tests against `tests/services/`, `tests/routers/`, `tests/broker/fleet/` or `tests/broker/alpaca/clerk/`, which may test the same services one layer down. Those tickets (#2717–#2725, #2730) own their side.
- **Parametrized cases one at a time.** Each parametrized test was judged as a whole.
