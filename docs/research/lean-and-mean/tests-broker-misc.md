# Kill list: IBKR, broker configuration and migration tests

Ticket #2722, map #2700. Plan only; nothing is deleted here.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` on 2026-09-30). The map was charted at `87b8e261`; every row below was checked at `6a4d7d39`.
- **Area:** `PythonDataService/tests/broker/ibkr/`, `broker/contract/`, `broker/capture/`, `broker/test_*.py`, `tests/broker_configuration/`, `tests/installation_migration/`, `tests/operator/`. That is 75 files, about 25K lines and about 860 test functions (more once parametrized).
- **Blocking list skipped:** everything #2702 (`dead-broker-rest`) lists, plus its owner addendum: test-only helpers follow their tests, so every seam #2702 kept stays kept. I also skipped every test #2706 (`dead-routes`) lists in this area: 11 of 26 in `tests/broker/test_brokers_router.py` (order-groups, activities, assets, clock).

## Method

1. A throwaway AST script listed every test with its line, docstring, and assert lines. I read every test at that level.
2. When a name or assert looked trivial, copy-pinned, duplicated or call-order-only, I read its source. I also read the test one layer up or down: unit to service to route, `decide()` to `resolve_worker_binding`, `_coerce_*` to `_ticker_to_quote`.
3. For each duplicate, the row names the stronger surviving test as `file::test`. For each cascade, I ran a `grep -rn` over `app/` and `scripts/`, excluding tests, and quote the result.
4. Sacred rules applied:
   - IBKR feed and Gateway tests stay unless they are trivial, pin wording, or duplicate a stronger test.
   - Money-path outcome tests stay. Call-order and wording checks in money-path files go only when another test proves the outcome.
   - When unsure on the money path, I kept the test.

Paths below are relative to `PythonDataService/tests/`.

## Kill list

### A. IBKR (feed and Gateway area)

| Item | Kind | Evidence |
|---|---|---|
| `broker/ibkr/test_models.py::test_option_quote_round_trips_via_json` | 1 | Dumps an `IbkrOptionQuote`, re-validates it and asserts equality. That only proves Pydantic works. |
| `broker/ibkr/test_models.py::test_chain_snapshot_keeps_quote_order` | 1 | Builds `IbkrChainSnapshot(quotes=[...])` and asserts the list reads back in the same order. No logic is involved. |
| `broker/ibkr/test_models.py::test_coerce_quote_strips_negative_and_nan_but_keeps_zero` | 3 | The same `_coerce_quote` outcomes through the live converter `_ticker_to_quote`: `broker/ibkr/test_market_data.py::test_negative_one_bid_ask_become_none`, `::test_zero_bid_is_preserved`, `::test_nan_quote_fields_become_none`. |
| `broker/ibkr/test_models.py::test_coerce_iv_treats_negative_as_none` | 3 | `broker/ibkr/test_market_data.py::test_negative_iv_in_model_greeks_falls_through_to_bid_greeks` proves the live path skips a `-1.0` IV. |
| `broker/ibkr/test_contracts.py::test_build_chain_contracts_returns_full_list_when_all_qualify` | 3 | `::test_build_chain_contracts_strips_none_placeholders` (same file) already proves that qualified contracts pass through `build_chain_contracts`. |

### B. Broker contract and the brokers router

| Item | Kind | Evidence |
|---|---|---|
| `broker/contract/test_errors.py::test_all_errors_subclass_broker_error` | 1 | `issubclass` checks on class declarations. `::test_http_status_mapping` already needs every class to carry `BrokerError.http_status`. |
| `broker/contract/test_errors.py::test_error_carries_message_broker_and_detail` | 1 | Asserts the constructor stores its three arguments. |
| `broker/contract/test_errors.py::test_rate_limited_carries_retry_after` | 3 | `broker/test_brokers_router.py::test_rate_limited_sets_retry_after_header` proves `retry_after_ms` reaches the caller as a header (`routers/brokers.py:154`). |
| `broker/contract/test_errors.py::test_rate_limited_retry_after_optional` | 1 | Asserts that a default is `None`. |
| `broker/contract/test_errors.py::test_error_carries_the_vendors_numeric_code_when_it_gave_one` | 1 | Asserts the constructor stores `code`. |
| `broker/contract/test_registry.py::test_resolve_unknown_broker_raises_with_detail` | 3 | `broker/test_brokers_router.py::test_unknown_broker_returns_404` proves the same thing at the route: a 404 with `detail.broker` and the message. |
| `broker/contract/test_registry.py::test_resolve_on_empty_registry_names_none` | 2 | Asserts that the word `"none"` appears in the detail string. |
| `broker/contract/test_registry.py::test_reset_clears_registrations` | 1 | The test-reset seam empties a dict. |
| `broker/contract/test_registry.py::test_process_singleton_is_stable_and_resettable` | 1 | Asserts `get_broker_registry() is get_broker_registry()`. |
| `broker/contract/test_models.py::test_account_snapshot_round_trips_snake_case` | 1 | `model_dump` echoes the declared field names. |
| `broker/contract/test_models.py::test_created_at_ms_is_nullable` | 1 | Passes `None` to a `T \| None` field and reads it back. |
| `broker/contract/test_models.py::test_position_carries_signed_quantity_and_unrealized_pl` | 1 | `quantity=-5.0, side="short"` are inputs, and the asserts read them back. Nothing is derived. |
| `broker/contract/test_models.py::test_order_defaults_to_no_events` | 1 | Asserts a `default_factory=list`. |
| `broker/contract/test_models.py::test_order_event_is_a_lifecycle_row` | 1 | Constructs the model and reads two inputs back. |
| `broker/contract/test_models.py::test_clock_evidence_is_vendor_shaped` | 1 | Same: construct and read back. |
| `broker/contract/test_models.py::test_limit_leg_carries_price_and_selected_tif` | 1 | Constructs a valid limit leg and reads its inputs back. The order rules are proven by the refusal tests beside it (`:166–:205`), which stay. |
| `broker/contract/test_models.py::test_capabilities_are_frozen_data` | 1 | Reads two inputs back and asserts `frozen=True` refuses a field assignment. That is a model-config pin. |
| `broker/test_brokers_router.py::test_positions_endpoint_returns_empty_list` | 3 | `::test_positions_endpoint_returns_list` (same file) proves the passthrough. An empty list is the same path. |

### C. Broker configuration

| Item | Kind | Evidence |
|---|---|---|
| `broker_configuration/test_alpaca_seams.py::test_the_adapters_satisfy_the_protocols_package_b_declared` | 1 | `isinstance` on runtime-checkable protocols. Pyright checks the same thing, and every other test in the file drives the adapters through the protocols. |
| `broker_configuration/test_binding_decision.py::test_a_recorded_apply_binds_the_staged_revision` | 3 | `broker_configuration/test_worker_binding.py::test_an_applied_revision_becomes_the_binding_when_the_prior_account_is_clear` proves the same binding end to end through `resolve_worker_binding`. |
| `broker_configuration/test_binding_decision.py::test_a_crash_with_a_staged_selection_boots_the_last_effective_revision` | 3 | `broker_configuration/test_worker_binding.py::test_a_crash_with_a_staged_selection_boots_the_last_effective_revision` is the same row of the acceptance matrix, driven end to end. |
| `broker_configuration/test_binding_decision.py::test_a_configured_installation_with_nothing_effective_is_not_unconfigured` | 3 | `broker_configuration/test_worker_binding.py::test_a_configured_installation_with_nothing_applied_closes_the_gate`: an `UnboundWorker` with `BROKER_UNCONFIGURED`. |
| `broker_configuration/test_binding_decision.py::test_an_installation_with_no_profiles_at_all_is_unconfigured` | 3 | `broker_configuration/test_worker_binding.py::test_an_installation_with_no_profiles_boots_from_the_environment` |
| `broker_configuration/test_binding_decision.py::test_an_ordinary_restart_of_an_unchanged_binding_writes_nothing` | 3 | `broker_configuration/test_worker_binding.py::test_an_ordinary_restart_does_not_advance_the_selection_generation` |
| `broker_configuration/test_binding_decision.py::test_the_credential_slot_is_never_what_decides_a_switch` | 1 | Asserts `"slot" not in str(signature(switch_verdict))`. That checks a parameter name, not an outcome. The switch-verdict tests beside it stay. |
| `broker_configuration/test_desk_state.py::test_worker_restart_command_names_the_declared_service` | 3 | `broker_configuration/test_routes.py::test_desk_state_authors_the_restart_command_from_the_declared_worker_service[compose-defaults-resolve-the-lane]`, the same command over HTTP. |
| `broker_configuration/test_desk_state.py::test_worker_restart_command_names_the_whole_declared_compose_context` | 3 | The same route test, `[the-lane-has-its-own-compose-context]`. It expects the identical `--project-name learn-ai-fleet -f compose.yaml -f compose.fleet.yaml --profile fleet` string. |
| `broker_configuration/test_desk_state.py::test_worker_restart_command_is_absent_when_the_deployment_declared_nothing` | 3 | `broker_configuration/test_routes.py::test_desk_state_reports_no_restart_command_when_no_worker_service_is_declared` |
| `broker_configuration/test_desk_state.py::test_desk_state_carries_the_declared_workers_restart_command` | 3 | The route test named two rows up (both params). |
| `broker_configuration/test_desk_state.py::test_desk_state_omits_a_restart_command_for_an_undeclared_worker` | 3 | `broker_configuration/test_routes.py::test_desk_state_reports_no_restart_command_when_no_worker_service_is_declared` |
| `broker_configuration/test_desk_state.py::test_projector_authors_the_live_choice_safety_copy` | 2 | Its only assert pins one exact sentence ("…Selecting this profile does not arm live trading."). |
| `broker_configuration/test_desk_state.py::test_desk_state_distinguishes_archived_profiles_from_a_fresh_installation` | 3 | `broker_configuration/test_routes.py::test_desk_state_distinguishes_archived_profiles_from_a_fresh_installation` asserts the same empty choices, message and action over HTTP. |
| `broker_configuration/test_legacy_environment.py::test_exactly_seven_settings_are_retired` | 1 | Asserts that `RETIRED_ENV_VARS` equals a literal tuple. The behaviour is proven by `::test_the_refusal_names_every_stale_variable` and tied to canon by `::test_the_retired_envelope_set_matches_the_canonical_required_set`. |
| `broker_configuration/test_legacy_environment.py::test_retired_and_never_retired_are_disjoint` | 3 | `::test_credentials_and_bootstrap_produce_no_refusal` sets every `NEVER_RETIRED_SETTINGS` name and asserts no refusal, so any overlap already fails it. |
| `broker_configuration/test_legacy_environment.py::test_the_credential_pair_is_never_retired` | 3 | `broker_configuration/test_retired_environment_gate.py::test_the_credential_pair_does_not_trip_the_gate` proves the same regression end to end through the worker boot. |
| `broker_configuration/test_prior_obligations.py::test_the_probe_satisfies_the_preflight_protocol` | 1 | `isinstance` on a protocol. |
| `broker_configuration/test_prior_obligations.py::test_observe_describes_several_bindings_in_readable_prose` | 2 | The docstring calls it out: "The refusal is operator copy". Its one assert pins the phrasing. The not-clear outcome tests in the file stay. |
| `broker_configuration/test_profiles_service.py::test_nickname_for_returns_the_set_nickname` | 3 | `::test_nickname_for_reflects_a_rename_immediately` (same file) asserts the set value before it renames. |
| `broker_configuration/test_profiles_service.py::test_listing_hides_archived_profiles_unless_asked` | 3 | `broker_configuration/test_routes.py::test_archived_profiles_are_hidden_unless_requested` |
| `broker_configuration/test_selection_service.py::test_staging_with_a_stale_generation_conflicts` | 3 | `::test_two_tabs_cannot_silently_clobber_a_staged_selection` (same file) proves the refusal and that the staged selection is untouched. `broker_configuration/test_routes.py::test_a_stale_staging_generation_returns_a_conflict` proves it over HTTP. |

### D. Installation migration

| Item | Kind | Evidence |
|---|---|---|
| `installation_migration/test_contents.py::test_each_known_skipped_token_says_why_it_needs_nothing_from_the_operator` | 2 | Four substring pins on `skipped_secret_note` prose: "nothing reads it", "copy it by hand". The skip behaviour is proven by `::test_the_auth_tokens_under_artifacts_are_secret_shaped_and_skipped` and `installation_migration/test_export.py::test_a_secret_inside_a_bundled_folder_is_skipped_and_listed`. |
| `installation_migration/test_tree.py::test_sha256_file_is_the_hex_digest_of_the_bytes` | 1 | Pins hashlib's digest of `b"abc"`. Digest verification is proven by `installation_migration/test_import.py::test_import_fails_loudly_on_a_tampered_member`. |
| `installation_migration/test_export.py::test_export_reads_every_account_before_it_stops_a_single_bot` | 4 | Its only assert is the fake lane client's call sequence (`["quiet","quiet","stop","stop","quiet","quiet"]`). The outcome is proven by `::test_export_refuses_a_non_flat_account_before_stopping_any_bot` (nothing stopped) and `::test_an_account_that_goes_non_flat_after_the_stop_is_refused_saying_bots_were_stopped` (the re-read). |

### E. Kind 5 that #2706 missed (these go with its route cut)

#2706 lists `GET`/`PATCH /api/brokers/alpaca/configuration/owner` and `GET …/configuration/events` as dead routes. Its test table names no test in `tests/broker_configuration/`. These tests drive those routes:

| Item | Kind | Evidence |
|---|---|---|
| `broker_configuration/test_routes.py::test_owner_is_readable_and_renameable` | 5 | Calls `GET` and `PATCH {PREFIX}/owner` (handlers at `app/routers/broker_configuration.py:183,188`). |
| `broker_configuration/test_routes.py::test_the_events_route_pages_the_audit_log` | 5 | Calls `GET {PREFIX}/events` (`:443`). |
| `broker_configuration/test_routes.py::test_every_route_requires_the_control_secret`: the `("GET","/owner")`, `("GET","/events")` and `("PATCH","/owner")` param rows | 5 | Drop the three rows. The other four rows stay. |
| `broker_configuration/test_profiles_service.py::test_renaming_the_owner_keeps_the_owner_id` | 5 (cascade) | `service.rename_owner` (`app/broker_configuration/service.py:257`) has one caller: the dead `patch_owner` route. `grep -rn "rename_owner" app scripts` finds only the router. |
| `broker_configuration/test_profiles_service.py::test_the_event_log_records_every_change_newest_first`, `::test_the_event_log_pages_backwards` | 5 (cascade) | `service.events` (`service.py:829`) and `store.list_events` (`store.py:605`) are read only by the dead `list_events` route. Event *writes* stay. |

The two service methods and `store.list_events` are dead code that #2706's cut leaves behind. A dead-code owner should list them (see "Pointers outside this area").

### F. Cascade that no dead-code ticket owns (decide the owner first)

These tests prove behaviour of code that becomes unreachable once a listed cut lands. No dead-code ticket lists the code, so I list the tests conditionally. They go with the code, in the same PR.

**F1. IBKR capability probe and evidence recorder** (gated on #2706's `broker_capability` router and `ibkr/evidence` route cuts)

| Item | Kind | Evidence |
|---|---|---|
| `broker/ibkr/test_capability.py` (whole file, 5 tests) | 5 (cascade) | `app/broker/ibkr/capability.py` is imported only by `app/services/market_data_capability_service.py:10`. That module is imported only by `app/routers/broker_capability.py:13`, which #2706 cuts. |
| `broker/ibkr/test_api_evidence.py` (whole file, 8 tests) | 5 (cascade) | The recorder's read side (`backfill`, `subscribe`, `clear`) is read only by `app/routers/broker.py:105–118` (the dead evidence routes) and `capability.py:423,430` (above). After both cuts, `IbkrApiEvidenceRecorder` becomes a sink that is written and never read. The four `evidence_response` serializer tests prove what goes into that sink. |

This is not the feed. `bars.py`, `market_data.py`, `contracts.py` and `surface.py` call `get_ibkr_api_evidence_recorder().record(...)`. Removing those calls edits feed files, so the dead-code owner must run the full feed suite (hazard 2).

**F2. The operator-notice vocabulary** (gated on #2702 section A and #2703's `mutation_rung_receipts.py` cut)

#2702 kept `OperatorNotice` and `OperatorNoticeAction` "because `app/schemas/live_runs.py` uses them". That premise is half wrong:
- `live_runs.py:34–39` imports only `OperatorNoticeAction`, `…Actionability`, `…RemedyStatus`, `…Tier` and `validate_actionability_action_pairing`, and its only consumer is `MutationRungReceipt`.
- `MutationRungReceipt`'s only user is `app/services/mutation_rung_receipts.py`, which #2703 cuts. `grep -rn MutationRungReceipt app scripts` finds nothing else.
- `OperatorNotice`, `OperatorNoticeCode`, `NOTICE_CODE_CONTRACTS` and `app/operator/notices/snapshot.json` have no importer outside `app/operator/` except the dead `incidents/safety_halt_notices.py`.
- `snapshot.json`'s only reader is `Frontend/src/app/api/operator-notice-codes.snapshot.spec.ts`, which #2709 cuts (A15).

So once those cuts land, all of `app/operator/notices/` is dead, and so is `MutationRungReceipt`.

| Item | Kind | Evidence |
|---|---|---|
| `operator/test_notice_schema.py` (whole file: the 21 tests #2702 does not already own) | 5 (cascade) | Every test targets `app/operator/notices/schema.py`. |
| `operator/test_notice_codes_snapshot.py` (whole file, 2 tests) | 5 (cascade) | It guards `snapshot.json` against the schema. After #2709 drops the TS mirror, the file it guards has no reader. |
| `operator/_helpers.py` | orphan | Used only by the operator tests above and the two #2702 deletes. |

**If the map keeps the notice package anyway**, these still fail the bar on their own:
- `test_tier_literal_is_three_values`, `test_actionability_literal_is_four_values`, `test_remedy_status_literal_is_two_values` and `test_action_kind_is_six_values` are kind 1 (each asserts that a `Literal` has a fixed member set).
- The eight `test_code_literal_declares_*_slots` tests are kind 3: `operator/test_notice_codes_snapshot.py::test_operator_notice_code_snapshot_matches` pins the whole union exactly.
- `test_notice_round_trips_through_pydantic` is kind 1.

## What the cuts orphan

- **Test-only constant stays.** `NEVER_RETIRED_SETTINGS` (`app/broker_configuration/legacy_environment.py:123`) loses two of its three test readers but stays, because `test_credentials_and_bootstrap_produce_no_refusal` survives.
- **No other app code is orphaned by A–D.** I checked each test-only seam the rows touch:
  - `restart_target` (conftest) stays, because `test_routes.py` uses it.
  - `worker_restart_command` stays, because `desk_state` calls it.
  - `get_broker_registry().reset()` stays, because every router test uses it.
- **Fixtures and helpers.**
  - `broker/ibkr/_support.py` stays (`test_auto_reconnect_monitor.py` and `test_connect_outage_budget.py` use it).
  - `installation_migration/_support.py` stays.
  - `broker_configuration/conftest.py` stays.
  - F2 orphans `operator/_helpers.py`.
- **Section E/F code** (for the dead-code owner):
  - `service.rename_owner`, `service.events`, `store.list_events`.
  - `capability.py`, `market_data_capability_service.py`, `schemas/broker_capability.py`.
  - The recorder's read side, or the whole recorder.
  - `app/operator/notices/` with `snapshot.json`, and `MutationRungReceipt` (`schemas/live_runs.py`).
- **Shard ledger.** `PythonDataService/scripts/pr_shard_durations.json` keeps the node IDs of deleted tests. Regenerate it or drop the stale keys (#2707/#2716).

## Hazards the cutting PR must carry

1. **Re-check at your own SHA.** Re-run each row's duplicate check: open the named survivor and confirm it still asserts the same outcome. For E and F, re-run the caller grep.
2. **IBKR feed.**
   - Rows in A touch only model, coercion and contract-builder unit tests. No feed or Gateway test is cut.
   - F1 edits feed files if the recorder's `record()` calls are removed. Afterwards run `pytest tests/broker/ibkr tests/marketdata tests/structural`.
   - `IBKR_BROKER_ENABLED` is load-bearing, so never flip it.
3. **Corrections to #2702 that the handoff must read.**
   - #2702 says "delete its cases in `test_option_search.py`" for `build_option_contract`. No test there calls it. Only the module docstring mentions it (`test_option_search.py:3,7`), so the cut is a docstring edit.
   - For `CaptureJournal.records_written` (#2702 E, "#2720 judges"), the answer is *delete the assertion lines*, no repoint. Each touched test keeps a file-based assert that proves the same count:
     - `capture/test_journal.py::test_multiple_records_append_to_same_day_file` keeps `len(_read_records(path)) == 3`.
     - `::test_unsafe_broker_component_is_nonfatal_and_counted` keeps `rglob("*.jsonl") == []`.
     - `::test_record_round_trips_verbatim_utf8_body` reads its record back.
   - #2702's premise for keeping `OperatorNotice` is wrong (F2).
4. **Money path.** The binding-decision rows (C) cut the pure `decide()` and `switch_verdict` copies of rows that `test_worker_binding.py` drives end to end. Keep every switch-verdict and acknowledgement test that has no end-to-end twin; they are listed under "Considered and kept".
5. **`test_routes.py` param edit.** `test_every_route_requires_the_control_secret` loses three param rows. Delete the rows only; the test stays.
6. **Structural guard.** `tests/structural/test_ibkr_feed_boundary.py` names `app.operator.*` modules by string. Lines `:162-163` and `:297-298` sit in its lists of already-retired modules, and none of those four modules exists today. Deleting the package (F2) keeps those entries true. Before the cut, check whether the test also names a *surviving* `app.operator` module anywhere (#2731 owns the test).

## Considered and kept

- **IBKR feed and Gateway suites.** Everything in `test_bars.py`, `test_minute_assembler.py`, `test_auto_reconnect_monitor.py`, `test_market_liveness.py`, `test_client.py`, `test_client_connection_generation.py`, `test_connect_log_budget.py`, `test_connect_outage_budget.py`, `test_health.py`, `test_recovery_state_machine.py`, `test_surface.py`, `test_router.py` and `test_market_data.py` stays.
  - The log-level tests (`test_live_idempotent_skip_logs_at_info_not_warning`, `test_on_ib_error_connectivity_lost_logs_at_info_not_warning` and the others) are not wording pins. The temporal rule requires a redelivery to be "surfaced, never silenced", and a log classifier anchors on the `(logger, message)` pair.
  - `test_the_grace_leaves_the_decision_allowance_room` pins a cross-constant invariant (8 s grace inside the 20 s allowance), not one constant.
- **`test_config.py` port and mode guards** (paper/live port mismatch) are live-safety tests.
- **`test_brokers_router.py::test_generic_order_cancel_route_is_retired_for_every_broker`** stays. It proves that no cancel can bypass the clerk ("no order was sent" is an outcome).
- **`test_errors.py::test_a_not_permitted_order_is_an_order_rejection_surfaced_as_409`** stays. It is money-path adjacent and I was unsure.
- **`test_export.py::test_export_stops_bots_before_it_stops_containers_clerks_first_postgres_last`** stays. It is a call-order check, but no other test proves the shutdown order, which keeps Postgres clean. Money path, unsure, so kept.
- **The remaining `test_binding_decision.py` tests** have no end-to-end twin and stay: the switch verdicts for an unrecorded account, the same account under a different revision, a different account and an unpinned candidate; and the apply acknowledgement.
- **Parity tests for deliberate duplicates** stay (CLAUDE.md #5): `test_clerk_dir_parity.py`, `test_legacy_environment.py::test_legacy_values_*` and `installation_migration/test_facts.py::test_the_clerk_database_layout_matches_the_canonical_writer`.
- **`test_podman_adapter.py` argv asserts** stay: the adapter's whole job is the command line.

## Pointers outside this area

- **#2702 / #2703 (or a new dead-code owner):** the F2 cascade (`app/operator/notices/`, `MutationRungReceipt`).
- **#2706 (or a new owner):**
  - The section E service and store methods.
  - The F1 capability module and capability service.
  - The evidence recorder's read side.
- **Unread IBKR config key.** `IbkrSettings.account_gate_authority` (`app/broker/ibkr/config.py:152`) has no reader (`grep -rn account_gate_authority app scripts` finds only its definition). It looks like a bot-control leftover. `test_config.py::test_defaults_are_paper_on_paper_port` and `::test_uppercase_ibkr_env_vars_are_honored` each assert it once; drop those asserts when the key goes. #2702 did not audit config keys (its "Not reviewed").

## Not reviewed

- **Depth.** I read every test by name, docstring and assert lines, and read the full source only for candidates. These money-path files were judged at that level and nothing was flagged: `test_developer_reset.py`, `test_store_concurrency.py`, `test_worker_lifecycle.py`, `test_paper_extended_hours_allowances.py`, `test_envelope_type_fidelity.py`, `test_cutover_rehearsal.py`, `test_go_live*.py`, `test_import.py`, and most of `test_prior_obligations.py`. Duplicates inside them that only a full read would reveal may remain.
- **Duplicates across tickets.** I compared layers only inside my area. I did not compare `broker/ibkr/test_market_liveness.py` with `tests/broker/alpaca/` liveness tests (#2720), or `installation_migration/test_coordinator_lanes.py` with the fleet tests (#2721).
- **`test_selection_service.py::test_a_stale_worker_cannot_publish_itself_as_effective`** versus `test_worker_binding.py::test_a_stale_worker_cannot_publish_itself_as_the_effective_runtime`. This looks like a cross-layer duplicate on the fencing path. I did not read both closely enough to cut one, so I kept both.
