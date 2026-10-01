# Kill list: dead scripts, tooling, compose and deploy files (#2707)

Part of map #2700. **Read at `87b8e261`** (`origin/master`, the SHA the map was charted at). This is a plan only. Nothing here has been deleted.

## How reachability was judged

A script is reachable when one of these runs it: CI (`.github/workflows/*`), a compose file, a kept runbook, a kept skill, `package.json`, or another reachable script. Live app code that imports it also counts. Callers were searched across the whole repo (`git grep -w` on each script's module name, outside `references/`). That search includes dispatch by name: `python -m scripts.X`, subcommand names, the golden-fixture registry (`scripts/generate_fixtures.py::FIXTURE_GENERATORS` against `tests/fixtures/golden/manifest.json`), compose `target:` and `profiles:`, and the env keys compose sets, checked against the pydantic settings classes that read them. A throwaway `vulture --min-confidence 60` run and an AST scan for unused top-level names found the symbol-level candidates. Every row below still has its own caller search behind it.

One reading needs the map's attention (see the last section). A script that a **kept artifact names as its regeneration command** was treated as reachable. That covers a golden fixture's `attribution.md` or `manifest.json`, a committed contract snapshot, and a pinned constant whose comment or test says "re-measure with X". Cutting such a script would leave sacred paperwork pointing at nothing. A measurement script whose result lives only in prose (an audit or a research note) was treated as dead.

## Kill list

| # | Path | Kind | Evidence |
|---|---|---|---|
| 1 | `PythonDataService/scripts/backfill_ten_symbols_1892.py` | script | `:1` says "One-off driver for issue #1892" (#1892 is closed). Searching for `backfill_ten_symbols_1892` finds nothing outside the file. |
| 2 | `scripts/probe_engine_lab_presets.py` | script | `:3` says "Manual diagnostic — NOT wired into CI". It has no caller anywhere and imports `playwright` (`:45`), which is not a repo dependency. |
| 3 | `scripts/verify_dataset_multipliers.py` | script | Nothing references it. It also hardcodes `RTH_MINUTES = 390` (`:26`), a session literal banned by `temporal-rigor.md`. |
| 4 | `PythonDataService/scripts/bench_panel_read_latency.py` | script | A one-off bench for #1801 (closed). Its only caller is its own test. Everything else is prose: `docs/known-gaps.md:347`, ADR 0052 `:45`, audits and plans. |
| 5 | `PythonDataService/tests/scripts/test_bench_panel_read_latency.py` | test (kind 5) | 1 test, and it only exercises #4. |
| 6 | `PythonDataService/scripts/measure_final_bar_decisions.py` | script | A research instrument for #2467 and #2607 (both closed). It is cited only in prose (`docs/references/final-bar-decisions-2467.md:65`, `:258`). It has no test. |
| 7 | `PythonDataService/scripts/measure_verdict_under_live_terms.py` | script | A research instrument for #2466 (closed). It is cited only in prose (`docs/references/ema-verdict-under-live-terms.md:187`, `:461`, `:471`). It has no test. |
| 8 | `PythonDataService/scripts/measure_sweep_cell_footprint.py` | script | Measured one cell for #1941 (closed). The only live mention is a descriptive docstring (`app/research/sweep/execution.py:23`). The same docstring says at `:26` "That limit is no longer this module's to enforce", so no constant waits on a re-measure. |
| 9 | `PythonDataService/scripts/migrate_lake_to_mode_roots.py` | script | `:1` says "One-time migration … (#1839)" (#1839 is closed). Its only caller is its own test. Prose: `docs/references/lake-adjustment-dimension.md:30`. |
| 10 | `PythonDataService/tests/unit/data_lake/test_migrate_lake_to_mode_roots.py` | test (kind 5) | 9 tests, all of #9. |
| 11 | `PythonDataService/scripts/manage_broker_configuration.py` | script | `:3` says "The one-time cutover tool for ADR 0060". No CI job, runbook or skill runs it. The worker refuses to bind a saved profile while the retired env lines are set (`PythonDataService/.env.example:30-31`). A running live clerk therefore shows the cutover has already happened. |
| 12 | `PythonDataService/tests/scripts/test_manage_broker_configuration.py` | test (kind 5) | 11 tests, all of #11. |
| 13 | `PythonDataService/scripts/qualify_alpaca_activation_inventory.py` | script | Emits a one-time ADR 0037 receipt. Nothing runs it: no CI job, no runbook (`add-an-alpaca-account.md` never calls it) and no skill. ADR 0037 `:99-101` still claims Consequence 5 "is enforced by" it, so that claim is already only on paper. |
| 14 | `PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py::test_migration_gate_requires_an_explicit_nonempty_inventory` | test (kind 5) | `:228`. It reads #13's source text (`:233-236`) and asserts strings in it. |
| 15 | `PythonDataService/scripts/run_manual_order_qualification.py` | script | Writes "pre-live evidence for the SQLite manual-order release gate" (`:1`). Its only caller is its own test, and no CI job, runbook or skill runs it. |
| 16 | `PythonDataService/tests/scripts/test_run_manual_order_qualification.py` | test (kind 5) | 4 tests, all of #15. |
| 17 | `PythonDataService/app/scripts/backfill_lean_runs.py` and `app/scripts/__init__.py` | script | `:1` says "One-shot CLI: backfill historical on-disk LEAN runs" (PRD #1929). Nothing imports it except its test. It is the only module in `app/scripts/`. |
| 18 | `PythonDataService/tests/scripts/test_backfill_lean_runs.py` | test (kind 5) | 19 tests, all of #17. |
| 19 | `scripts/dev/fleet/` (`_api.py`, `action_storm.py`, `churn_wave.py`, `fleet_launch.py`, `panel_action.py`, `read_bench.py`, `runner_stop.py`, `test_fleet_tooling.py`) | script dir | Manual fleet-stress tooling. Its only callers are its own test and CI lint. Docs mention it only in audits and plans (`docs/audits/bot-fleet-stress-2026-08-26.md:63`, `read-latency-profile-live-2026-08-31.md:15`). |
| 20 | `.github/workflows/ci.yml:42-56`, job `fleet-tooling-tests` | CI job | Runs only ruff and the test of #19. It is not a required check: the master ruleset requires only `Backend Tests`. Listed here because it tests dead code, so #2716 does not need to judge it. |
| 21 | `compose.yaml:285-351`, service `alpaca-clerk-qualification` (profile `qualification`), and its volume `compose.yaml:477-478` | compose service | Nothing runs `--profile qualification` or this service: no CI job, runbook, skill or script. Its only run produced the 2026-08-07 evidence under `docs/audits/`. CI runs the qualification directly as `--profile smoke` (`ci.yml:521`). |
| 22 | `PythonDataService/Dockerfile:75-end`, stage `qualification` | build stage | Its only `target: qualification` is `compose.yaml:289` (#21). Every other service pins `target: runtime` (`compose.yaml:76`, `compose.fleet.yaml:11,64`, `compose.fleet.dev.yaml:21`). |
| 23 | `PythonDataService/scripts/run_alpaca_sqlite_qualification.py`, the `--synthetic-rehearsal` mode: `_SyntheticHarness` `:59-61`, flag `:71-75`, rehearsal-only args and validation `:79-120`, dispatch `:128-129`, helpers `:184-515` (including `_ui_test_command` `:347` and `_bounded_process_failure` `:505`) | retired-mode branch | The only caller of this mode is #21. `_bounded_process_exception` `:516`, `_stream_tail` `:527` and `_bounded_os_error` `:535` stay, because the profile path calls them at `:156` and `:165`. |
| 24 | `PythonDataService/tests/scripts/test_run_alpaca_sqlite_qualification.py`: every test except `test_profile_arguments_remain_backward_compatible` (`:89`) | tests (kind 5) | 22 tests (`:107` to `:731`). Each one drives the rehearsal mode (#23) or pins the compose service (#21, `:731`). |
| 25 | `PythonDataService/scripts/manage_alpaca_sqlite_clerk.py`, subcommands `upgrade-v9` and `rollback-v9`: import `:44-47`, parsers `:76-85`, dispatch `:176-193` | retired-mode branch | An offline v8→v9 migration. The schema is now v22 (`app/broker/alpaca/clerk/sqlite/schema.py:48`). Nothing else reaches `offline_v9_upgrade`. The money-path check is under hazards. |
| 26 | `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py::test_v9_upgrade_and_rollback_cli_require_account_bound_process_stop_evidence` | test (kind 5) | `:406`. Tests only #25. |
| 27 | `PythonDataService/scripts/manage_broker_fleet.py:578`, `_MIGRATION_EPOCH` | dead constant | Not used anywhere, in the file or the repo. Its neighbour `_MIGRATION_INSTANCE` is used. |
| 28 | `scripts/generate_fixtures.py:35` `GOLDEN_SUPPORT`, `:46` `_lazy_import`, `:59` `_save_manifest` | dead symbols | Nothing calls them, in the file or the repo. The `GOLDEN_SUPPORT` in `test_golden_manifest.py:28` is that file's own constant. Removing these changes no fixture: the manifest hashes fixture files only (`file_sha256` and `content_sha256`). |
| 29 | `scripts/fixture_generators/volatility.py:323-324`, `_SVI_N_STRIKES` and `_SVI_N_CASES` | dead constants | Assigned and never read. |
| 30 | `contracts/data-plane-control-surfaces.schema.json` | contract schema | Nothing reads it. The `.json` it describes is read by `Frontend/proxy.conf.js:5`, `data-plane-control-intent.interceptor.ts:2` and `tests/test_data_plane_control_security.py:33`. None of them validates against the schema, and the `.json` has no `$schema` link. |
| 31 | `.env.example:4-5` (`ALPACA_QUALIFICATION_API_KEY_ID` and `ALPACA_QUALIFICATION_API_SECRET_KEY`), `.env.example:16` comment, `deploy/fleet/ci-render.placeholders:10-12`, `PythonDataService/.env.example:34-35` comment (the `:32-33` lines naming #11 go with #11) | config keys | Only #21 reads them (`compose.yaml:296-342`). The `ALPACA_CLERK_PRODUCTION_ACCOUNT_ID` and `ALPACA_CLERK_UI_EVIDENCE_PATH` placeholders go too. |
| 32 | `PythonDataService/tests/contracts/test_data_plane_image_stage.py`: `QUALIFICATION_SERVICES` `:41`, the union in `:157`, and `test_the_qualification_stage_copies_every_file_its_dev_install_reads` `:209` | test pins (kind 5) | They pin #21 and #22. The rest of the file still guards `target: runtime` and should stay. |

### Goes only if a doc ticket cuts the runbook that runs it

Each of these is reachable through exactly one doc. Whether that doc is kept is the doc ticket's call.

| Path | Kind | Only live caller | Owner of that doc |
|---|---|---|---|
| `PythonDataService/scripts/run_broker_fleet_conformance.py`, `PythonDataService/scripts/broker_fleet_conformance.py`, `PythonDataService/tests/scripts/test_run_broker_fleet_conformance.py` | script and test | `docs/runbooks/fleet-e-conformance-and-operator-acceptance.md:16`, a "Delivery E ceremony and acceptance template" (`:3`). The tests it selects already run in CI. | #2713 |
| `PythonDataService/scripts/manage_alpaca_shadow.py`, `tests/scripts/test_manage_alpaca_shadow.py` | recovery CLI and test | `docs/references/alpaca-shadow-authority.md:341-368` (operator invocations). It is on the money path, so keep it if that doc stays. | #2713 / #2714 |

## What the cuts orphan (outside this area: pointers, not rows)

- **#2701 (Alpaca clerk dead code), with #2717 and #2718 for its tests:**
  - `app/broker/alpaca/clerk/sqlite/offline_v9_upgrade.py` and `schema.OFFLINE_V9_SCHEMA_VERSION` (`schema.py:47`). Their only entry point is #25. Their test is `tests/broker/alpaca/clerk/sqlite/test_offline_v9_upgrade.py` (9 tests).
  - `app/broker/alpaca/clerk/sqlite/activation_inventory.py`. Its only caller is #13. Its test is `tests/broker/alpaca/clerk/sqlite/test_activation_inventory.py` (9 tests), which also imports #13 at `:10`.
  - The synthetic-rehearsal island, reachable only from #23:
    - `qualification_synthetic_rehearsal.py`, `qualification_ui_evidence.py`, `qualification_ui_campaign_contract.py`, and the rehearsal parts of `qualification_storage_recovery.py` and `qualification_polygon_replay.py`;
    - the re-exports among the imports at `qualification.py:7-40`;
    - `app/schemas/account_custody_synthetic_qualification.py`.
  - `contracts/alpaca-clerk-ui-correlation-campaign.v4.json` stays while `Frontend/tests/e2e/alpaca-clerk-ui-correlation.spec.ts` stays, because `npm run e2e` runs that spec and its fixture imports the contract. #2734 judges the spec.
- **#2703 (services layer):** `app/services/manual_order_qualification.py`. Its only importer is #15.
- **#2702 (broker configuration and migration layers), with #2722 for tests:**
  - `app/broker_configuration/legacy_import.py`. Its only importer is #11 (`:65`). Its tests are `tests/broker_configuration/test_legacy_import.py` and `test_cutover_rehearsal.py`.
  - `app/installation_migration/contents.py:63-67`, the `BUNDLED_VOLUMES` entry for `alpaca-clerk-qualification-data` and the `"qualification"` kind. This one is a hazard, not an option; see below.
  - `app/broker_configuration/legacy_environment.py:129-134`, the four qualification-only keys in `NEVER_RETIRED_SETTINGS`.
- **Doc prose that names a cut script.** No doc uses a markdown link to one, so the doc contract will not fail, but the prose goes stale:
  - #2713: `docs/known-gaps.md:347`; `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md:237-270` (the v9 section); `docs/runbooks/migrate-installation.md:22`.
  - #2712: ADR 0037 `:99-101`; ADR 0047 (v9 ceremony); ADR 0052 `:45`; `docs/architecture/alpaca-configuration-ownership-inventory.md:79-86,143`; `docs/architecture/engine-authority-map.md:57`.
  - #2714: `docs/references/final-bar-decisions-2467.md`, `ema-verdict-under-live-terms.md`, `lake-adjustment-dimension.md:30`, `alpaca-credential-slots.md:193,206` and `alpaca-live-authority.md:233`.
  - #2711: the audits and plans.
- **Stays, nothing to do:** `PythonDataService/tests/scripts/conftest.py`, because other script tests use it. `PythonDataService/scripts/pr_shard_durations.json`: stale entries for deleted tests are harmless (`scripts/pytest_shard.py:7-11`); regenerate it with `update_pr_shard_durations` once the cuts land.

## Hazards the cutting PR must carry

1. **Cut the qualification service in one PR.** That PR takes #21, #22, #23, #24, #31, #32 and the `BUNDLED_VOLUMES` entry together. `migrate_installation export` refuses when a bundled volume is missing (`tests/installation_migration/test_export.py:394-400`). Dropping the compose volume but leaving the entry would make the next host migration refuse. `target: runtime` must stay on every data-plane service. The comments at `compose.yaml:73-75` and `compose.fleet.yaml:9-10`, which say the Dockerfile ends on `qualification` (#2576), become false and should be reworded. The owner's existing podman volume `learn-ai-alpaca-clerk-qualification-data` stays on the host until the owner runs `podman volume rm`; the PR cannot do that.
2. **Money path, #25 (`upgrade-v9` and `rollback-v9`).** Before cutting, confirm that no authority database and no restorable backup below schema v9 exists anywhere (live clerks are on v22). If one does, keep #25 and its orphans.
3. **Money path, #11 (`manage_broker_configuration`).** Before cutting, confirm that the live and paper clerks bind a saved profile. A clerk that is running proves it, because the worker refuses to bind while retired env lines are set.
4. **No sacred paperwork is touched.** Every golden-fixture regeneration command was kept. #28 and #29 change no fixture bytes, and the manifest hashes fixture files, not generator sources. The fixture regenerators are `generate_fixtures.py`, all `fixture_generators/`, `build_iv30_golden`, `capture_*`, `probe_ibkr_history_settle`, `probe_lean_trade_count`, `regenerate_*`, `hitl_alpaca_capture` and `measure_fill_to_cash_visibility`.
5. **CI and branch protection.** Removing the `fleet-tooling-tests` job (#20) needs no branch-protection change. The CI check of the compose service set (`ci.yml:121-127`) lists only default-profile services and is unaffected. `deploy/fleet/topology.snapshot.json` has no entry for the qualification service.
6. **Re-check at your own SHA.** Kill lists age. Re-run each row's caller search at the cutting PR's SHA.

## Checked and kept

Each of these was checked; the reason it stays is in the second column.

| What | Why it stays |
|---|---|
| `restart.sh`, `setup-macos.sh`, `bootstrap-host-venv.sh` | Run by `.claude/CLAUDE.md:6-7` and by the runbooks `first-time-setup`, `windows-onboarding` and `add-an-alpaca-account`. |
| `compose.yaml` (db, redis, python-service, backend, frontend) | Always-on stack. Redis is read by `Backend/Jobs/JobsApi.cs`. |
| `compose.fleet.dev.yaml`, `compose.fleet.yaml`, `compose.fleet.qualification.yaml` | `restart.sh:58-66`; the daily `fleet-compose-qualification` job (`daily-tests.yml:155`); `broker_configuration` desk state; the handoff script. The `legacy-combined` profile on `python-service` (`compose.fleet.yaml:183`) is the switch that turns the combined service off. It is not dead. |
| `deploy/fleet/*` | CI (`ci.yml:96-125`), `render_fleet_topology.py`, and the runbooks. `provision-fleet-lake-catalog-role.sql` is mounted by `compose.fleet.dev.yaml:66`. |
| Every env key set in compose and `deploy/` | Each is read, either by a prefixed pydantic setting (`FLEET_*` maps to `AGENT_ENDPOINT_REF` at `app/config.py:64`, `IBKR_*` maps to `live_bars_root` at `app/broker/ibkr/config.py:122`), by `Frontend/proxy.conf.js` (`*_PROXY_TARGET`), or by the runtime itself (`ASPNETCORE_*`, `NODE_OPTIONS`, `NUMBA_DISABLE_JIT`, `PYTHONDONTWRITEBYTECODE`). The only exceptions are the qualification keys in #31. |
| `run_fast_tests`, `pytest_shard`, `update_pr_shard_durations`, `export_openapi_contract`, the three `regenerate_*_snapshot` scripts, `run_alpaca_sqlite_qualification` (profile mode), `run_broker_fleet_compose_qualification`, `render_fleet_topology` | Run by CI, or they regenerate a file CI checks. |
| `manage_alpaca_sqlite_clerk` (other subcommands), `manage_broker_fleet`, `fleet_lifecycle_ceremonies`, `_operator_cli`, `manage_canary_admission`, `manage_data_root`, `migrate_installation`, `alpaca_onboarding_gates` | Every subcommand is named by a runbook, by app code (refusal vocabulary, `recovery.py`), or by `setup-macos.sh`. |
| `export_analytical_metric_catalog`, `lean_sidecar_pin_image`, `run_signal_program_build_qualification`, `measure_fill_to_cash_visibility` | Each is the named regenerator of a kept artifact: the Frontend metric catalog, the `config.py:26` image pins, the build receipts that `app/engine/strategy/program_sources.py` loads, and the fill-visibility fixture plus the constant at `tests/broker/alpaca/clerk/test_live_envelope.py:101-103`. |
| All golden-fixture generators and capture scripts (hazard 4) | Named by `manifest.json` or `attribution.md`. All 30 `FIXTURE_GENERATORS` IDs exist in the manifest. |
| `contracts/fixtures/*`, `contracts/run-verdict-v1/fixture.json`, `contracts/strategy-lab/*`, `contracts/openapi`, `contracts/graphql` | Read by tests on both sides, by the Frontend, or by CI contract diffs. `strategy-metric-help-golden-v1.json` is read by the v2 parity test as history (`test_strategy_metric_help_golden.py:40`). |
| `Frontend/tools/openapi-codegen`, `Frontend/scripts/run-test-budget.cjs`, `data-plane-control-secret.cjs`, `generate-bs-parity-fixture.py` | Run by `Frontend/package.json:17`, by the test budget, or as a parity-fixture regenerator. |
| `Frontend/src/assets/scripts/alpaca-paper-to-live-handoff.sh` | Served by `configuration-handoff-script.component.ts:121`. |

## Cuttable or judged elsewhere (pointers)

- **Gate scripts go to #2716:** `scripts/check_adr_status.py`, `check_temporal_authority.py` and `check_documentation_contract.py`; `scripts/test_check_adr_status.py`; `Frontend/scripts/verify-*.cjs`; `.husky` and `lint-staged`.
- **`contracts/fixtures/alpaca-bot-control/v1/*` goes to #2709.** Only the `components/examples/alpaca-bot-control` example route (`app.routes.ts:306`) and its specs read these fixtures, so they go if that route is dead.
- **`skills-lock.json` goes to #2715.** It is the external skills installer's lockfile.
- **`PythonDataService/lean_sidecar/Dockerfile*` goes to #2704.** The plain `Dockerfile` builds the historical phase-1c derivative. `config.py:83` keeps that historical digest so reconciliation fixtures stay reproducible.

## Not reviewed

- **Branches inside large live scripts.** Only unused top-level names were scanned, plus vulture at 60% confidence. Dead branches inside these functions were not checked: `run_broker_fleet_compose_qualification.py` (1,291 lines), `manage_broker_fleet.py`, `hitl_alpaca_capture.py`, `regenerate_cross_engine_study.py`, `alpaca_onboarding_gates.py`, `setup-macos.sh`, and each fixture generator's internals.
- **Compose details.** Healthchecks, ports, labels, resource limits and tmpfs mounts were not judged. Only services, volumes, build targets and env keys were.
- **`Frontend/scripts/verify-*.cjs`** were left to #2716 without a caller check.

## For the map

- **Rule question for the owner: is a fixture's regeneration script sacred along with the fixture?** This list treats it as sacred. The fixture's attribution names the script, and the numerical-rigor rule requires a regeneration command. The locked rules list "golden fixtures, their attribution files, and their tolerance-pinned parity tests" but do not name generator scripts. Recommendation: say it outright in the rule rewrite, so the test and dead-code tickets read it the same way.
