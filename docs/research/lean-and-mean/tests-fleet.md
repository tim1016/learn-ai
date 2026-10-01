# Kill list: fleet tests

Ticket #2721, map #2700. This plans the cuts and makes none.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` on 2026-09-30). That is later than the map's charting SHA `87b8e261`.
- **Area:** `PythonDataService/tests/broker/fleet/`: 48 test files plus `conftest.py`, 557 tests.
- **Already owned elsewhere, so skipped here:**
  - #2702 (`dead-broker-rest`):
    - the identity-predicate assertions at `test_identity_and_errors.py:76-83`;
    - the lane-quiet reader repoints (`test_lane_quiet_transport.py` ×5, `test_lane_quiet_confirmation.py:395,410,589`, `test_schema_migration.py:697`). Those are repoints, not cuts.
  - #2706 (`dead-routes`):
    - `test_b_scoped_contracts.py::test_a_clerk_agent_still_answers_the_stranded_operator_mutations_unpinned`;
    - the compatibility-inventory trims at `test_lane_runtime.py:387,472`;
    - the coordinator live-verdict alias in `test_compatibility_reads.py`.

## Method

1. **Inventory.** An AST script listed every test in the area with its line count, docstring, assert expressions and `pytest.raises` blocks. A second pass flagged tests that compare against long string literals, read source text, count calls, use mocks, have no assertion, or are 8 lines or fewer.
2. **Candidates.** I read the source of about 40 flagged tests in full. Every row below comes from that read.
3. **Duplicates.** For each duplicate I named the surviving test and checked that it asserts the same outcome. Where the claim depends on app code, I read that code:
   - the catalog validator runs inside `production_provider_adapters()` (`app/broker/fleet_composition.py:36`);
   - the snapshot generators sort their output and write the adapter version (`scripts/regenerate_fleet_operation_catalog_snapshot.py:110-111`, `scripts/regenerate_fleet_refusal_vocabulary_snapshot.py:84-85`).
4. **External consumers.** I checked the scripts and CI that name fleet tests by node id. `PythonDataService/scripts/broker_fleet_conformance.py` selects 12 of them, and none of those is cut.

## Kill list

Kinds: 1 trivial, 2 copy or doc pinning, 3 duplicate, 4 mock theater, 5 retired feature.

### Copy pins (kind 2)

The routed refusal for a reconnecting lane differs from the generic 503 only in its words. These four tests assert those words. The directory state they describe is proven by the two tests that stay in the file: `test_the_beat_reports_reconnecting_then_what_the_reconnect_ended_in` and `test_a_reconnect_that_ends_final_is_reported_unavailable`.

| Path | Kind | Evidence |
|---|---|---|
| `test_reconnecting_lane_copy.py::test_a_reconnecting_lane_s_503_says_it_will_recover_on_its_own` | 2 | Asserts status 503, which the generic refusal also returns. Beyond that it only checks wording: a substring of the message (`"could not reach its broker when it started and is reconnecting"`) and the exact `next_step` sentence. |
| `test_reconnecting_lane_copy.py::test_a_bound_lane_whose_authority_is_unavailable_names_the_fix_and_promises_no_retry` | 2 | Wording only: `"will not retry on its own" in message`, the exact `next_step` sentence, and `"Retry" not in next_step`. |
| `test_reconnecting_lane_copy.py::test_a_reconnecting_lane_s_stream_open_tells_the_same_truth` | 2 | The stream-open twin. It asserts `"cannot open read_account yet"` and `"no restart is needed"` substrings. |
| `test_reconnecting_lane_copy.py::test_any_other_lane_503_keeps_the_generic_copy` | 2 | Asserts `"failed serving read_account with 503." in message` and `"reconnecting" not in message`. That is the generic sentence and nothing else. |

### Duplicates (kind 3)

| Path | Kind | Evidence: the stronger test that survives |
|---|---|---|
| `test_a2_alpaca_lane.py::test_the_composition_registry_maps_alpaca_and_nothing_else` | 3 | `test_import_isolation.py::test_the_fleet_packages_own_registry_is_not_the_production_composition` makes the same assertion, `set(production_provider_adapters()) == {"alpaca"}`, and also proves that the fleet package's own registry is empty. |
| `test_a2_alpaca_lane.py::test_live_verdict_declares_configuration_access_not_execution_readiness` | 3 | A one-line pin of the catalog field `readiness is CONFIGURATION_ACCESS`. `test_a2_alpaca_lane.py::test_live_verdict_stays_routable_for_an_activation_required_lane` proves the outcome the field exists for: an unactivated lane still routes and reaches `ACTIVATION_REQUIRED`. |
| `test_lane_go_live_operations.py::test_the_catalog_with_both_operations_still_validates` | 3 | Calls `validate_operation_catalog(production_provider_adapters()["alpaca"].operations())` and asserts nothing. `production_provider_adapters()` already runs that validator on every adapter (`fleet_composition.py:36`). So every test that builds the production composition validates the catalog, for example `test_import_isolation.py::test_the_fleet_packages_own_registry_is_not_the_production_composition`. |
| `test_lane_quiesce_operations.py::test_the_catalog_with_both_operations_still_validates` | 3 | Same body as the row above, with the same survivor. |
| `test_operation_catalog_snapshot.py::test_snapshot_operations_are_sorted_by_operation_id` | 3 | `::test_committed_snapshot_matches_freshly_generated_output` compares the file byte for byte with the generator's output, and the generator writes `dict(sorted(operations.items()))`. An unsorted committed file therefore already fails. |
| `test_operation_catalog_snapshot.py::test_adapter_version_matches_the_live_adapter` | 3 | Same survivor. The generator writes `alpaca.adapter_version` from the live adapter. |
| `test_operation_catalog_snapshot.py::test_manual_order_cancel_declares_a_path_converter_on_order_ref` | 3 | `test_b_scoped_contracts.py::test_the_manual_order_path_converter_routes` proves that a slashed order reference actually routes through the `:path` converter. The snapshot equality and byte-identity tests catch a converter rename on either side. |
| `test_refusal_vocabulary_snapshot.py::test_snapshot_reasons_are_sorted_and_cover_every_declared_code` | 3 | `::test_committed_snapshot_matches_freshly_generated_output` covers it. The generator builds `reasons` from `sorted(FLEET_REFUSAL_REASONS.items())` with the same `status_code` and `meaning`, so ordering, coverage and both fields are already pinned. |
| `test_internal_http.py::test_the_seam_module_imports_no_provider_surface` | 3 | Asserts that the word "alpaca" is absent from `internal_http`'s source. `test_import_isolation.py::test_no_fleet_module_sources_import_a_provider_implementation` AST-walks every `app/broker/fleet/*.py`, this module included, for provider imports. `::test_importing_the_spine_loads_no_provider_execution_module` proves the same at runtime. |
| `test_operation_catalog.py::test_alpaca_declares_the_seven_desk_reads_at_their_pinned_routes` | 3 | Restates seven `(method, path_template)` pairs. `test_operation_catalog_snapshot.py::test_the_declared_set_equals_the_live_catalog` pins method and path template for every operation. `test_b_scoped_contracts.py::test_b2_desk_reads_and_account_bound_run_evidence_route_through_the_lane` routes all seven paths and checks the identity echo on each. |
| `test_b_scoped_contracts.py::test_lane_reads_route_through_the_public_surface` | 3 | A 6-line check that one lane read returns 200 with its body. `::test_b2_desk_reads_and_account_bound_run_evidence_route_through_the_lane`, in the same fixture, proves forward plus identity echo across eight lane reads. |
| `test_b_scoped_contracts.py::test_the_catalog_stays_the_single_routing_contract` | 3 | Asserts unique operation ids, paths starting with `/`, and that some `custody_command` exists. Unique ids are enforced by the validator that runs on the production catalog (`fleet_composition.py:36`), and its refusal is proven by `test_operation_catalog.py::test_catalog_validation_refuses_malformed_operation_shapes`. Every path template is pinned by `test_operation_catalog_snapshot.py::test_the_declared_set_equals_the_live_catalog`. |
| `test_provider_conformance.py::test_the_two_fakes_canonicalize_the_same_raw_account_differently` | 3 | Calls the conftest fakes directly. `test_assignments.py::test_identical_raw_account_ids_coexist_across_providers` proves the same disagreement through the real reservation path (`'ACCT-1'` vs `'acct_1'`, two independent rows). |
| `test_audit_routing_receipts.py::test_service_known_clerk_id_still_returns_its_receipts` | 3 | `::test_service_clerk_id_excludes_another_lanes_receipts` covers this. It makes the same scoped read and asserts both that the requested lane's receipt is present and that the other lane's is absent. |
| `test_audit_routing_receipts.py::test_service_omitted_clerk_id_still_reads_every_lane` | 3 | The same survivor's positive control makes the unscoped read and asserts that both lanes' receipts come back. |
| `test_audit_routing_receipts.py::test_http_serves_receipts_when_fleet_service_is_installed_positive_control` | 3 | `::test_http_accepts_the_request_with_the_correct_secret_header` sends the same request to the same coordinator app with the secret and gets 200. It also asserts the exact receipt list, where this test asserts only that the list is non-empty. |
| `test_audit_routing_receipts.py::test_http_known_clerk_id_still_returns_200_with_its_receipts` | 3 | Same survivor. It already passes `clerk_id=lane.clerk_id` and asserts 200 with exactly that lane's receipt. |
| `test_audit_routing_receipts.py::test_http_rejects_the_wrong_secret_header` | 3 | `::test_http_rejects_the_request_without_the_secret_header` already proves the route carries the guard (403, reason, flat body). The wrong-value comparison belongs to the shared dependency, which is tested in `tests/test_data_plane_control_security.py` (`*_rejects_missing_or_wrong_secret`). See hazard 4. |
| `test_directory_and_aggregation.py::test_http_aggregate_directory_rejects_the_wrong_secret_header` | 3 | Same reasoning: `::test_http_aggregate_directory_rejects_the_request_without_the_secret_header` proves the wiring, and the guard's wrong-value branch is tested centrally. |
| `test_directory_and_aggregation.py::test_http_aggregate_directory_accepts_the_request_with_the_correct_secret_header` | 3 | `::test_http_aggregate_directory_returns_every_clerk_ok_true_when_all_project_cleanly` sends the same secret to the same route and gets 200 with real lanes. This test does it with an empty roster. |
| `test_history_batch_timeouts.py::test_http_lane_delivery_builds_its_client_with_the_operations_bound` | 3 | Captures the `read_timeout_s` handed to the client builder. `::test_lane_router_thread_the_operations_bound_through_to_http_delivery` asserts the same captured value through the full `LaneRouter` → `HttpLaneDelivery` path. |
| `test_history_batch_timeouts.py::test_http_lane_delivery_builds_the_default_operations_client_at_10s` | 3 | It passes `read_timeout_s=DEFAULT_INTERNAL_TIMEOUT_S` explicitly, so it runs the same branch as the row above with a different number. It does not exercise an undeclared timeout. Same survivor. |

### Trivial (kind 1)

| Path | Kind | Evidence |
|---|---|---|
| `test_drain_ceremony.py::test_no_release_proof_token_remains_in_the_fleet_package` | 1 | Its only check is `assert not hasattr(service_module, "RELEASE_PROOF_TOKEN")`, the absence of a deleted name. The release outcome stays proven by `::test_drain_wait_release_force_retire_is_a_complete_exit` and the `test_release_*` gate tests in the same file. |
| `test_operation_catalog_snapshot.py::test_bots_deploy_apply_stays_gone` | 1 | Its only check is that a retired operation id is absent from the snapshot. If the operation came back, `::test_the_declared_set_equals_the_live_catalog` and the committed-equals-generated test would force it into the snapshot diff. |
| `test_operation_catalog.py::test_the_fake_providers_declare_valid_catalogs` | 1 | Validates the two conftest fakes, which are test code. No app behaviour depends on it. |

## What the cuts orphan

- **`test_reconnecting_lane_copy.py`.** The four helpers at `:32-82` lose every caller: `_lane_answering_503`, `_router_over_a_lane_that_beat`, `_beat` and `_routed_read_refusal`. Their imports go too (`ClerkUnreachable`, `LaneRouter`, `DeliveryRequest`/`DeliveryResult`, as the cut leaves them). The two surviving tests use `_lane_boot` (`:163`). Consider renaming the file, because it no longer tests copy, but that is optional.
- **`test_history_batch_timeouts.py`.** `_probe_operation` (`:99`) is used only by the two cut tests. `_probe_delivery` stays, because the survivor uses it at `:174`.
- **Imports to re-check after the cut:**
  - `test_a2_alpaca_lane.py`: `_alpaca_operation`, if the live-verdict pin was its last caller, and `production_provider_adapters`.
  - `test_operation_catalog_snapshot.py`: `production_provider_adapters`.
  - `test_operation_catalog.py`: `fake_alpha` / `fake_beta`, and the `AlpacaProviderAdapter` local import.
  - `test_internal_http.py`: `inspect`.

  `ruff check` will name any that become unused.
- **`conftest.py`.** Nothing is orphaned. The fake providers and fixtures are still used by the conformance and spine tests.
- **Shard ledger.** `PythonDataService/scripts/pr_shard_durations.json` keeps the node ids of deleted tests. Drop the stale keys or regenerate (the same note appears on #2702 and #2706).

## Hazards the cutting PR must carry

1. **The conformance script selects fleet tests by node id.** `PythonDataService/scripts/broker_fleet_conformance.py:40-83` names 12 tests in this area. #2707 keeps the script and #2713 keeps its runbook. None of the rows above is in that list. That is also why the protocol-version duplicate stays (see Considered and kept). Re-run `grep -o 'test_[a-z_0-9]*\.py::test_[a-z_0-9]*' PythonDataService/scripts/broker_fleet_conformance.py` before deleting.
2. **#2716 depends on the snapshot tests.** The gates kill list removes the `broker-v2-vocabulary-contract` CI job because these four tests already do its job:
   - `test_operation_catalog_snapshot.py:95,100` (committed-equals-generated, Python-equals-Frontend);
   - `test_refusal_vocabulary_snapshot.py:116,123` (the same pair).

   This list keeps all four. Only their redundant neighbours go.
3. **Interaction with #2706's 13-operation cut.** `test_b_scoped_contracts.py::test_b2_desk_reads_and_account_bound_run_evidence_route_through_the_lane` iterates `/activities?current_session=true`, and `activities_read` is one of the operations #2706 cuts. If #2706 lands first, that path comes out of the survivor's loop. `test_alpaca_declares_the_seven_desk_reads_at_their_pinned_routes`, cut here, names `activities_read` too, so cutting it first removes one conflict.
4. **The guard's wrong-value coverage.** Before cutting the two wrong-secret rows, confirm that a test in `tests/test_data_plane_control_security.py` drives `require_data_plane_control_secret_always`, the dependency `app/routers/broker_clerks.py` declares, with a wrong header and gets 403. If only the non-`always` variant is covered there, keep one of the two rows.
5. **#2702's lane-quiet repoint touches survivors.** `test_lane_quiet_confirmation.py::test_the_retirement_gate_reads_the_scoped_answer_not_the_latest_one` and `::test_the_gates_read_never_returns_a_superseded_sessions_confirmation` contrast the scoped reader with `read_latest_lane_quiet_confirmation`, which #2702 calls test-only. Those are lease and fencing outcomes and they stay. The #2702 PR decides how they read once the latest-reader goes.
6. **Re-check at your own SHA.** This list was read at `6a4d7d39`. Re-read each row's test and its survivor before deleting it. A survivor that was edited or removed in the meantime turns that row into a keep.
7. **Run before pushing:** `ruff check PythonDataService/app/ PythonDataService/tests/` and `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet tests/test_data_plane_control_security.py`.

## Considered and kept

- **Money-path scenario tests**, all of them. These cover leases, epochs, confirmations, drains, retirement, reassignment, registry restore holds, routing receipts and "no redispatch", and "no bot was stopped" outcomes. That includes the `CALLS`-flagged tests in `test_offline_boot_rejoin.py`, which count bot stops to prove none happened. Many overlap in setup, but each pins a distinct race or ordering.
- `test_b_scoped_contracts.py::test_incompatible_protocol_versions_refuse_registration`. It duplicates `test_admission_probes_2026_09_13.py::test_an_agent_speaking_another_protocol_version_refuses`, but the conformance ceremony selects it by node id (hazard 1).
- `test_lane_go_live_operations.py` and `test_lane_quiesce_operations.py` field-by-field catalog pins. Readiness, idempotency and capability are not in the operation snapshot. The operations themselves (go-live release, stop-all-bots, account-quiet) are on the money path, so when unsure, keep.
- `test_history_batch_timeouts.py::test_outer_bound_is_strictly_larger_than_the_inner_bound` and `::test_both_bounds_exceed_the_fleet_default`. Constant relations, but nothing else catches an outer bound that fires before the inner one.
- `test_history_batch_operation.py::test_batch_provider_is_awaited_exactly_once_per_build_history_chart_call`. Its notice-only half is not covered by the HTTP call-count test.
- `test_probe_strings_stay_print_free.py`. A lint implemented as a test. The ★ gates rule keeps lint. See the map note below.
- `test_refusal_body_shape.py` accept-path controls. Each one is the positive control for a refusal-shape test on the same guard.
- `test_identity_and_errors.py::test_every_refusal_family_pins_reason_status_and_detail` and `::test_no_two_refusal_families_share_a_reason`. They pin the refusal wire contract: reason, status and body keys, not prose.
- `test_secret_absence.py`, `test_registry_contains_no_custody.py` and `test_import_isolation.py` (except the duplicate row above). Invariants no other test proves.

## Pointers outside this area

None found beyond what is already listed in hazards 2, 3 and 5. The docstring at `test_refusal_vocabulary_snapshot.py:7` names the CI job #2716 cuts; #2716 already lists the rewording.

## Not reviewed

- **Long scenario bodies.** I judged these from their docstrings and assert lines, not a full read:
  - `test_a2_alpaca_lane.py` (3,306 lines)
  - `test_drained_lane_resurrection.py`
  - `test_offline_boot_rejoin.py`
  - `test_fleet_cli.py`
  - `test_schema_migration.py`
  - `test_registry_recovery.py`
  - `test_restore_refuses_drained_lane.py`
- **Cross-file duplicates among those scenarios.** Several boot, re-registration and refusal scenarios in `test_a2_alpaca_lane.py`, `test_offline_boot_rejoin.py` and `test_drained_lane_resurrection.py` look alike by name. I did not diff their setups and assertions to prove one subsumes another. They are on the money path, so they stay until someone does.
- **Per-route secret-gate tests outside the two files above.** `test_attention_aggregation.py::test_the_route_rejects_a_request_without_the_secret_header` and the guard tests in `test_refusal_body_shape.py` were not compared against the central control-security tests.
- **`test_fleet_boot_lost_configuration.py`.** A log-warning test that I did not read.
- **Fixture usage counts in `conftest.py`.** Not computed. I checked only that the fakes keep callers.
