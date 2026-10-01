# Kill list — dead code in the Alpaca clerk (#2701)

Part of map #2700. Area: `PythonDataService/app/broker/alpaca/clerk/` (149 modules, ~65.6K lines).

**Read at `87b8e261`** (`origin/master` on 2026-09-30; the same SHA the map was charted at). Plan, don't cut.

Paths below are relative to `PythonDataService/app/broker/alpaca/clerk/` unless they start with `PythonDataService/`, `docs/` or `contracts/`. Tests are relative to `PythonDataService/tests/`.

## How this was judged

1. **Module reachability.** An AST import graph over every tracked `.py` file (lazy and `TYPE_CHECKING` imports count as edges, which errs toward keeping things). Roots: `app.main`, every non-test script under `PythonDataService/scripts/` and `scripts/`, the top-level `PythonDataService/*.py` files, `lean_sidecar`. Result: every clerk module is reachable from a root except `sqlite/repository_boundary.py` (only a test imports it). Eleven modules are reachable **only** from scripts (see Pointers).
2. **Symbol census.** For each top-level function, class, constant and method in the clerk, every non-test reference to its name was collected: Python identifiers, attribute names, keyword names and exact string constants (dispatch by name), plus whole-word matches in `.ts`, `.cs`, `.yaml`, `.yml`, `.sh`, `.toml`, `.ini`, `.cfg` and `.html` files across `PythonDataService/`, `Backend/`, `Frontend/`, `scripts/`, compose, deploy and `.github/`. `__all__` entries and package re-exports in `__init__.py` were not counted as uses. Each reference was tied to the function or method it sits in. A symbol was dead when every reference came from code that was already dead, repeated until nothing changed. Pydantic validators, properties the framework calls, and module `__getattr__` hooks were left out.
3. **Every candidate was then checked by hand**: what it does, the full repo search for its name, the git history where it mattered, and which tests touch it.

Names are compared as plain text, so two symbols with the same name share one count. That can only hide dead code, never invent it: the economy reader's cursor helpers were hidden this way, behind same-named helpers in `timeline_query.py` and `external_orders.py`. A symbol missing from this list is **not proven live**.

**Test seams are not dead code.** Many symbols with only test callers are read accessors or one-line wrappers that tests use to *observe live behavior*: an observation, a mirror's finalize state, a FIFO snapshot. Deleting them would mean rewriting the tests that use them to check live money-path outcomes, and rewriting tests is out of scope for the map. They are listed under **Kept: test seams**, not as rows.

## Kill list

### A. Dead production code with no tests

| Path | Kind | Evidence |
|---|---|---|
| `exit_terms.py::registered_exit_terms_at` (30–40) | function | Zero references anywhere outside its own definition, tests included. The one live reader is `read_exit_terms` (`exit_terms.py:24`). |
| `fleet_adapter.py::_EXECUTE`, `::_READ` (83–84) | constants | Assigned and never read. Their siblings `_CONFIGURATION`, `_DURABLE` and `_ONE_SHOT` are read. |
| `models.py::AlpacaEffectOperation.entry_leg` (83–87) | property | Zero references repo-wide. |
| `models.py::OrderJournalEntry.attributed_from` (294–340) | classmethod | Zero references repo-wide. |
| `models.py::UNEXPLAINED_ORDER_HOLD_CODE` (171), `::STREAM_HEALTH_HOLD_CODE` (178) | constants | Never imported. The canonical copies live at `sqlite/uncertainty_causes.py:36-37`, and a third copy of the wire value at `stream_health.py:34` is used. |
| `sqlite/commands.py::_ALREADY_ACTIVE_REASON` (77) | constant | Never read. |
| `sqlite/custody_schema_contract.py::MANUAL_LEG_SUBJECT_COMPATIBILITY_DDL` (424–426) | constant | Never read. `schema.py:38,448` uses only the `_V10_` variant, and `schema.py:636,647` uses `MANUAL_LEG_IDENTITY_V11_DDL` directly. Removing it changes no DDL. |
| `sqlite/cutover_initialization.py::require_completed_initialization` (194–258), `::read_initialization_record` (261–271) | functions | No callers and no tests. These are dead copies of the live private pair `sqlite/cutover.py::_require_completed_cutover_initialization` (351) and `::_read_initialization_record` (418). |
| `sqlite/execution_coverage.py::direct_cumulative_recovery_fill_for_order` (920–943) | function | No callers and no tests. It is the narrower #1554 admission rule; the set proof replaced it. |
| `sqlite/reads.py::external_orders_observed_since` (429–441) and `sqlite/repository_read_api.py::ClerkSqliteRepositoryReadApi.external_orders_observed_since` (488–491) | function + facade method | No production callers and no tests. The facade method is the reader's only caller. |
| `sqlite/runtime.py::MissingEntryCustodyError` (317–318) | exception class | Never raised or caught. Only a docstring mentions it (`services/bot_runner/test_trade_bot_liveness_gating.py:180`). |
| `sqlite/economic_projection.py::SqliteEconomicProjectionReader.account_net_cash_spent_usd` (674–708) | method (math) | No production caller. Its provenance block cites `test_day_pnl.py::test_net_cash_spent_is_buys_less_sells_over_every_subject`, and no such test exists at this SHA. The shadow cash rehearsal it served no longer calls it. |
| `sqlite/economic_projection.py::order_decisions` (444–455), `::exit_execution_evidence` (463–483), `::missing_exit_execution_evidence` (457–461) and `synthesized_orders.py::SynthesizedOrderLedger.read_latest_beside_database` (204–217) | methods (a chain) | No tests. `missing_exit_execution_evidence` has no callers, and each method below it is called only by the one above. This was the old shadow-evaluation evidence read. |

### B. Dead production code and the tests that exist only for it

| Path | Kind | Evidence |
|---|---|---|
| `sqlite/process_repositories.py` (whole module, 56 lines) | module | `get_or_open_repository` is the only thing that fills `_repositories`, and it has no production caller: `tests/contracts/test_alpaca_active_authority_wiring.py:81` even asserts `main.py` never calls it. So the shutdown call at `PythonDataService/app/main.py:1110-1114` always closes an empty dict. `reset_for_testing` has zero callers. |
| `broker/alpaca/clerk/sqlite/test_repository.py::test_process_repository_shutdown_closes_every_cached_handle_after_one_failure` (61) | test | It fills the private cache by hand, then checks the shutdown on it — a path production never hits. |
| `sqlite/decision_receipts.py::SqliteDecisionReceipts.update_final_outcome` (555–570), `sqlite/repository.py::ClerkSqliteRepository.update_decision_receipt_for_bar` (1400–1426, plus its import at 48), `sqlite/decision_receipts.py::update_decision_receipt_for_bar` (396–438) | write path (3 functions) | Nothing in production replaces a receipt with its final outcome. The only `UPDATE decision_receipts` statement (`decision_receipts.py:420`) is reached only through `update_final_outcome`, and `update_final_outcome` has no production caller. |
| `broker/alpaca/clerk/sqlite/test_decision_receipts.py::test_final_outcome_replaces_the_provisional_receipt_without_new_sequence`, `::test_new_evidence_walk_sees_an_older_receipts_final_outcome` | tests | Kind 5. Both set up a final-outcome rewrite that production cannot perform. |
| `broker/alpaca/clerk/sqlite/test_repository_writer_boundary.py::test_writer_census_detects_decision_final_outcome_updates` (224) and the `update_final_outcome` clause of `_writer_call_name` (134) | test + helper branch | They pin detection of a writer call that no production code makes. |
| `sqlite/decision_receipts.py::SqliteDecisionReceipts.by_transaction` (595–608), `sqlite/repository_read_api.py::decision_receipts_by_transaction` (420–433), `sqlite/reads.py::decision_receipts_by_transaction` (282–295) | read chain | No production caller at the top of the chain. |
| `broker/alpaca/clerk/sqlite/test_decision_receipts.py::test_by_transaction_matches_intent_or_order_and_stays_bounded` (285) | test | Kind 5: it exercises only the dead read. |
| `sqlite/repository.py::ClerkSqliteRepository.raise_uncertainty_if_none_active` (1480–1510) | method | No production caller. Production raises uncertainty through other entrances. |
| `broker/alpaca/clerk/sqlite/test_reconcile.py::test_raise_uncertainty_if_none_active_serializes_two_concurrent_callers` (1401, with its `worker_a`/`worker_b`), `broker/alpaca/clerk/sqlite/test_repository.py::test_raise_uncertainty_if_none_active_is_idempotent` (943) | tests | Kind 5: they test only the dead method. |
| `sqlite/external_orders.py::SqliteExternalOrderReader` (408–553), `::ExternalOrderPage` (61–68), `::InvalidExternalOrderCursor` (54–55), `::ExternalOrderLifecycleState` (58), and `sqlite/reads.py::external_order_page` (382–426) | class + its helpers | No production reference to the reader class. Each of the other symbols is used only by the reader. External-order holds are still folded and acknowledged by live code in the same module (`observe_external_order` and the others). |
| `broker/alpaca/clerk/sqlite/test_reconcile.py::test_external_order_reader_paginates_durable_observations_with_account_scoped_cursor` (865), `::test_external_order_cursor_survives_a_later_broker_snapshot_update` (888), `::test_external_order_reader_filters_review_state_without_cross_filter_cursor_reuse` (936) | tests | Kind 5: they test only the dead reader's paging. |
| `sqlite/economic_projection.py::SqliteEconomicProjectionReader.from_database_path` (231–258), `::runs_for_strategy` (485–495) | methods | No production caller. They were the paper-twin reconciliation opener and its run read. |
| `broker/alpaca/clerk/sqlite/test_economic_projection_openers.py` (whole file, 2 tests) | test file | Both tests exercise only those two dead methods. |
| `broker/alpaca/clerk/sqlite/test_economic_projection.py::test_pages_are_keyset_bounded_and_account_rows_preserve_effective_state` (978) | test | Kind 5: it checks cursor paging of `bot_fills` / `account_executions`. No production code pages; live reads use `bot_fill_window` / `account_fill_window`. The two methods stay as a seam for the golden test (see Kept). |
| `synthetic_broker.py::SyntheticBarBindingError` (80) | alias | It is the sim world's old name for `SynthesizedBarBindingError`. Only `services/test_source_bar_ledger.py:16,249` uses it, and that import moves to the canonical name. |
| `recovery_reduction.py::RECOVERY_BAND_ALLOWANCE_MULTIPLE` (119–121) | alias constant | It equals `DEFAULT_EXIT_BAND_MULTIPLE`, and no production code reads it. Its only use is a tautological equality assert at `broker/alpaca/test_marketable_limit.py:194,203`, inside `test_allowance_converters_keep_defaults_legacy_knobs_only_apply_at_upgrade`. That assert and the import go; the test stays. |

### C. A correction path that was never wired: `EXECUTION_CORRECTED`

No production code has ever constructed an `EXECUTION_CORRECTED` custody transition. `git log -G EXECUTION_CORRECTED -- PythonDataService/app` returns exactly one commit, c559e7c6 (#1454). Its additions to `app/` are the fold, its registration, a kind check inside the writer, a docstring and a panel label. None of them builds such a transition. The only writer is `append_execution_correction_or_raise`, and only tests call it. So no live, paper, Dry Run, sim or shadow store can hold such a row, and the fold that would replay one cannot run.

Live broker corrections take a different path. A changed redelivery raises uncertainty (`repository.py:659`, `reads.correction_uncertainty_exists`). The effective-leaf SQL over `fills.superseded_execution_ref` stays: the supersession fold writes it too (`sqlite/execution_coverage_supersession_fold.py:212`).

| Path | Kind | Evidence |
|---|---|---|
| `sqlite/repository.py::append_execution_correction_or_raise` (937–975), `::_execution_correction_invalid_reason` (977–1010) | writer | No production caller since #1454. |
| `sqlite/folds.py::_fold_execution_corrected` (1240–1323) and its `DEFAULT_FOLD_REGISTRY.register("EXECUTION_CORRECTED", …)` line (1636) | fold + dispatch-by-name entry | It is reachable only by replaying an `EXECUTION_CORRECTED` row, and none can exist (above). See hazard H1. |
| `sqlite/facts.py::ExecutionCorrectedFacts` (911–932), `::validate_execution_corrected_facts` (1379–1402) | facts model + validator | Used only by the writer and the fold above. |
| `broker/alpaca/clerk/sqlite/test_folds_execution.py::test_execution_correction_replaces_effective_quantity` (319), `::test_execution_correction_invalid_target_raises_uncertainty` (444), `::test_execution_correction_missing_target_symbol_raises_uncertainty` (506), and the `_correction_transition` helper (181) | tests | Kind 5: the correction is what each test is about. |
| `broker/alpaca/clerk/sqlite/test_economic_projection.py::test_a_correction_that_explains_the_difference_clears_the_conflict` (1530), `::test_late_correction_keeps_root_execution_time_for_fifo_and_session_window` (813), `::test_execution_page_cursor_is_rejected_after_effective_fill_revision` (1010), and the `_append_correction` helper (154) | tests | Kind 5: a correction is the scenario under test. |
| `broker/alpaca/clerk/sqlite/test_fee_evidence.py::test_corrected_fill_reprices_original_fee_day` (982) | test | Kind 5. |
| `broker/alpaca/clerk/sqlite/test_manual_orders.py::test_manual_execution_correction_replaces_only_manual_custody` (998) | test | Kind 5. |
| `broker/alpaca/clerk/sqlite/test_historical_execution_recovery.py::test_timeline_execution_filter_includes_a_correction_of_that_execution` (338) | test | Kind 5. |
| `broker/alpaca/clerk/sqlite/test_envelope_reservations.py::test_a_corrected_fill_reserves_at_its_restated_size` (606), `::test_a_legacy_remainder_a_correction_reopens_refuses_the_next_enter_under_its_own_code` (942), and the `_append_correction` helper (553) | tests | Kind 5. |
| `PythonDataService/tests/fixtures/golden/alpaca-sqlite-execution/downward_correction/` (`input.json`, `expected.json`, `attribution.md`) and its entry in `_FIXTURE_FAMILIES` / `_S2_FAMILIES` plus the `_correction_transition` helper (176) in `broker/alpaca/clerk/sqlite/test_execution_authority_golden.py` | golden fixture family | Dead beats sacred: it replays a correction that production can never record. The other four golden families stay. |

Tests where a correction is one step inside a broader live scenario go to hazard H2, not into these rows.

## What the cuts orphan

- `PythonDataService/app/main.py:1110-1114`: the `close_all_repositories()` shutdown block (process_repositories).
- `tests/contracts/test_alpaca_active_authority_wiring.py:81`: the assert `"get_or_open_repository" not in source` becomes vacuous. Drop that line.
- `tests/broker/alpaca/clerk/sqlite/test_repository.py:21`: the `process_repositories` import.
- `tests/broker/alpaca/clerk/sqlite/test_decision_receipts.py:349-350`: the `receipts.by_transaction("")` assert inside `test_write_rejects_missing_strategy_and_reader_rejects_invalid_bounds`. The rest of that test proves live write and tail bounds and stays.
- `tests/broker/alpaca/clerk/sqlite/test_reconcile.py:42-43`: the `InvalidExternalOrderCursor` / `SqliteExternalOrderReader` imports.
- `__all__` entries in `sqlite/external_orders.py` and `sqlite/economic_projection.py` for removed names.
- Imports left unused in `cutover_initialization.py`, `repository.py`, `folds.py`, `facts.py`, `reads.py`, `models.py` and `synthetic_broker.py`. Project-scope ruff finds them.
- `PythonDataService/scripts/pr_shard_durations.json`: stale ids for the deleted tests. Regenerate with `scripts.update_pr_shard_durations`, or confirm the sharder ignores unknown ids.
- `tests/broker/alpaca/clerk/test_authority_isolation.py` parametrizes over sole-authority module names (shard ids such as `[recovery_status…]`). Deleting `process_repositories.py` drops one case. Check that the list is computed, not hand-written.

## Hazards the cutting PR must carry

- **H1: store census before deleting the `EXECUTION_CORRECTED` fold.** Replay goes through the fold registry by name. Before removing the registration, run a read-only `SELECT count(*) FROM custody_transitions WHERE transition_kind = 'EXECUTION_CORRECTED'` against every clerk store: live, paper, Dry Run/sim, shadow, and any authority a clean-slate reset moved aside. Do it through the same read-only path the economy reader uses, never by taking the execution lease. Any non-zero count means the fold stays. The writer, facts and validator can still go.
- **H2: mixed tests that use a correction as setup.** `test_folds_execution.py::test_cumulative_recovery_fill_is_explicitly_tagged` (559), `test_fee_evidence.py::test_simulated_fee_cutoff_uses_inclusive_economic_time` (1060), `test_budget_claims.py::test_a_partly_filled_entry_keeps_claiming_its_whole_fee_provision` (453) and `::test_interleaved_same_symbol_deployments_keep_own_fifo_after_correction_and_stop` (519), and `test_offline_v9_upgrade.py::_seed_production_v8` (121, which seeds an `EXECUTION_CORRECTED` row into a synthetic v8 store). Each one calls the dead writer or builds a correction row. The cutting PR re-reads each: if the correction is what the test proves, it goes as kind 5. If the test proves another live outcome, that outcome's coverage is checked against the test tickets (#2717, #2718) before deletion. Fixing such a test so it stays is a rewrite, out of scope for the map.
- **H3: OpenAPI contract.** `EXECUTION_CORRECTED` appears in `contracts/openapi/python-data-service.openapi.json` through `services/broker_v2_panel/evidence_service.py:129`. If that label goes too, see #2703, regenerate the snapshot and merge serially with other contract regenerations.
- **H4: the writer-boundary census.** `sqlite/repository_boundary.py` (kept, see below) and its AST test `test_external_repository_writers_match_the_explicit_census` must still pass once `update_final_outcome` and `append_execution_correction_or_raise` are gone. Run that test file.
- **H5: money-path test run.** Every row except A-list constants touches custody, receipts or economics. Run `tests/broker/alpaca/clerk/`, `tests/broker/v2panel/` and `tests/contracts/` at the cutting SHA, and re-check each row's evidence there, because kill lists age.

## Kept on purpose (reachable, or a seam for tests of live behavior)

**Schema-upgrade and recovery code stays.** Which stores could still hit it:

- `sqlite/schema.py::SCHEMA_MIGRATIONS` (v9 to v22, applied in `repository_lifecycle.py:411-419`): any store opened below v22 hits these steps. That means any live, paper, Dry Run/sim or shadow authority last opened by an older build. No repo evidence shows every store is at v22.
- `sqlite/offline_v9_upgrade.py` (v8 to v9): only via `scripts/manage_alpaca_sqlite_clerk.py`, which `docs/runbooks/add-an-alpaca-account.md` and `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` link. Any v8 store, including moved-aside clean-slate authorities, needs it.
- `sqlite/runtime.py::SqliteAlpacaClerkFacade.upgrade_legacy_exit_terms`: runs on every startup (`active_runtime.py:465`). Any store holding a bot without sealed exit terms hits it.
- `live_arming.py:35-60`, the retired arming reason codes: kept so that replaying `blocked` receipts recorded under them is not called drift. Any store that recorded an arming refusal before #2553/#2629 hits it.
- `sqlite/cutover.py` and `ceremony.py`: live through `services/alpaca_live_graduation.py` (routed from `main.py`).

A one-off read-only census of every store's `schema_version` (live, paper, Dry Run/sim, shadow, moved-aside) is what would let a later PR drop migration steps below the oldest store. That census was not run: it means opening live stores, which this ticket must not do.

**Test seams** (only tests call them; the tests check live outcomes through them; keep while those tests live):

| Symbol | Seam for |
|---|---|
| `live_envelope.py::LiveEnvelopeGate.latest_observation` (320) | About 30 envelope-sync, risk-policy and budget tests that read the gate's published observation. |
| `live_arming_ledger.py::LiveArmingLedger.records_for` (75) | `tests/_helpers/historical_arming.py` and the arming-ledger tests. |
| `sqlite/mirror.py::MirrorFile.has_finalize` (143) | Crash-recovery finalize tests (`test_repository.py:369`, `test_corrective_foundation.py:350`). |
| `sqlite/intake_fence.py::ReentrantAsyncLock.last_hold_duration_seconds` (86) | `test_intake_fence.py:248`. |
| `sqlite/repository.py::reconcile_poison` (510) | `test_corrective_foundation.py:507` (poison blocks writes). |
| `sqlite/repository_read_api.py::verify_operation_claim` (196) | `test_corrective_foundation.py:704` (claim compare-and-swap fencing). |
| `sqlite/repository_read_api.py::attributed_positions_for_subject` (709) and `sqlite/reads.py::attributed_positions_for_subject` (1291) | Manual-subject custody tests in `test_manual_orders.py` and `test_trade_evidence.py`. |
| `sqlite/economic_projection.py::bot_economic_snapshot` (768), `::bot_fills` (276), `::account_executions` (497), with `_account_execution_rows`, `_visible_page`, the module's `_encode_cursor`/`_decode_cursor`, `FillPage`, `ExecutionPage`, `InvalidEconomicCursor` and `DEFAULT_FILL_PAGE_LIMIT` | `test_execution_authority_golden.py::_project_fixture` (golden FIFO parity) and the economics-conflict tests. `bot_economic_snapshot` is a thin wrapper over the live `_project_bot_session_economics`. |
| `active_authority.py::set_alpaca_clerk` (691), `::reset_alpaca_clerk_for_testing` (698) | About 165 test call sites. Documented as a "compatibility test seam". |
| `sqlite/dry_run_close.py::closes_owed` (93) | `test_dry_run_close.py`. One line over the live `ended_dry_runs` + `closes_owed_for`. |
| `sqlite/broker_port_guard.py::missing_guarded_async_methods` (261) | `test_intake_fence.py:144-153` (guarded ports cover every async port method). |
| `sqlite/schema.py::load_pinned_ddl` (1069) | `test_schema_parity.py` (DDL against the pinned-contracts doc). |
| `sqlite/repository_boundary.py` (whole module; the only clerk module with no non-test importer) | `test_repository_writer_boundary.py`, the AST census of external custody writers (`docs/known-gaps.md:216` links it). Whether that structural guard earns its place is a test question for #2719, not a dead-code one. |

## Pointers: cuttable things outside this area

- **#2707 (scripts):** `PythonDataService/scripts/qualify_alpaca_activation_inventory.py` (49 lines). Nothing invokes it — no CI job, compose file, runbook or hook — and only `docs/architecture/adrs/0037-sqlite-sole-alpaca-custody-authority.md:101`, the 2026-08-19 retirement audit and `engine-authority-map.md` mention it. It was the one-time gate for retiring legacy custody, which is done (`test_alpaca_active_authority_wiring.py::test_legacy_custody_modules_and_selector_are_absent`). If #2707 cuts it, these go with it: `sqlite/activation_inventory.py` (313 lines, reachable only through that script), `tests/broker/alpaca/clerk/sqlite/test_activation_inventory.py` (250 lines), and `tests/contracts/test_alpaca_active_authority_wiring.py::test_migration_gate_requires_an_explicit_nonempty_inventory` (228).
  - The other script-only clerk modules stay. `sqlite/qualification*.py` (7 modules, about 2.1K lines) runs through `scripts/run_alpaca_sqlite_qualification`, which `compose.yaml` and `.github/workflows/ci.yml` invoke. `sqlite/dev_reset.py`, `sqlite/catalog_quarantine.py` and `sqlite/offline_v9_upgrade.py` run through `scripts/manage_alpaca_sqlite_clerk`, which runbooks link.
- **#2706 (schemas):** `PythonDataService/app/schemas/account_custody_synthetic_qualification.py` is reachable only through the qualification script path, not through any route. Check whether any router uses it.
- **#2703 (services):** `PythonDataService/app/services/broker_v2_panel/evidence_service.py:129`, the `EXECUTION_CORRECTED` evidence label. It is dead once section C lands; see H3.
- **#2714 (math reference notes):** `docs/references/alpaca-live-envelope.md:98,205` describes the shadow envelope subtracting `account_net_cash_spent_usd()`, which nothing calls any more. `docs/references/alpaca-sqlite-clerk-invariant-traceability.md:18` describes the `repository_boundary` census, which is kept.
- **#2711 (one-off docs):** `docs/superpowers/plans/2026-09-08-live-slice-5-risk-envelope.md` specifies `account_net_cash_spent_usd` and its never-landed test. `docs/references/two-bots-one-symbol-2469.md` names `exit_recovery.py`, which is live.
- **#2712 (ADRs):** ADR 0037 Consequence 5 names the inventory script. Update it if #2707 cuts the script. ADR 0045 line 153 names `dry_run_close.py`, which is live.

## Not reviewed

- **Branches inside live functions.** Unreachable `if` arms kept for a retired mode were only spot-checked by grepping for `legacy|retired|deprecated|compat|ibkr` across the clerk. A branch-by-branch read of the 65K lines was not done.
- **Unread projection columns and schema columns.** No query-by-query check of which `CREATE TABLE` columns any `SELECT` still reads.
- **Pydantic fields never read**, and **config/settings keys** the clerk declares but nothing reads.
- **Methods hidden by shared names.** A method whose name matches another class's live method counts as live in this census. Only the economy reader's cursor helpers were untangled by hand.
- **On-disk store state.** The `schema_version` of each store, and whether H1's `EXECUTION_CORRECTED` count is zero, were not inspected: that means opening live stores, which the cutting PR must do read-only.
