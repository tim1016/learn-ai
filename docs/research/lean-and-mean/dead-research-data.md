# Kill list: dead code in research, the data lake and the small packages

Ticket #2705, part of map #2700. **Plan, don't cut.**

- **Read at:** `87b8e261021ec673c2e7c80448c0973bccd45378` (latest `origin/master` on 2026-09-30, same SHA the map was charted at).
- **Area:** `PythonDataService/app/{research,data_lake,jobs,ml,marketdata,data,utils,security,models,scripts}/`.
- **Paths:** relative to `PythonDataService/` unless they start with `docs/`, `Backend/`, `Frontend/`, `compose` or `CONTEXT.md`.

## How this was judged

1. **Module reachability.** I built a static import graph over all of `PythonDataService/` with `ast`, then walked it from every live root: `app.main`, every non-`app` script, every `app.*` module named in compose, Dockerfiles, CI workflows or shell scripts, and every `"app.x.y"` string literal. Test files are never roots. A module the walk never reaches is dead unless something outside Python runs it, which I checked by hand for every hit (UI copy, docs, runbooks).
2. **Symbol callers, import-aware.** For every top-level function, class and constant in the area I counted callers in non-test files that import the defining module or a package re-exporting it. Then I followed calls inside each file, so a helper only an unused function calls counts as dead too. A second pass looked for symbols whose only callers are dead modules.
3. **Method and name pass.** A throwaway `vulture` 2.16 run (scratch venv, excluding tests, 60% confidence) plus a name-only search over methods. Every candidate was confirmed with `rg -w` across the whole repo (`PythonDataService/`, `Backend/`, `Frontend/`, `scripts/`, compose, `deploy/`, `.github/`, docs), skipping only `references/`.
4. **Retired-mode markers.** I grepped the area for `legacy|retired|deprecated|Slice N|kept for|back-compat|TODO` and checked each branch's selector.

**Test-only callers.** I split these into two groups. If a symbol's only callers are its *own* unit tests, it is dead, and those tests are rows here. If its callers are tests that prove *live* behavior (an oracle table, a fixture importer, a report renderer, a teardown helper), it is test-support code that happens to live in `app/`. Cutting it would mean rewriting surviving tests, so those items are listed under **Kept: test-support** for the test tickets to judge. This split is a rule-level question for the map (see the end).

## Kill list

### Data lake

| Path | Kind | Evidence |
|---|---|---|
| `app/data_lake/cache_import.py` | module, one-shot CLI (1181 lines) | Its docstring says "One-time, idempotent import of the existing lean-cache" (`cache_import.py:1`). Issue #1832 closed 2026-08-28. Nothing imports it outside tests, and no compose file, CI job, script, runbook or skill runs `python -m app.data_lake.cache_import`: the only usage text is its own (`cache_import.py:134`). It also holds the never-raised `ProvenanceCoverageError` (`:236`). |
| `tests/unit/data_lake/test_cache_import.py` | test file (67 tests) | Tests only the module above. |
| `tests/integration/data_lake/test_flag_flip_parity.py` | test file (8 tests, daily/Postgres) | Imports `verify_and_read_zip` at module level (`:58`) and builds its state with `import_cache_root` (`:526`). Its subject is the #1839 cache-to-lake migration ("the lake serves the same run the policy cache did", `:1`). See hazard H3. |
| `app/data_lake/catalog_client.py::mark_complete_artifact_failed` (`:1078`) | function | Only caller is `cache_import.py:776,802`. |
| `tests/unit/data_lake/test_catalog_write_ops.py::test_mark_complete_artifact_failed_refuses_a_claimed_row` | test | Tests the row above only. |
| `tests/_helpers/fake_lake_catalog.py` fake `mark_complete_artifact_failed` (`:487`, `:574`) | test-helper member | Orphaned by the row above. |
| `app/data_lake/fake_polygon.py` | module | No importers anywhere. Its docstring says "Slice 1d deletes the module entirely" (`:44`), and `synth_artifact_record` only raises `NotImplementedError` (`:46`). |
| `tests/fixtures/data_lake_skeleton/canned_response.json` (whole dir) | fixture | Its only reader is `fake_polygon.py:25`. |
| `app/data_lake/sweep.py` | module | `reclaim_expired_leases` (`:19`) has no non-test caller. The planned scheduler hookup ("Slice 4 wires it onto a scheduler", `:3`) never landed: no job, route or startup hook names it. Expired leases are reclaimed inline: `catalog_client.steal_or_retry_minute_bar` (`catalog_client.py:1140`, expired-lease clause at `:1161`, live call at `:1300`). |
| `tests/unit/data_lake/test_sweep.py` | test file (2 tests) | Tests the module above only. |
| `app/data_lake/catalog_client.py::refresh_lease` (`:1112`) | function | No non-test caller; `catalog_client.py:539` is a comment. No live writer heartbeats a minute-bar lease. |
| `test_catalog_write_ops.py::test_refresh_lease_extends_expiry`, `::test_refresh_lease_rejects_wrong_owner` | tests | Test `refresh_lease` only. Two more asserts sit inside a test that survives (hazard H4). |
| `app/data_lake/path_policy.py::staging_path_for` (`:323`) | function | Test-only (`test_path_policy.py:27`). |
| `tests/unit/data_lake/test_path_policy.py::test_staging_path_isolation`, `::test_two_attempts_produce_distinct_paths` | tests | Test the row above only. |
| `app/data_lake/root_identity.py::RootContext.lake_container` (`:127`) | method | Test-only. |
| `tests/unit/data_lake/test_root_identity.py::test_lake_container_matches_path_policy` | test | Tests the row above only. |

### Jobs

| Path | Kind | Evidence |
|---|---|---|
| `app/jobs/progress.py::create_job` (`:146`) and its re-export in `app/jobs/__init__.py:38,47` | function | Called nowhere, tests included. Job records are created by .NET (`Backend/Jobs/JobsApi.cs:123`), and cached results by `app/jobs/cache.py:119`. |
| `app/jobs/progress.py::renew_lease` (`:183`) | function | Test-only. `ProgressEmitter` slides the lease inline (`progress.py:235`, `:278`). The `renew_lease` in `CONTEXT.md:1161,1432` and ADR 0019 `:40` is the daemon recovery action, which is unrelated. |
| `tests/jobs/test_job_lease.py:61` (the `renew_lease(...)` line in `test_an_expired_lease_stays_expired_when_the_worker_emits_again`) | assert line | Delete the line. The test survives because it still proves the `ProgressEmitter` path (`:60`). |
| `app/jobs/phases.py::total_weight` (`:158`) | function | Test-only. `total_weight` in `services/strategy_engine.py:129` is an unrelated local variable. |
| `tests/jobs/test_phases.py::test_total_weight_sums_correctly`, `::test_total_weight_unknown_job_zero` | tests | Test the row above only. |

### ml

| Path | Kind | Evidence |
|---|---|---|
| `app/ml/protocols.py` | module | `MarketDataProvider` has zero references anywhere in the repo. |

### models

| Path | Kind | Evidence |
|---|---|---|
| `app/models/requests.py::TradeRequest` (`:51-57`) | class | No importer. It also carries a `timestamp: str` field the temporal rule bans. |
| `app/models/requests.py::IndicatorRequest` (`:59-75`) | class | No importer. The router's `IndicatorRequest` is `research/divergence/preflight.py`'s (`routers/research_divergence.py:36-37`). |
| `app/models/responses.py::IndicatorTableRow` (`:414-433`) | class | No importer. `IndicatorTableResponse.rows` is `list[dict]` (`:443`). |
| `app/models/responses.py::IndicatorInfo`, `::AvailableIndicatorsResponse`, `::DatasetGenerationResponse` (`:452-478`) | classes | No importer. `IndicatorInfo` is used only by `AvailableIndicatorsResponse` (`:464`). None of these appears in `contracts/openapi/`. |

### research: divergence (trade half)

The bar half of the divergence study is live and stays (see "Judgment calls"). The trade half has no entry point.

| Path | Kind | Evidence |
|---|---|---|
| `app/research/divergence/analysis/run_trades.py` | module | `run_day4` (`:114`) is its only entry. It has no caller anywhere, `cli.py` never calls it, and there is no `__main__`. |
| `app/research/divergence/analysis/trade_divergence.py` | module | Only importer is `run_trades.py:22`. |
| `app/research/divergence/strategies/` (`__init__`, `common`, `engine_runner`, `s1_ema_crossover`, `s2_rsi_mean_reversion`, `s3_sma_crossover`) | package | Only importers are `run_trades.py:26-31` and `trade_divergence.py:21`. No tests. |
| `app/research/divergence/dashboard/build_dashboard.py`: `_trade_section` (`:732`), `_worst_days_section` (`:868`), `_eth_contamination_section` (`:990`), the trade half of `_executive_summary` (`:160-`), the footer bullet for `cache/divergence/15m/trades/` (`:1449-1450`) | dead branches | All read `cache/divergence/{tf}/trades/*`, which only `run_day4` writes (`run_trades.py:148,175`). With no entry point they can only render "Day 4 has not been run yet". |
| `build_dashboard.py::VARIANT_FULL` (`:45`) | constant | No reader. |
| `app/research/divergence/ingest/polygon_ingest.py::ingest_polygon_aggregates` (`:220`) and the docstring that calls it "Used in production" (`:5-7`) | function | No caller anywhere. |
| `app/research/divergence/ingest/tv_ingest.py::TV_DEFAULT_COLS` (`:63`) | constant | No reader. |
| `docs/references/trade-divergence.md` | math reference note | Names `trade_divergence.py` as the canonical implementation. Status is `pending-fixture`; there is no golden fixture, and nothing live links it (only an old audit row, `docs/audits/auto-research/baseline-math-rigor.md:350`). |
| `docs/math-sources-of-truth.md:243` ("Trade divergence" row) | doc row | Points at the dead module. |

### research: other

| Path | Kind | Evidence |
|---|---|---|
| `app/research/feature_spec.py::list_specs` (`:232`) | function | No caller. |
| `app/research/features/options_features.py::MIN_VOLUME_SKEW`, `::MIN_OI_SKEW` (`:11-12`) | constants | No reader. |
| `app/research/options/contract_finder.py::_passes_liquidity_filter` (`:107`), `::_get_trading_days` (`:127`), `_SEMAPHORE` (`:24`), plus `MIN_VOLUME`/`MIN_OPEN_INTEREST`/`MAX_SPREAD_RATIO` (`:26-28`) | functions, constants | No callers. The three thresholds are read only by `_passes_liquidity_filter` (`:112-121`). `_SEMAPHORE` is also named in `app/research/options/README.md:173`. |
| `app/research/options/iv_builder.py::MIN_OI` (`:34`) | constant | No reader. |
| `app/research/options_runner.py::_build_daily_timestamps` (`:65`) | function | No caller. |
| `app/research/parity/qc_reconciler.py::ReconciliationReport.render_json` (`:252`) | method | No caller, tests included. Only an old spec mentions it (`docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md:265`). |
| `app/research/parity/fixture_data_reader.py::fixture_data_source_factory` (`:189`) and its `__all__` entry (`:205`) | function | Only its own unit test calls it. |
| `tests/research/parity/test_fixture_data_reader.py::test_factory_returns_callable_matching_runner_signature` | test | Tests the row above only. |
| `app/research/recency/stats.py::_ET` (`:34`) | constant | No reader. |
| `app/research/return_distribution.py::SEGMENT_NAMES` (`:108`) | constant | No reader; only a docstring mentions it (`:162`). |
| `app/research/grid_search/models.py::PresentedStatus` (`:16`) | type alias | No reader. |
| `app/research/sweep/eligibility.py::eligible_strategy_keys` (`:74`) | function | Test-only. |
| `tests/research/sweep/test_eligibility.py:33` (last assert of `test_the_operational_harness_is_excluded_by_category_not_by_a_list`) | assert line | Delete the line. The test survives on its `sweep_eligibility` asserts (`:29-32`). |
| `app/research/sweep/warmup.py::RunUpPlan.is_primed` (`:97-99`) | property | Test-only. The assert at `tests/research/sweep/test_warmup.py:91` goes with it. |
| `app/research/walk_forward/splits.py::ms_to_date_str` (`:367`) | function | No caller. `date_str_to_ms` (`:377`) stays as test-support; its "Inverse of ms_to_date_str" docstring needs rewording. |

### scripts

| Path | Kind | Evidence |
|---|---|---|
| `app/scripts/backfill_lean_runs.py` and `app/scripts/__init__.py` (the whole `app/scripts/` package) | one-shot CLI | "One-shot CLI: backfill historical on-disk LEAN runs" for PRD #1929, which is closed. Nothing runs it: its only usage text is its own (`:15`). The live path persists every run (`app/services/lean_sidecar_service.py:156`). |
| `tests/scripts/test_backfill_lean_runs.py` | test file (19 tests) | Tests the module above only. |

## Kept: test-support code in `app/`

These have no live caller, but the tests that call them prove *live* behavior, so cutting them means rewriting surviving tests. They follow whatever the test tickets (#2726, #2728) decide for their tests.

| Path | Used by |
|---|---|
| `app/data_lake/catalog_schema.py` | Expected-schema table for `tests/integration/data_lake/test_schema_drift.py` (the Python-vs-EF schema contract). Also imported by `app/services/clerk_transaction_projection_schema.py`, which is itself unreachable (pointer to #2703). |
| `app/data_lake/catalog_client.py::close_pool` (`:98`) | Once `cache_import` goes, only test teardown in 18 test files calls it. |
| `app/research/ml/generators/quantconnect_fixture.py` (`import_qc_fixture` and helpers) | Loads the `tests/fixtures/golden/qc-precomputed-predictions/` golden fixture for the engine-vs-QC parity tests (5 test files, including `test_qc_aapl_phase3_trade_parity.py`). |
| `app/research/parity/qc_reconciler.py::ReconciliationReport.render_markdown` (`:223`) | `test_qc_aapl_phase3_trade_parity.py:257` writes a reviewer artifact with it. |
| `app/research/walk_forward/splits.py::date_str_to_ms` (`:377`) | Builds inputs in `tests/research/walk_forward/{test_endpoint,test_runner,test_splits}.py`. |
| `app/research/persistence/schema.py::SCHEMA_VERSION` (`:57`) | `tests/research/persistence/test_schema.py:42,64` pin the versioned DDL against it. |
| `app/research/documentation/analytical_metric_catalog_entries.py`: `LEAN_PORTFOLIO_ORACLE_KEYS`, `LEAN_TRADE_ORACLE_KEYS`, `LEAN_RUNTIME_SOURCE_KEYS`, `TRACER_OWNED_PORTFOLIO_KEYS` | Oracle key sets for the metric-catalog parity tests (`test_analytical_metric_catalog_entries.py:48-67`). |

## Judgment calls: kept

| Path | Why it stays |
|---|---|
| `app/research/divergence/cli.py`, `analysis/bar_divergence.py`, `indicators/*`, `ingest/*` | The routed page (`Frontend/.../research-lab.routes.ts:131`) tells the user to run `python -m app.research.divergence.cli all ...` (`data-divergence.component.html:99`). The router and dashboard read its outputs. The module graph marks the CLI unreachable only because a human runs it. |
| `app/research/divergence/ingest/dividend_adjuster.py::detect_dividends_from_gap` | A manual check that `docs/tv-polygon-validation-gotchas.md:26` instructs; the dashboard footer links that doc. |
| `app/research/ml/generate_prediction_set.py`, `generators/deterministic_rule.py` | An operator CLI documented in `docs/ml-predictions-authority.md:128` (linked from `docs/doc-authority.md` and `app/research/ml/artifact.py`). It is the only producer of prediction sets that the live spec `prediction` primitive reads (`engine/strategy/spec/evaluator.py`). If #2704 finds that primitive dead, all of `app/research/ml/` goes with it, plus its golden fixture and parity tests. |
| `app/data/qc-shadow/*.py` | `vulture` flags the LEAN classes, but they are audit copies resolved by path (`app/services/strategy_validation_manifest.py:844-845`). |
| `app/marketdata/ibkr_feed.py::_stream_bars_legacy` | A live branch, selected whenever continuity is off (`ibkr_feed.py:253-255`), inside the sacred IBKR read-only feed. |
| `app/utils/*`, `app/security/*`, `app/marketdata/*` (otherwise) | No dead top-level symbol found. `RETIRED_DATA_PLANE_CONTROL_SECRET` is a live guard that rejects the old value (`security/data_plane_control.py:69`). |

## What the cuts orphan

- **Fixtures and helpers:** `tests/fixtures/data_lake_skeleton/` and the fake `mark_complete_artifact_failed` in `tests/_helpers/fake_lake_catalog.py`, both listed as rows. `tests/scripts/conftest.py` stays because other script tests use it.
- **Packages:** `app/scripts/` disappears. `app/research/divergence/strategies/` disappears. `app/research/divergence/analysis/__init__.py` already exports only `bar_divergence` names, so it needs no edit.
- **Callers that lose one user but stay live:** `lean_sidecar/trading_calendar.regular_session_mask_ms_utc`, `divergence/ingest/polygon_ingest.resample_ohlcv` and `divergence/indicators/engine_adapter` each lose `engine_runner.py` as a caller but keep other live ones. `data_lake/ensure_data._minute_trade_dch` and `root_identity.marker_path` lose `cache_import`, and stay live through their own module and `scripts/manage_data_root.py`.
- **Docs and comments that name cut code (edit in the cutting PR):**
  - ADR 0049 `:25` cites `sweep.py`, and `:68` mentions `cache_import`.
  - `docs/math-sources-of-truth.md:98` names `cache_import`.
  - Docstrings: `catalog_client.py:407,640`, `lean_writer.py:100`, `ensure_data.py:579`, `path_policy.py:113`, `PythonDataService/scripts/migrate_lake_to_mode_roots.py:13`.
  - Test docstrings: `tests/lean_sidecar/test_lake_mount.py:722`, `tests/utils/test_advisory_lock.py:48`, `tests/integration/data_lake/test_gate_chain_convergence.py:11`, `tests/integration/data_lake/test_both_price_adjustment_modes.py:15`.
  - Backend comments: `Backend/Data/AppDbContext.cs:401` and `Backend/Models/MarketData/DataLakeArtifact.cs:24`. Leave the EF migration comment (`20260830120000_...cs:89`) alone, because migrations are immutable.
  - `app/research/options/README.md:173` (`_SEMAPHORE`).
- **Sediment docs that only mention cut symbols:** `docs/audits/computational-fidelity-2026-04-22.md` and `docs/audits/structural-integrity-2026-04-22.md` (`IndicatorTableRow`), and the phase-3 PnL spec (`render_json`). These belong to #2711, not to this list.

## Hazards the cutting PR must carry

- **H1. Doc checks.** After the doc edits, run `pytest tests/contracts` and `scripts/check_documentation_contract.py`. The latter treats `docs/math-sources-of-truth.md` as `protected-canonical` (`check_documentation_contract.py:37`).
- **H2. Daily-only tests skip silently.** `test_flag_flip_parity.py` and `test_catalog_write_ops.py` need Postgres. Without `POSTGRES_URL` (plus `POSTGRES_URL_IS_EPHEMERAL=1`) they skip and show green. Run them against an ephemeral database, or wait for the daily run, before calling the cut clean.
- **H3. Three tests in `test_flag_flip_parity` prove live lake behavior.** `test_chart_serves_a_covered_completed_window_with_zero_provider_calls`, `test_chart_serves_an_adjusted_request_from_its_own_imported_root` and `test_engine_backtest_over_an_imported_window_makes_zero_provider_calls` all build their lake through `cache_import`. Before deleting the file, confirm (with #2728) that another test proves "covered window, zero provider calls" on a natively written lake. If none does, raise it rather than silently losing that proof.
- **H4. Removing `refresh_lease` touches a fencing test that survives.** `test_a_stale_writer_cannot_mutate_the_winners_generation` uses it in the stale-negative assert (`:1190`) and in the winner-positive "fence is not blanket denial" assert (`:1217`). Deleting both leaves the stale-negative proof to `fail_artifact`/`complete_artifact`, but loses the positive check unless another call carries it. Choose deliberately.
- **H5. `cache_import` is the only tool that adopts a pre-lake `lean-cache` policy root.** Compose still mounts `./PythonDataService/lean-cache` and sets `LEAN_DATA_CACHE` (`compose.yaml:113,149`; `compose.fleet.yaml:81,105`). Before cutting, confirm no un-imported policy root still needs adopting, for example during the machine migration (#2273). Git history keeps the tool.
- **H6. No contract regeneration expected.** None of the cut models or functions sits on a route, and none appears in `contracts/openapi/`. If an OpenAPI export shows a diff, stop and re-check.
- **H7. Sealed program sources.** The only file in this area that `DECLARED_PROGRAM_SOURCE_PATHS` seals (`app/engine/strategy/program_sources.py:230`) is `app/utils/timestamps.py`. Nothing on this list edits it, and no follow-up should.
- **H8. Kill lists age.** Re-run the caller searches at the cutting PR's own SHA.

## Pointers: cuttable candidates outside this area

These are leads from the same module graph, not rows. The graph does not follow registries that dispatch by name: `lean_sidecar/cross_runner.py:152-158` walks `app.engine.strategy.algorithms` with `pkgutil`, and `engine/strategy/__init__.py:28` loads lazily. The owning ticket must check those before calling anything dead.

- **#2704 (engine, LEAN sidecar, volatility).** No importer outside tests:
  - `app/engine/live/`: `account_clerk_journal`, `account_clerk_journal_models`, `account_effect_models`, `account_epoch`, `account_owner`, `action_plan_signal_executor`, `artifacts`, `divergence/*`, `halt`, `intent_events`, `intent_ledger`, `live_artifact_io`, `reconcile`, `reconciliation_classifier`, `reconciliation_receipt`. These are money-path-adjacent, so keep when unsure.
  - `app/engine/options/{chain_resolver,pricer}`, `app/engine/edge/{calibration/*,features_realtime/delta_inversion,regime_drift}`, `app/engine/framework/alpha`, `app/engine/indicators/{vwap,rolling_distance_sigma}` (check the indicator registry).
  - `app/engine/strategy/algorithms/{buy_and_hold,spy_vwap_reversion}` (reachable through the `cross_runner` package walk, so likely live), `app/engine/strategy/spec/descriptors`, `app/engine/action_plan/parity`, `app/lean_sidecar/launcher/app.py` (check the sidecar image entrypoint), `app/volatility/example.py`.
  - Also: `app/engine/tests/` and `app/engine/strategy/spec/tests/` are test trees inside `app/` (for the test tickets).
- **#2703 (services).** No importer outside tests: `app/services/clerk_transaction_projection_schema.py`, `mutation_rung_receipts.py`, `paper_live_comparison.py`, `paper_live_evidence_reader.py`, `paper_live_evidence_store.py`.
- **#2701 (Alpaca clerk).** `app/broker/alpaca/clerk/sqlite/repository_boundary.py` has no importer outside tests.
- **No owning ticket on the map.** `app/operator/incidents/{safety_halt_notices,store}.py` has no importer outside tests. `app/operator/` is not in any child ticket's area.
- **#2707 (scripts).** One-shot scripts that keep area symbols alive:
  - `PythonDataService/scripts/migrate_lake_to_mode_roots.py` keeps `path_policy.resolve_lake_container`.
  - `scripts/backfill_ten_symbols_1892.py` uses `data_lake/backfill._rollup_spec`, which stays live through its own module.
  - `scripts/measure_verdict_under_live_terms.py` keeps `research/sweep/identity.SERVICE_ROOT` and `walk_forward_study/service._cell`, which are also used in their own files.
  - `scripts/manage_data_root.py` is the only caller of `root_identity.{init_empty_root,stamp_existing_root,inspect_root}`. It looks live: it is the documented root-marking path.
  - Also `PythonDataService/run_spy_partial_parity.py` (self-described "Throwaway diagnostic").
- **#2706 (routes).** If a route is cut, these area packages go with it. Each is reachable only through the routers named:
  - `research.baselines` (baselines), `research.monte_carlo` (monte_carlo), `research.indicator_reliability` (indicator_reliability), `research.return_distribution` (return_distribution).
  - `research.walk_forward` (walk_forward, walk_forward_study), `research.walk_forward_study` (walk_forward_study), `research.recency` (recency, jobs).
  - `research.{signal,features,options,options_runner,runner,target,config,feature_spec,feature_validation,batch_runner}` and `app.ml` (research, jobs).
  - `research.divergence` (research_divergence, dataset, jobs).
- **#2714 (math reference notes).** `docs/references/trade-divergence.md` is a row here (it goes with dead code), so #2714 can skip it.
- **#2711 (one-off docs).** The audits and specs that only mention cut symbols, listed under "What the cuts orphan".

## Not reviewed

- **Methods:** checked by name only. A method used only by tests whose name matches some other identifier in non-test code would be missed.
- **Pydantic and dataclass fields:** not reviewed (`vulture` reports 260 "unused variable" hits in the area, mostly wire fields).
- **Branches inside live functions:** reviewed only where a `legacy`/`retired`/`deprecated`/`Slice N` comment marks them. Unmarked dead branches were not searched for.
- **Route and script liveness:** modules reachable through a router or a `PythonDataService/scripts/` entry count as live here. Whether each route or script is still used belongs to #2706 and #2707 (see the cascade pointers above).
- **Registry entries:** functions registered in module-level dicts count as live through the dict. I did not judge whether individual registry entries (feature specs, phase tables, catalogs) are themselves retired.

## Questions for the map

- **Rule-level: test-support code in `app/`.** The ticket says "A symbol whose only callers are tests is dead." Applied literally, that also removes oracle tables, fixture importers, report renderers and teardown helpers whose tests prove *live* behavior, including the golden-fixture importer behind the engine-vs-QC parity tests and the schema-drift contract table. This list treats those as test-support, not dead code, and leaves them to the test tickets. Options:
  1. Test-support follows its tests (recommended).
  2. Literal rule: cut them and rewrite the tests that use them.
