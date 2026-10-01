# Kill list — dead code in `PythonDataService/app/services/` (#2703)

Part of map #2700. Read at **`87b8e261021ec673c2e7c80448c0973bccd45378`** (the map's charting SHA; `origin/master` when this branch was cut, 2026-09-30). Paths below are relative to `PythonDataService/` unless they start with `docs/`, `Backend/`, `Frontend/`, `scripts/` or `compose`.

## How this was judged

- **Module reachability.** I built an AST import graph of every `.py` under `PythonDataService/` (top-level and function-level imports, relative imports, and dotted `"app.x.y"` string literals so dispatch by name counts). I walked it from every entrypoint: `app.main` (the Dockerfile and compose `uvicorn app.main:app`), every `scripts.*` module (compose runs `scripts/run_alpaca_sqlite_qualification.py` and `scripts/run_broker_fleet_compose_qualification.py`), `lean_sidecar`, and the top-level scripts. Of the 159 service modules, 154 are reachable from some entrypoint and 146 from `app.main`.
- **Symbol reachability.** For every top-level function, class, method and constant in `app/services/**`, I counted identifier tokens across the whole repo. That covers `PythonDataService/` (tests counted separately), `Backend/`, `Frontend/`, `scripts/`, compose/deploy YAML, JSON and workflows. `docs/` was counted separately. A symbol is a candidate when nothing outside tests uses it except its own definition. I also ran vulture 2.16 from a scratch venv, used only to find candidates. I checked every row below with `rg` across the repo. Pydantic validators and other framework-dispatched members are not on the list.
- Token counting misses a dead symbol whose name collides with a live one, for example a generic `stop` or `start`. So this list is a floor, not a complete inventory (see **Not reviewed**).

## Kill list

### Whole modules

| # | Path | Kind | Evidence |
|---|---|---|---|
| 1 | `app/services/paper_live_comparison.py` | module | Its only importer is `paper_live_evidence_store.py:43`, which is itself dead (row 2). Apart from that, only `tests/services/test_paper_live_comparison.py:9`. No route, script or job imports it. (Paper/Live experiment, #2371.) |
| 2 | `app/services/paper_live_evidence_store.py` | module | No importer outside tests anywhere in the repo. `PaperLiveEvidenceStore` (`:89`) is constructed only in `tests/services/test_paper_live_evidence_{store,reader}.py`. |
| 3 | `app/services/paper_live_evidence_reader.py` | module | No importer outside tests. It is the only consumer in the repo of the fleet operation `bot_decision_evidence` (`:23`). |
| 4 | `tests/services/test_paper_live_comparison.py` | test file | Tests row 1 only. |
| 5 | `tests/services/test_paper_live_evidence_store.py` | test file | Tests row 2 only. |
| 6 | `tests/services/test_paper_live_evidence_reader.py` | test file | Tests row 3 only. |
| 7 | `app/services/mutation_rung_receipts.py` | module | Zero importers across the repo, tests included. `app/schemas/live_runs.py:21` already records it as "importer-less" since `03ce52b6`. |
| 8 | `app/services/clerk_transaction_projection_schema.py` | module | Its only importer is `tests/integration/data_lake/test_schema_drift.py:22`. It describes the Postgres `clerk_transactions` / `clerk_transaction_events` tables, which no Python code has written since the IBKR projection retired (`app/services/clerk_transaction_projection.py:3-6`). |
| 9 | `tests/integration/data_lake/test_schema_drift.py:22,26` | test edit | Remove the clerk-projection half (`CLERK_TRANSACTION_PROJECTION_TABLES`). The data-lake half stays. |

### Symbols inside live modules

| # | Path | Kind | Evidence |
|---|---|---|---|
| 10 | `app/services/bar_persistence.py:321` `BarPersistence.compact` (+ `_publish_parquet_atomic` `:156`, `_COMPACTED_PREFIX` `:60`) | method | It has never had a caller outside tests since it was introduced in `1e12b312` (#483). No job, loop or script schedules it. |
| 11 | `app/services/bar_persistence.py:367` `BarPersistence.apply_retention` | method | No caller. The "periodic retention sweep" that `app/broker/ibkr/config.py:124-126` describes does not exist. |
| 12 | `app/services/bar_persistence.py:303` `read_parquet` + `_parquet_path` `:468` + the parquet leg of `active_dates`; `app/services/live_chart_window.py:63` (protocol method) and `:232` (call) | dead branch | Only `compact` (row 10) writes parquet, so the live call at `live_chart_window.py:232` always reads nothing. |
| 13 | `tests/services/test_bar_persistence.py::{test_compact_emits_parquet_and_archives_jsonl, test_compact_is_idempotent_after_jsonl_is_archived, test_compact_failed_publish_preserves_parquet_and_jsonl, test_append_waits_for_compaction_and_preserves_both_bars, test_read_parquet_round_trips_bars, test_retention_deletes_files_older_than_window, test_retention_keeps_quarantined_files}` | tests | These test rows 10–12 only. |
| 14 | `tests/services/test_bar_persistence.py::test_active_dates_lists_jsonl_and_parquet`, `tests/services/test_bar_timestamp_rigor.py::test_bar_provenance_fields_survive_persistence_round_trip` | test edit | Drop only the compact/parquet leg. The JSONL behaviour these tests prove is live. |
| 15 | `app/services/session_authority.py:520-583`: `SessionSubmitBlockReason`, `_TRADEABLE_PHASES`, `evaluate_session_submit`, `order_mechanism_sessions_from_capability` | functions | No caller outside tests. Their own docstring (`:538-540`) says the submit path does no session branching of its own, and nothing wires this gate in. The block-reason strings (`order_mechanism_not_enabled`, …) appear nowhere else in `app/`. |
| 16 | `tests/services/test_session_authority.py::{test_order_mechanism_stays_rth_only_while_extended_placement_disabled, test_order_mechanism_without_capability_is_rth_only, test_order_mechanism_admits_only_proven_extended_sessions_when_enabled, test_evaluate_session_submit_branch_table}` | tests | Row 15's tests. Dead beats sacred. |
| 17 | `app/services/bs_greeks.py:145` `bs_european_vega` | math function | Only tests call it. It was added for the Newton step of `bs_solver.py`, deleted 2026-04-25 (`docs/architecture/options-math-authorities.md:87-89`). The live IV solver has its own vega step (`app/volatility/solver.py:200`). No registry dispatches it by name, and the golden-fixture tests import only `black_scholes_greeks` / `bs_european_price` (`tests/fixtures/test_options_pricing_fixtures.py:24`). |
| 18 | `tests/services/test_bs_greeks.py::TestBsEuropeanVega` (4 tests) | tests | Row 17's tests. No golden fixture involved. |
| 19 | `app/services/bot_trade_strategy.py:796` `strategy_intents` | function | No caller. It is only named in comments (`app/broker/alpaca/clerk/sqlite/qualification_polygon_replay.py:255`, `qualification_shadow_trace.py:260`, `tests/services/bot_runner/test_signal_adapter.py:127`). |
| 20 | `app/services/bot_trade_strategy.py:87` `EXPOSURE_CARRYOVER_STRATEGY_KEYS` | constant | An empty `frozenset` that no code reads. Only a docstring names it (`app/services/bot_trade_strategy_warmup.py:140`). A policy nothing reads cannot gate anything. |
| 21 | `app/services/bot_runner.py:2431` `BotTaskRegistry._start_custody_projection` | method | Zero references. |
| 22 | `app/services/bot_binding_repository.py:52,254,528`: `LEGACY_MIGRATION_LINEAGE_FILENAME`, `LegacyMigrationLineageRecord`, `read_legacy_migration_lineage` | retired-mode reader | Reads lineage from "the removed legacy clone workflow" (`:255`). Nothing writes the file and nothing outside tests reads it. |
| 23 | `tests/services/test_signal_program_admission.py::test_historical_clone_lineage_remains_readable_without_a_clone_writer` | test | Row 22's test. |
| 24 | `app/services/polygon_client.py:163` `PolygonClientService.fetch_trades` | method | Zero references repo-wide, tests included. |
| 25 | `app/services/polygon_client.py:1157` `PolygonClientService.fetch_technical_indicator` | method | Zero references repo-wide, tests included. |
| 26 | `app/services/sanitizer.py:127` `DataSanitizer.sanitize_trades`, `:182` `sanitize_indicator` | methods | Only tests call them. Their Polygon fetchers (rows 24–25) are dead too. |
| 27 | `tests/test_sanitizer.py::TestSanitizeTrades` (4), `::TestSanitizeIndicator` (2) | tests | Row 26's tests. |
| 28 | `app/services/ta_service.py:168` `TechnicalAnalysisService.generate_indicator_table` | method | Only tests call it. |
| 29 | `tests/test_ta_service.py::test_generate_indicator_table_{returns_all_columns, nan_replaced_with_none, ema_values_populated, rsi_range, supertrend_exclusive}` | tests | Row 28's tests. |
| 30 | `app/services/fred_service.py:177` `prefetch_rate_cache` | function | Zero references repo-wide. |
| 31 | `app/services/market_monitor.py:153` `PolygonMarketMonitor.display_dashboard` | method | Zero references. It prints a console dashboard. |
| 32 | `app/services/dataset_service.py:94` `DEFAULT_INDICATORS` | constant | Zero references, Frontend included. |
| 33 | `app/services/iv_recorder.py:92` `_RecorderResult` | class | Zero references. |
| 34 | `app/services/surface_hub.py:447` `SurfaceHubRegistry.start_all` | method | Zero references. |
| 35 | `app/services/sqlite_clerk_transaction_projection.py:1054` `_window_summary` | function | Zero references. |

## What the cuts orphan

- `app/schemas/paper_live_experiments.py` loses every model except `ClerkDecisionEvidencePage`, which `app/routers/alpaca_clerk_sqlite.py:91` still uses, plus whatever that page nests. Pointer → #2706.
- `app/schemas/live_runs.py`: `MutationRungReceipt`, `MutationBlockageStageId`, `MutationRungReceiptCode`. Its docstring counts (`:5-22`) go stale. Pointer → #2706.
- The fleet operation `bot_decision_evidence` (`ALPACA_OPERATIONS` in `app/broker/alpaca/clerk/fleet_adapter.py`; `app/broker/fleet/operation_catalog.snapshot.json:80`; the Frontend copy at `Frontend/src/app/fleet/fleet-operation-catalog.snapshot.json:80`) and the clerk route `GET /accounts/{account_id}/bots/{strategy_instance_id}/decision-evidence` (`app/routers/alpaca_clerk_sqlite.py:279`) lose their only consumer in the repo. Pointer → #2702 (fleet) / #2706 (route).
- `bar_persistence.py`: the `pyarrow` / `pyarrow.parquet` / `atomic_parquet_write` imports and `_PARQUET_SUFFIX`; the `retention_days` constructor parameter (`:170`) and its pass-through at `live_bar_aggregator.py:504`; the config key `live_bars_retention_days` (`app/broker/ibkr/config.py:127`, #2702's file).
- `scripts/pr_shard_durations.json`: the node IDs of every killed test (rows 4–6, 13, 16, 18, 23, 27, 29).
- No golden fixture is involved: none of the killed tests loads anything under `tests/fixtures/golden/`. I did **not** check whether the killed tests are the last users of a shared conftest fixture or helper. The cutting PR runs `ruff` plus a fixture-usage grep after the deletions.

## Hazards the cutting PR must carry

- **Re-check at your own SHA.** Re-run the caller search for each row. Paper/Live (#2371) and the extended-hours session submit gate (row 15) look like unfinished features rather than retired ones. If an open PR is wiring either one in, drop those rows.
- **Retention never ran.** Rows 10–12 also show that nothing prunes `artifacts/live_bars/`: it has grown without limit since #483. Cutting `apply_retention` is still correct, because no code runs it. If disk use matters, that is a new ticket, not a reason to keep dead code. Before cutting row 12, check whether any `*.parquet` exists under the live hosts' `artifacts/live_bars/` (none should, since nothing ever wrote one).
- **Docs name these paths in backticks, not as links**, so `scripts/check_documentation_contract.py` (which checks markdown link targets, `:171`) will not fail. They do become sediment (see pointers). `docs/math-sources-of-truth.md` is `protected-canonical` (`check_documentation_contract.py:37`). Its row `:323` must be edited, not left pointing at deleted files.
- **Stale comments.** Rows 19 and 20 are named in comments and docstrings in live files (listed in their rows). Edit them in the same PR.
- **Contract snapshots.** Removing `bot_decision_evidence` regenerates the fleet operation-catalog snapshots and the OpenAPI contract. Those PRs merge serially (map fog). This list does not require that removal. It belongs to #2702/#2706.
- **Row 8 vs Backend.** The Postgres `clerk_transactions` tables still exist through EF migrations. Dropping the Python expectation only stops checking their drift. Whether to drop the tables is #2710's call.

## Considered and kept

**Helpers in app code that only tests call, where those tests prove live behaviour.** By the ticket's literal rule ("only callers are tests ⇒ dead") these are dead. But the tests that call them prove live outcomes, many of them on the money path. Cutting the helper means rewriting those surviving tests, and rewriting surviving tests is out of the map's scope. **Kept pending a rule call (see the question to the map).**

| Path | Why it is only called from tests |
|---|---|
| `app/services/bot_runner.py:614` `BotTaskRegistry.deploy` | A thin wrapper over `deploy_with_admission`. Production calls `deploy_with_admission` (`broker_v2_panel/panel_deploy.py:409`). 136 test call sites in 27 files use it as setup. |
| `market_liveness.py:628`, `broker_v2_panel/action_execution_service.py:579`, `broker_v2_panel/live_projection.py:200` `reset_*_for_testing`; `dividend_service.py:98`, `fred_service.py:265` `clear_cache` | Resets singletons and caches between tests. |
| `live_bar_aggregator.py:244` `LiveBarAggregator.shutdown` | Test teardown in 5 files. Nothing calls it at app shutdown. That may be a missing wiring rather than dead code, and it sits in the sacred IBKR feed. |
| `iv_recorder.py:121` `InMemoryIvSnapshotStore` | A test fake that lives in app code. |
| `source_bar_ledger.py:879` `find_by_market_bar` | Assertion probe in `test_source_bar_ledger_treats_later_fetch_time_as_exact_redelivery`. |
| `sovereign_equity_snapshots.py:196` `capture_next_session_close` | Drives `test_scheduler_uses_calendar_derived_early_close_boundary`. The live `_run` loop inlines the same two calls. |
| `broker_v2_panel/action_execution_service.py:75` `UnknownActionError` | Never raised. `test_performer_raised_action_execution_error_burns_key_not_released` uses it as a stand-in subclass. |
| `account_custody_synthetic_scenarios.py:188` `SYNTHETIC_REHEARSAL_SCENARIO_IDS` | Read only by `test_schema_compatibility_exports_share_the_typed_scenario_registry`. |
| `bot_carryover.py:89` `read_checkpoint` | The only reader of the stop checkpoint (`carryover_checkpoint.json`), which is written in production (`bot_carryover.py:175`) and read only in `test_stopped_run_entries.py`. Money path: kept. Whether a file nothing reads should be written at all is a question for #2701. |

**On-disk compatibility readers on the money path.** These are kept because I cannot prove that no live host still holds the old rows: `bot_binding_repository.py:541-567` (`_migrate_legacy_binding` / `_read_legacy_binding`, `launch_reason="legacy"`) and `bot_dry_run.py:40` (`_lift_legacy_authority`). Checking the live instance directories for pre-authority `binding.json` / dry-run rows would settle it.

**Modules reachable only through scripts.** These are live as long as their scripts are:
- `alpaca_sqlite_synthetic_{drills,drill_support,fault_drills,frame_drills,state_drills}.py`, `account_custody_synthetic_scenarios.py` and `account_custody_qualification.py` are reached only through `scripts/run_alpaca_sqlite_qualification.py` (the compose qualification service, `compose.yaml:323-337`) and `app/broker/alpaca/clerk/sqlite/qualification_*`.
- `manual_order_qualification.py` is reached only through `scripts/run_manual_order_qualification.py`.

If #2707 kills those scripts or that compose service, these modules and their tests join this list.

## Pointers outside this area (not rows)

- **#2704**: `app/engine/live/reconciliation_receipt.py`, recorded as importer-less by `app/schemas/live_runs.py:21`.
- **#2706**: the orphaned schemas and the `decision-evidence` route above. Also, any route #2706 cuts may orphan services that are reachable today only through that route. This list assumes every route at the SHA is live.
- **#2702**: `bot_decision_evidence` in the fleet operation catalog; `live_bars_retention_days` in `app/broker/ibkr/config.py`.
- **#2710**: the Postgres `clerk_transactions` / `clerk_transaction_events` tables (`Backend/Migrations/20260725010000_AddClerkTransactionProjection.cs` and its follow-up migrations).
- **#2712**: `docs/architecture/engine-authority-map.md:49,212,223`; `docs/architecture/options-math-authorities.md:23,89`.
- **#2713**: `docs/math-sources-of-truth.md:323`; `docs/runbooks/clerk-transaction-projection-rebuild.md` (it describes the retired Postgres projection).
- **#2714**: `docs/references/paper-live-decision-comparison.md`. It documents only the dead Paper/Live stack (rows 1–6).
- **#2711**: `docs/research/adversarial-extended-hours-design-review-2026-09-04.md:67-68` (names row 15).
- **#2707**: whether `scripts/run_manual_order_qualification.py`, `scripts/run_alpaca_sqlite_qualification.py` and the compose qualification service are still run.

## Not reviewed

- Members inside the 154 reachable modules that are used *only from other functions in the same file*. My filter shows them as used, so a private helper whose only caller is itself unreachable (a chain of more than one) would be missed. I followed one level of these chains, only for the rows above.
- Dead members whose names collide with a live name elsewhere (`start`, `stop`, `read`, `run`, …). The token count treats them as used. vulture surfaced a few of these (`deploy`, `compact`, `shutdown`), but it shares the same blind spot.
- Unused fields on Pydantic and dataclass models, and unused enum or literal members. Serialization makes those a contract question (#2706).
- Dead branches *inside* live functions, beyond the parquet read leg (row 12) and the legacy readers I kept above. I did not read the large modules (`bot_runner.py`, `chart_service.py`, `dataset_service.py`, `lean_sidecar_service.py`, `run_replay_proof.py`, `broker_v2_panel/*`) branch by branch.
- Config keys in `app/config.py` and `app/broker/ibkr/config.py` that only services read. I checked only the key orphaned by row 11.
