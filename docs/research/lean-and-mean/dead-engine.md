# Kill list — dead code in the engine, LEAN sidecar and volatility packages (#2704)

Part of map #2700. Plan, don't cut: nothing here has been deleted.

- **Read at:** `87b8e261021ec673c2e7c80448c0973bccd45378` (`origin/master`, 2026-09-30; the SHA the map was charted at).
- **Area:** `PythonDataService/app/engine/`, `app/lean_sidecar/`, `app/volatility/` (~51K lines, 238 files).
- **Rule applied:** dead code is decided on reachability alone; dead beats sacred, so dead code leaves with its tests, golden fixtures, docs and config.

## How this was checked

1. **Module reachability.** An AST import graph over all 2,098 Python modules in `PythonDataService/`, walked from the two real process roots: `app.main` (uvicorn in `PythonDataService/Dockerfile:63`, `compose.yaml:261`) and `app.lean_sidecar.launcher.app` (the host LEAN launcher, `app/lean_sidecar/launcher/app.py:5`). Test modules are never walked through. A second pass also added every `scripts/*` module and every `__main__` module as a root, so anything reached only by a script is tagged separately.
2. **Symbol reachability.** A name-based fixpoint over every top-level function, class, method and assignment in the reachable modules. The roots are module-level statements, decorated handlers (routes), and every identifier in the repo's non-Python production files (TS, C#, YAML, shell, JSON). Matching on names is conservative: a symbol whose name appears anywhere live counts as live. Every candidate was then checked by hand with `git grep -w` across the whole repo (`PythonDataService/`, `Frontend/src`, `Backend/`, `scripts/`, compose, `.github/`).
3. **Dispatch by name.** These by-name surfaces were checked before any formula or strategy was called dead:
   - the strategy registry (`app/engine/strategy/registry.py`);
   - the reflective strategy resolver `resolve_strategy_class`, which walks every module under `app/engine/strategy/algorithms/` (`app/lean_sidecar/cross_runner.py:139-176`);
   - the program-source anchor's `DECLARED_PROGRAM_SOURCE_PATHS` (`app/engine/strategy/program_sources.py:230`), imported at boot by `app/services/program_source_anchor.py:108`;
   - lazy re-exports (`app/engine/strategy/__init__.py:16-28`);
   - the `importlib`/`pkgutil` sites;
   - the router table in `app/main.py`;
   - every frontend caller of `/api/lean-sidecar/*` and `/api/volatility/*`.
4. A string reference to a module was counted only when it was real dispatch. Four modules were "rescued" only by docstring mentions and stay dead: `action_plan.parity`, `live.artifacts`, `live.live_artifact_io`, `live.reconcile`.

Almost everything below is the residue of two retirements:

- **#1679** (2026-08-19, "retire legacy broker control") removed the IBKR live runner. That stranded the warm-start, divergence, reconcile, intent-ledger and halt machinery.
- **#1813** (2026-08-27) removed the IBKR control plane. In its close-out, PR-C deliberately kept several readers of on-disk artifacts that had **no production caller** (`docs/architecture/engine-authority-map.md:187-200`). Under the locked rules reachability alone decides, so those kept readers are rows here. See "Needs the map's attention" for this conflict with an earlier decision.

## Kill list

### A. Whole modules with no production importer

| Path | Kind | Evidence |
|---|---|---|
| `app/engine/options/__init__.py`, `options/chain_resolver.py`, `options/pricer.py` | module ×3 | No importer anywhere, tests included. `git grep chain_resolver` matches only the file itself, and `pricer` is imported only by `chain_resolver`. The "engine adapter" that `docs/architecture/options-math-authorities.md:31` describes has no caller. |
| `app/engine/edge/calibration/__init__.py`, `edge/calibration/confidence.py` | module ×2 | No importer anywhere, tests included. (`app/engine/edge/confidence.py` is a different, live module.) |
| `app/engine/framework/alpha/__init__.py` | empty package | One line, no importer. |
| `app/engine/action_plan/__init__.py`, `action_plan/parity.py` | module | `parity_diagnostics` (`parity.py:27`) is imported only by `tests/engine/action_plan/test_parity.py`. The only other mentions are docstrings (`app/schemas/action_plan.py:176,192`). |
| `app/engine/edge/regime_drift.py` | module | Its only importer is `tests/edge/test_regime_drift.py`. |
| `app/engine/edge/features_realtime/delta_inversion.py` | module | Its only importer is `tests/edge/test_iv30_and_vrp.py`. |
| `app/engine/strategy/spec/descriptors.py` | module | Its only importer is `tests/engine/strategy/spec/test_descriptors.py`. |
| `app/engine/live/action_plan_signal_executor.py` | module | `StockActionPlanSignalExecutor` (`:56`) is imported only by `tests/engine/strategy/algorithms/test_signal_only_ema_crossover.py`. |
| `app/engine/live/account_clerk_journal.py`, `account_clerk_journal_models.py`, `account_effect_models.py`, `account_epoch.py`, `account_owner.py` | module ×5 | A closed cluster: they import only each other. No reachable module imports any of them. This is the JSONL Account Clerk journal from the IBKR era. `read_account_clerk_journal` and `fold_account_clerk_custody_statuses` are the readers #1813 PR-C kept (`engine-authority-map.md:197-198`). |
| `app/engine/live/intent_events.py`, `intent_ledger.py`, `reconciliation_classifier.py`, `reconciliation_receipt.py` | module ×4 | A closed cluster. `reconciliation_classifier.broker_snapshot_from_ibkr` (`:84`) and `classify` (`:158`) have no caller. `LedgerProjection` and `projection_from_envelope` (`intent_ledger.py:92,272`) are kept-by-PR-C readers with no caller. |
| `app/engine/live/halt.py` | module | Its only importer is `app/operator/incidents/safety_halt_notices.py`, which nothing imports (pointer below). `write_poisoned_flag`, `read_poisoned_flag`, `check_outside_mutation` and `check_lost_fill` (`:118-282`) are reached by tests only. |
| `app/engine/live/artifacts.py` and the `app/engine/live/divergence/` package (8 files: `bar_series_joiner`, `common`, `exec_pipeline`, `execution_divergence`, `execution_matcher`, `replay_divergence`, `report_bundler`, `__init__`) | module ×9 | Only test importers and each other. The live run artifacts these read (`decisions.parquet` / `executions.parquet` from the IBKR runner) stopped being written at #1679. The mentions in `strategy/base.py:48` and `spec/schema.py:421,536` are docstrings. |
| `app/engine/live/reconcile.py` | module + CLI | Reachable only as `python -m app.engine.live.reconcile` (`:829`). No compose, CI, script, Makefile, skill or doc invokes it; its only mentions are architecture-doc prose. Its last code change was #1679. |
| `app/engine/live/live_artifact_io.py` | module | Its sole production importer is `reconcile.py:102` (`artifact_sha256`, dead above). Its other readers are already test-only (`engine-authority-map.md:195`). |
| `app/volatility/example.py` | demo script | A `__main__` demo with no importer and no invoker. |

### B. engine/live — symbols left inside modules that are still imported

| Path | Kind | Evidence |
|---|---|---|
| `app/engine/live/exit_taxonomy.py` (whole file) | module | Its only importer is `account_registry.py`, for `CRASH_RETIRED_BINDING_SOURCES` / `TERMINAL_RESTART_BLOCKING_BINDING_SOURCES`, and those names are used only by the dead registry functions in the next row. `classify_run_exit` (`:105`), `read_run_exit_evidence` (`:78`), `false_crash_repair_source` (`:155`) and `terminal_restart_failure_phrase` (`:166`) are test-only. |
| `app/engine/live/account_registry.py`: `bot_order_namespace_for_instance` `:73`, `latest_account_instance_binding` `:185`, `has_account_recovery_evidence_after` `:197`, `crash_retired_restart_blocking_binding` `:210`, `account_recovery_evidence_exists_after` `:285`, `pending_account_binding_retirements` `:303`, `account_binding_ledger_parity` `:318`, `_account_ids_for_registry_scan` `:335`, `compute_reconcile_namespaces` `:344`, `evaluate_account_instance_binding` `:369`, `_registry_gate_result` `:448`, `AccountInstanceBindingIndex.duplicate_active_namespaces` `:65` | functions | The live importers (`cutover_roster.py`, `alpaca/clerk/sqlite/catalog_quarantine.py`) take only `read_account_instance_registry`, `index_account_instance_bindings` and `ACTIVE_INSTANCE_BINDING_STATES`. The names listed here have no non-test caller. **Keep** lines 38-183 (the registry read and index, plus their legacy read). |
| `app/engine/live/account_binding_ledger.py:105` `pending_binding_retirement_proposals` | function | Its only caller is the dead `pending_account_binding_retirements` above. The rest of the file feeds the live `read_account_instance_registry`. |
| `app/engine/live/account_artifacts.py` (1,380 lines) — everything except `AccountArtifactError`, `safe_account_artifact_id`, `_safe_account_path_segment`, `account_artifacts_root` and what those four need | ~60 symbols | Its live importers (`cutover_roster`, `account_binding_ledger`, `account_registry`, `producer_operational_log`, `catalog_quarantine`) import only those four names. The symbol sweep found these unreached: freeze, recovery clearance, owner/clerk generation, clerk lease, event log, sequence repair and their models (`:22-46`, `:68-233`, `:275-1301`), the `_REGISTRY_COMPAT_EXPORTS` / `__getattr__` compat shim (`:1310-1338`), and `ACCOUNT_RECOVERY_EVIDENCE_EVENT_TYPES` / `read_or_migrate_account_recovery_clearance` (used only by the dead registry functions). `read_account_freeze`, `write_account_freeze`, `read_legacy_account_events` and `AccountFreezeEvidence` are kept-by-PR-C readers. |
| `app/engine/live/producer_operational_log.py` (whole file) | module | Its only importer is `account_artifacts.py`, and only the dead event-log functions in that file use it (`merge_operator_history`, `read_producer_operational_events`, `append_producer_operational_event[_if_absent]`). `order_operator_history`, `process_producer_boot_id` and `ProducerOperationalLogRecord` have no reference outside the file. |
| `app/engine/live/live_state_sidecar.py`: `LiveStateSidecarCorruptError` `:25`, `stable_live_state_path` `:40`, `LiveStateEnvelope` `:56`, `LiveStateSidecarRepo` `:106` | classes/function | `LiveStateEnvelope` is used only by the dead `intent_ledger`. The mentions in `desired_state.py:76,204` and `reconciliation_receipt.py:4` are docstrings. **Keep** `fsync_parent_dir` `:248` and `_file_lock` `:267`: `bot_lifecycle_state`, `desired_state`, `durable_append_log`, `sealed_ledger`, `jsonl_wal` and `operator/incidents/store` use them. |
| `app/engine/live/durable_append_log.py`: `append_jsonl_record` `:28`, `_append_with_directory_fd` `:48`, `_append_on_service_owned_filesystem` `:63`, `rewrite_jsonl_records` `:88`, `_rewrite_with_directory_fd` `:105`, `_rewrite_on_service_owned_filesystem` `:130`, `_require_single_jsonl_record` `:340` | functions | Callers are the dead `account_clerk_journal` / `producer_operational_log` and tests only. **Keep** `create_exclusive_durable_file` and `create_atomic_exclusive_durable_file`, which `bot_binding_repository` and `deploy_submissions` use. |
| `app/engine/live/order_identity.py:109` `emergency_flatten_strategy_instance_id`, `:181` `validate_order_ref_components` | functions | No reference outside the file, or test-only. Every live caller (Alpaca clerk, panel) imports other names. |
| `app/engine/live/bot_lifecycle_state.py:39` `BotDisplayStatus` | class | No reference outside the file. |

### C. The indicator warm-start persistence (retired with the IBKR runner, #1679)

`hydrate()` and `maybe_write()` were the only callers of the strategy and indicator persistence hooks. `git log -S 'maybe_write('` shows the last caller removed in `e3e302b6` (#1679).

| Path | Kind | Evidence |
|---|---|---|
| `app/engine/live/indicator_state.py` (whole file) | module | `HydratePolicy`, `IndicatorStateEnvelope`, `HydrationReceipt`, `IndicatorStateHydrationError`, `stable_global_path`, `IndicatorStateRepo`, `hydrate` (`:300`) and `maybe_write` (`:565`) have no caller outside the file. The only names imported from it, `ValidationResult` (by `strategy/base.py:27,394` and `ema_crossover_signal.py`), serve only the dead `validate_state_payload` hooks below. `IndicatorStateRepo` is a kept-by-PR-C reader. |
| `app/engine/strategy/base.py`: `report_state_for_persistence` `:344`, `is_warm_startable` `:355`, `restore_state_from_persistence` `:371`, `validate_state_payload` `:385` | methods | Called only from `indicator_state.hydrate` / `maybe_write`. |
| `app/engine/strategy/algorithms/ema_crossover_signal.py`: `report_state_for_persistence` `:611`, `restore_state_from_persistence` `:647`, `validate_state_payload` `:666` | methods | The overrides of the hooks above; nothing else calls them. |
| `app/engine/indicators/base.py`: `Indicator.to_state_dict` `:97`, `restore_state` `:116`, `_to_state_extra` `:137`, `_restore_state_extra` `:141`; the same four on `BarIndicator` `:218-262`; `_str_to_decimal` `:270`; `_int_ms_or_none` `:274` | methods | Their callers are only the dead strategy hooks above and each other. |
| `app/engine/indicators/ema.py:51,58`, `rsi.py:102,112`, `sma.py:43,49` — `_to_state_extra` / `_restore_state_extra` | methods | Overrides of the dead base hooks. |

### D. Engine branches and hooks kept for retired modes

| Path | Kind | Evidence |
|---|---|---|
| `app/engine/execution/portfolio.py:134` `submit_limit_order`, and the LIMIT-order fill branch in `app/engine/engine.py:436-520` (resting-limit penetration fill) | method + branch | `OrderType.LIMIT` is constructed only at `portfolio.py:158`, inside `submit_limit_order`, whose callers are tests only (`app/engine/tests/test_limit_orders.py`, `tests/engine/test_portfolio.py`). No strategy or spec primitive submits a limit order. The engine branch at `:436` can therefore never see one. |
| `ExecutionConfig.limit_penetration` (`app/engine/execution/execution_config.py:40`) | config key | Read only by the dead LIMIT branch (`engine.py:498`). It is wired through as an API field (`app/schemas/engine_backtest.py:75`), into `engine_backtest_service.py:664,1189` and into run records (`app/research/backtest_runs/records.py:204,216`). See hazard H4: removing the field is a contract change. |
| `rollback_blocked_entry` / `rollback_blocked_exit` on `_rsi_range_base.py:300,305`, `deployment_validation.py:378,389`, `ema_crossover_signal.py:480,491`, `rsi_mean_reversion.py:243,248`, `sma_crossover.py:277,282` | methods ×10 | No production caller. `app/services/bot_trade_strategy.py:735` says in prose that the strategies "still carry" them while the runner no longer calls them. Tests only. |
| `app/engine/strategy/base.py:196` `StrategyContext.market_order` | method | Its only caller is `spy_vwap_reversion.py`, which is itself reachable only by name (see the conditional section). |
| `app/engine/strategy/registry.py:94` `EmaCrossoverOptionsParams` | class | No reference outside the file. No registry entry builds it. |
| `app/engine/data/trade_bar.py:78` `TradeBar.period_seconds` | property | Test-only. |
| `app/engine/framework/insight.py:90` `InsightScore.get_score` | method | Test-only. |
| `app/engine/results/statistics.py:495` `validate_equity_curve` | function | Test-only. |

### E. Volatility and edge

| Path | Kind | Evidence |
|---|---|---|
| `app/volatility/cache.py`: `SurfaceCache.is_valid` `:134`, `write_meta` `:160`, `write_grid` `:183`, `write_smiles` `:200`, `write_diagnostics` `:214`, `read_meta` `:228`, `read_grid` `:250`, `read_smiles` `:272`, `read_diagnostics` `:294` | methods | The router constructs `SurfaceCache()` (`routers/volatility.py:80`) and calls only `_cache.load_surface(...)` (`:132`). **That method does not exist** (`grep 'def load_surface'` finds nothing); the `except Exception: return None` at `:133` swallows the `AttributeError`. So the disk cache never runs in production. |
| `app/volatility/data_loader.py`: `OptionChainLoader.fetch_chain` `:58` and its private helpers `:132-391`, plus `ChainLoadResult` `:32` | methods/class | `routers/volatility.py:445` calls `loader.fetch_and_filter(...)`, **which does not exist**, so `/build-from-ticker` always returns 500 and `fetch_chain` never runs. Neither Frontend nor Backend calls any `/api/volatility/surface/*` route (pointer to #2706). |
| `app/volatility/models.py`: `OptionTypeEnum` `:26`, `VolQuery` `:73`, `SurfaceQueryRequest` `:80`, `SurfaceGridRequest` `:86`, `SurfaceDiagnosticsResponse` `:120`, `SurfaceBuildResponse` `:133`, `VolQueryResponse` `:146`, `SurfaceQueryResponse` `:155`, `GridPointResponse` `:161`, `SurfaceGridResponse` `:169`, `MatrixGridRequest` `:267` | Pydantic models | No importer except `volatility/__init__.py` re-exports. The router uses its own response models. |
| `app/volatility/vix_replication.py`: `vix_style_iv30` `:470`, `replicate_expiry_variance` `:87`, `_select_atm_strike` `:73`, `wrap_legacy_as_opra_mid` `:171`, `OptionQuote` `:46` | functions/class | Production uses `vix_style_iv30_with_provenance` (`routers/iv30.py:39`, `services/iv_recorder.py:45`). The legacy variant's callers are `iv30_health.compute_iv30_health` (dead, next row), the fixture generators `scripts/build_iv30_golden.py` and `scripts/fixture_generators/volatility.py`, and tests. See "Needs the map's attention", item 2: this takes golden fixture IV-003 with it. |
| `app/volatility/iv30_health.py`: `compute_iv30_health` `:60`, `_drop_random` `:49`, `_drop_alternates` `:55` | functions | Production uses `compute_iv30_health_normalized` (`services/iv_recorder.py:43`). The `vrp.py:17` mention is a docstring. |
| `app/volatility/price_normalization.py`: `from_recorded_snapshot` `:179`, `from_eod_close` `:199`, `from_eod_close_tiered_moneyness` `:233`, `tiered_moneyness_half_spread` `:63`, `TIERED_MONEYNESS_HALF_SPREAD_RULE` `:50` | functions | Callers are the dead legacy IV30 path and tests only. |
| `app/volatility/solver.py:404` `solve_iv_chain` | function | Test-only (`tests/volatility/test_solver.py`). |
| `app/volatility/analytics.py:306` `compute_put_call_parity_forward` | function | Test-only. The whole module orphans if the `/api/volatility/surface` router goes (conditional section). |
| `app/volatility/basis.py:100` `convert_iv_trading252_to_act365` | function | Test-only. |
| `app/volatility/conventions.py:50` `SurfaceConventions.discount_factor` | method | Test-only. |
| `app/engine/edge/features_realtime/iv30_constructor.py`: `iv30_atm_50d_trading_basis` `:73`, `iv_change` `:102`, `iv_vol` `:107` | functions | Test-only. The `vrp.py:52` and `regime_features.py:18` mentions are docstrings or column-name strings. |
| `app/engine/edge/regime_strategy_eval.py:58` `regime_run_lengths` | function | No reference outside the file. |
| `app/engine/edge/spread_model.py:21-27` `OPTION_SPREAD_FLOOR`, `DEFAULT_K`, `DEFAULT_ALPHA`, `option_spread` | constants/function | `trade_simulator.py:17,211` mentions `option_spread` only in prose; the code uses a literal fallback. Test-only (`tests/edge/test_spread_model.py`). |
| `app/engine/edge/threshold_events.py:38` `log_iv_dominance_warn` | function | No reference outside the file. |

### F. LEAN sidecar

| Path | Kind | Evidence |
|---|---|---|
| `app/lean_sidecar/data_policy.py:22` `__getattr__` (`DataPolicyManifest` deprecation alias) and its `__all__` entry | compat shim | Its own docstring says it is kept "for one deprecation cycle". No production importer uses `DataPolicyManifest`; one test does. |
| `app/lean_sidecar/config.py:83` `HISTORICAL_LEAN_IMAGE_DIGEST_ARM64` | constant | Test-only. |
| `app/lean_sidecar/staging.py:52` `StagedRun`, `:244` `list_factor_map_files` | class/function | No reference outside the file. |

### G. Tests, fixtures and helpers that leave with the code above

Whole files (all tests in them target dead code):

- `tests/engine/action_plan/test_parity.py` (7 tests)
- `tests/edge/test_regime_drift.py` (7)
- `tests/engine/strategy/spec/test_descriptors.py` (3)
- `tests/engine/live/divergence/` — all 7 files (49 tests)
- `tests/engine/live/test_artifacts.py` (32), `test_reconcile.py` (30), `test_qc_python_native_feed.py` (2), `test_live_artifact_io.py` (4)
- `tests/engine/live/test_halt.py` (29), `tests/operator/test_safety_halt_notices.py` (2)
- `tests/engine/live/test_intent_ledger.py` (8), `test_intent_dropped_before_submit.py` (9), `test_reconciliation_classifier.py` (21)
- `tests/engine/live/test_account_epoch.py` (4), `test_producer_operational_log.py` (9), `test_exit_taxonomy.py` (4)
- `tests/engine/live/test_indicator_state_envelope.py` (9), `test_indicator_state_repo.py` (14), `test_spy_ema_persistence.py` (11)
- `tests/indicators/test_ema_persistence.py`, `test_rsi_persistence.py`, `test_sma_persistence.py`, `test_indicator_base_persistence.py` (15)
- `tests/engine/indicators/test_vwap_reversion_indicators.py` — only if the conditional `spy_vwap_reversion` row goes
- `app/engine/tests/test_limit_orders.py` (10)
- `tests/volatility/test_cache.py` (21)

Partial files (only the tests that exercise a symbol listed above):

- `tests/engine/live/test_account_artifacts.py` — keep only the tests on the four live names
- `tests/engine/live/test_account_registry.py` — keep the read/index tests
- `tests/engine/live/test_live_state_sidecar.py` — keep the `_file_lock` / `fsync_parent_dir` tests
- `tests/engine/live/test_durable_append_log.py` — drop the append/rewrite JSONL tests
- `tests/engine/live/test_account_binding_ledger.py` — drop the retirement-proposal tests
- `tests/engine/test_portfolio.py` — the limit-order tests
- `tests/engine/test_deployment_validation_strategy.py` — the `is_warm_startable` / `hydrate_policy` / persistence tests
- `app/engine/tests/test_strategies_abc.py`, `tests/services/test_bot_trade_strategy_discard.py`, `tests/engine/strategy/test_deployment_validation_signal_program.py`, `tests/engine/strategy/test_signal_program_session_boundaries.py` — the `rollback_blocked_*` assertions
- `tests/edge/test_iv30_and_vrp.py` — the `delta_inversion` / `iv_change` / `iv_vol` tests
- `tests/edge/test_spread_model.py` — the `option_spread` tests
- `tests/engine/strategy/algorithms/test_signal_only_ema_crossover.py` — the executor tests
- `tests/volatility/test_solver.py`, `test_price_normalization.py`, `test_vix_replication.py`, `tests/edge/test_iv30_stability.py` — the legacy-IV30 and `solve_iv_chain` tests

## What the cuts orphan

- **`tests/_helpers/legacy_ibkr_artifacts.py`.** It imports the dead journal modules, but the surviving Alpaca SQLite cutover tests also use it (`cutover_test_support.py`, `test_catalog_quarantine.py`, `test_dev_reset.py`). Trim it to what cutover needs; do not delete it.
- **`tests/fixtures/golden/options-pricing/IV-003/`** and its `manifest.json` entry, plus the `IV-003` generator path in `scripts/fixture_generators/volatility.py` and `scripts/build_iv30_golden.py`. They go only if the legacy `vix_style_iv30` row goes (see "Needs the map's attention", item 2).
- **Docs that describe retired machinery** (pointers to the doc tickets):
  - `docs/architecture/engine-authority-map.md:187-200` (#1813 close-out residue) and its rows `:94`, `:243`;
  - `docs/math-sources-of-truth.md` rows naming `engine/options/pricer`, `regime_drift`, `delta_inversion`, `calibration/confidence`, `live/reconcile`, `indicator_state` and `iv30_health`;
  - `docs/architecture/options-math-authorities.md:31,65`;
  - `docs/architecture/edge-feature-design.md` (regime_drift, delta_inversion);
  - `docs/architecture/iv-ownership-research.md` (legacy IV30).
- **Program-source declarations.** `app/engine/live/indicator_state.py` appears in every `*_ARTIFACT_PATHS` tuple in `app/engine/strategy/program_sources.py` (`:46,72,99,115,146,184`), so those tuples lose an entry.
- **`ExecutionConfig.limit_penetration`** and its request, service and record plumbing (`schemas/engine_backtest.py:75`, `engine_backtest_service.py:664,1189`, `research/backtest_runs/records.py:204,216`, `services/lean_sidecar_persistence.py:588`).

## Hazards the cutting PR must carry

- **H1 — Signal Program build proofs.** `indicator_state.py`, `strategy/base.py`, `indicators/base.py`, `indicators/{ema,rsi,sma,adx}.py`, `ema_crossover_signal.py`, `_rsi_range_base.py` and the other algorithm files are in `DECLARED_PROGRAM_SOURCE_PATHS` (`program_sources.py:230`). The boot anchor hashes them. Any edit changes every affected program's source digest, so sealed qualifications for those programs stop matching and must be re-run. Removing `indicator_state.py` also means editing every `*_ARTIFACT_PATHS` tuple, plus the closure tests that recompute them (`tests/engine/strategy/test_signal_decision_digest_closure.py`, `tests/services/test_signal_program_admission.py`; the `test_spy_strategy_c_signal_decision_digest_closure.py` named in a `program_sources.py` comment does not exist at this SHA). Land the C/D strategy edits in one PR with one re-qualification, not file by file.
- **H2 — Retirement contract test reads dead files by path.** `tests/contracts/test_ibkr_order_actuation_retirement.py::test_historical_ibkr_evidence_modules_expose_no_writer_api` (`:159-172`) opens `engine/live/account_clerk_journal.py`, `account_artifacts.py` and `account_binding_ledger.py` by path. Deleting the journal file makes it crash rather than pass. Delete or retarget that test in the same PR (owner ticket #2730).
- **H3 — On-disk artifacts nobody reads after the cut.** The old account JSONL, freeze, recovery and live-state sidecars, `indicator_state` JSON, `decisions.parquet` / `executions.parquet` and poisoned flags stay on disk under `artifacts/`, unread. The cut deletes readers only; it must not touch `artifacts/`. The Alpaca SQLite clerk is the sole custody authority and does not import any of this. Of the account layer, only the four `account_artifacts` names, the registry read and the binding ledger read survive, for `cutover_roster`.
- **H4 — `limit_penetration` is on the wire.** It is a field of the `/api/engine/backtest` request and of stored backtest run records. Removing it regenerates the OpenAPI snapshot (`contracts/openapi/python-data-service.openapi.json`, and `Frontend/src/app/api/broker.types.ts` via codegen). PRs that regenerate contracts merge serially. The backtest-run records keep the key; check the readers tolerate its absence. The safe split: cut the engine branch and `submit_limit_order` first, and remove the request field in a follow-up owned by #2706.
- **H5 — Docs link contract.** Deleting modules that docs link by path (`engine-authority-map.md`, `math-sources-of-truth.md`) can trip `tests/contracts` link checks. Land the doc slimming with or before the code cut.
- **H6 — Kill lists age.** Re-run the import-graph and symbol sweep at the cutting PR's own SHA. In particular, re-check `account_artifacts.py`'s live four names, because Alpaca SQLite cutover is still active work.
- **H7 — Money path.** Cluster B touches leases, generations, freezes and custody files. Each row there was confirmed to have no production caller, and the rule says dead beats sacred. The cutting PR still gets thermo review (money-path) and must keep every test of `read_account_instance_registry`, `index_account_instance_bindings` and `cutover_roster`.

## Conditional rows — dead only if another ticket's cut lands

| Path | Kind | Gate and evidence |
|---|---|---|
| `app/engine/strategy/algorithms/spy_vwap_reversion.py`, `app/engine/indicators/vwap.py`, `indicators/rolling_distance_sigma.py`, golden fixture `tests/fixtures/golden/spy-vwap-reversion-qc/`, `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py`, `tests/engine/strategy/test_spy_vwap_reversion.py`, `docs/references/spy-vwap-reversion-port.md` | strategy port + fixture | **Gate: #2706 cuts `POST /api/lean-sidecar/runs/{run_id}/cross-reconcile`.** Not in the strategy registry, not imported by anything. Its only path is the reflective `resolve_strategy_class` (`cross_runner.py:139`), behind a route no UI, Backend or script calls (`Frontend/src` has only generated types for it). No LEAN trusted sample pairs with it. Kept today because the map's rule counts dispatch by name. |
| `app/engine/strategy/algorithms/buy_and_hold.py` (`BuyAndHoldStrategy`) + its use in `tests/lean_sidecar/test_cross_runner.py` | strategy | Same gate. The baselines feature's `buy_and_hold` method is a different code path (`app/research/baselines/generators.py:35`). |
| `app/lean_sidecar/cross_reconciler.py` | module | Same gate: its only importer is `routers/lean_sidecar.py`. (`cross_runner.py` stays; the cross-engine-study fixture regenerator uses it, see below.) |
| `app/lean_sidecar/reconciler.py` (`reconcile_against_ibkr`) | module | **Gate: #2706 cuts `POST /runs/{run_id}/reconcile`.** Its only importer is the router. The UI calls only `/trusted-runs`, `/diagnose` and `/calendar/next-trading-day-open` (`Frontend/src/app/services/lean-sidecar.service.ts:48,58,83`). |
| `app/volatility/analytics.py`, `app/volatility/data_loader.py` (whole), then a re-sweep of `surface.py` / `fitting.py` | modules | **Gate: #2706 cuts `/api/volatility/surface/*`.** No Frontend or Backend caller. Removing `app.routers.volatility` orphans exactly these two modules at module level (graph diff). |
| `app/engine/strategy/spec/schema.py:610` `load_spec_from_path` | function | **Gate: #2703 cuts `services/spec_strategy_runner.py::run_spec_against_bars[_and_persist]`.** Those are its only production callers (`:208,:259`), and they are themselves test-only (the `run_gate.py` mention is prose). |
| `app/lean_sidecar/polygon_canonical.py:97` `RecordedPolygonFixtureProvider` | class | **Gate: #2707 cuts `scripts/probe_lean_trade_count.py`**, its only non-test caller. |

## Checked and kept (so the next reader does not re-derive them)

- **`app/lean_sidecar/parity_matrix/*`, `cross_runner.py`, `config.RECONCILIATION_FIXTURE_IMAGE_DIGESTS`.** Reached only through `scripts/regenerate_cross_engine_study.py`. That script is the regeneration command named in the attribution files of the live golden fixtures under `tests/fixtures/golden/cross-engine-studies/`, so it is math-parity plumbing, not dead.
- **`signal_program.trace_corpus_root`.** Used by `scripts/generate_signal_program_trace_corpus.py`, a fixture generator.
- **`AverageDirectionalIndex.plus_di` / `minus_di` (`adx.py:78,82`) and `Supertrend.upper_band` / `lower_band` (`supertrend.py:90,94`).** Test-only accessors, but they expose intermediate state of live ported indicators that the golden parity tests assert on. Cutting them deletes parity coverage of live math, not dead code. (`research/divergence/indicators/native.py:157` has local variables of the same name, not callers.)
- **`BotLifecycleStateRepo.set_roster` / `set_phase` / `record_terminal_outcome` / `reopen_for_deploy` (`bot_lifecycle_state.py:157-240`).** No production caller, but they are the arrange step of surviving money-path tests (`tests/services/test_boot_recovery.py`, `tests/broker_configuration/test_prior_obligations.py`, `tests/broker/v2panel/test_bot_clear.py`, `tests/services/test_bot_lifecycle_projection.py`). Cutting them forces rewrites of surviving tests, which the map puts out of scope. When unsure on the money path, keep.
- **`app/engine/strategy/__init__.py` lazy `__getattr__`.** Load-bearing for the boot anchor (#2450); see its docstring.
- **Every registry-listed strategy** (`ema_crossover_signal`, `sma_crossover`, `rsi_mean_reversion`, `deployment_validation`, `spy_strategy_a/b/c`) and the LEAN `trusted_samples/*`. Reached by registry and template name.

## Pointers — cuttable things outside this area

- `app/operator/incidents/safety_halt_notices.py` — nothing imports it; it is `halt.py`'s only importer → **#2705**.
- `app/services/spec_strategy_runner.py::run_spec_against_bars`, `run_spec_against_bars_and_persist`, `pair_engine_fills` — test-only → **#2703**.
- `/api/lean-sidecar/runs`, `/runs/{id}/manifest`, `/observations`, `/normalized`, `/reconcile`, `/cross-reconcile`, `/log`, `/compare`, `/calendar/blocked-dates` — no Frontend, Backend or script caller → **#2706**. Several of section A's and the conditional rows' orphans hang off these.
- `/api/volatility/surface/*` — no caller, and two runtime bugs. `build-from-ticker` calls a non-existent `OptionChainLoader.fetch_and_filter` (`routers/volatility.py:445`). The cache read calls a non-existent `SurfaceCache.load_surface` inside a silent `except Exception: return None` (`:131-134`, a hard-rule violation) → **#2706**.
- `/api/engine/backtest` request field `limit_penetration` → **#2706** (hazard H4).
- `scripts/probe_lean_trade_count.py` → **#2707**.
- `docs/architecture/engine-authority-map.md` "#1813 close-out residue", `options-math-authorities.md`, `edge-feature-design.md`, `iv-ownership-research.md` → **#2712**. `docs/math-sources-of-truth.md` rows → **#2713** / **#2714**. `docs/references/spy-vwap-reversion-port.md` (conditional) → **#2714**.
- `tests/contracts/test_ibkr_order_actuation_retirement.py` (hazard H2) → **#2730**.

## Needs the map's attention

1. **This list overrides a recorded "retain" decision.** #1813 PR-C kept the production-callerless readers of on-disk IBKR-era artifacts, on purpose, as "the read side of a durable artifact the repo still stores" (`engine-authority-map.md:187-200`). The locked rule decides dead code by reachability alone, so this list cuts them. If the owner wants on-disk artifacts readable in perpetuity, that is a rule-level exception ("readers of retired artifacts are not dead"), and it would pull rows A (journal, intent ledger), B (account_artifacts readers, live_state_sidecar) and C (`IndicatorStateRepo`) back out.
2. **Cutting the legacy IV30 leaves the live IV30 without a golden fixture.** Golden fixture `IV-003` proves only the legacy `vix_style_iv30` (`tests/fixtures/test_volatility_fixtures.py:231-263`), which is dead. The live `vix_style_iv30_with_provenance` is a separate implementation with unit tests (`tests/volatility/test_vix_replication.py`) but no golden fixture of its own. Under dead-beats-sacred, IV-003 goes with the dead function. That removes the repo's only golden proof of the constant-maturity interpolation that the live path re-implements. Retargeting IV-003 at the live function would be a test rewrite, which the map puts out of scope. The rule decides this, but the owner may want to know.
3. **New ticket candidate.** The `/api/volatility/surface/*` router has two latent `AttributeError`s, one of them swallowed silently. If #2706 decides the routes stay, a bug ticket must follow. If they go, the surface/fitting/cache stack should be re-swept as a unit.

## Not reviewed

- The symbol sweep is name-based. A dead symbol that shares a name with a live symbol elsewhere (for example `fetch_chain`, `classify`, `fold`) reads as live and is **not** listed. The list is precise, not complete. A per-module, call-graph-accurate pass would likely find more inside `app/engine/strategy/registry.py` (1,839 lines), `app/engine/results/lean_statistics.py`, `app/lean_sidecar/runner.py`, `staging.py` and `normalized_parser.py`.
- Route-level reachability inside `app/routers/edge.py`, `app/routers/engine.py` and `app/routers/lean_sidecar.py` (which handlers the UI calls) was checked only far enough to gate the conditional rows. The full route call belongs to #2706.
- In-package test directories (`app/engine/tests/`, `app/engine/strategy/spec/tests/`) were judged only for dead-code coverage. Their other tests belong to #2727.
- Branch-level dead code inside live functions (retired modes behind flags), other than the LIMIT-order branch, was not swept systematically.
