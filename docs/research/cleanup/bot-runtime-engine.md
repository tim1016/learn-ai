# Clean-up findings — Bot runtime and engine

- **Ticket:** [Clean-up — Bot runtime and engine: bot runner, trade strategy, backtest service](https://github.com/tim1016/learn-ai/issues/3157), a module ticket of [Map — Robustness and cleanliness](https://github.com/tim1016/learn-ai/issues/3146).
- **Read at:** `origin/master` `862b50d3` (2026-10-13 21:17 CDT), the charted SHA. When the read began, none of the twelve most recent PRs was open on this scope, and no other session had claimed the ticket. Between the read and the cut `origin/master` gained one merge (#3171, options first-reading landing), which touches none of the cut's files.
- **Scope:** 129 production app files and 27 in-package test files (40,540 lines) under `PythonDataService/app/{services,engine,routers}`, and 157 test files (43,611 lines) under `PythonDataService/tests/`. 117 commits since 2026-10-01 touch the app files, 124 the tests; the last is 2026-10-11. `bot_option_run.py` and its option floors belong to [#3150](https://github.com/tim1016/learn-ai/issues/3150): those tests were run, not audited.
- **Depth:** all seven lenses.
- **How it was read:** eight read-only readers, one per sub-package slice, each read every app file of its slice completely, the bodies of the tests it flagged and the names of the rest, ran its slice's tests, ran the brief's ruff set and ban-list greps, and built its own repros. The lead ran the repo-wide symbol census, vulture and ruff first and handed the rows out; verified every cut row by its own grep; re-ran the F2-1, E-1 and E-2 repros; read the kill-list tickets' comments (see "Corrections"); and cut. The slices were: **A** bot runner core, **B** lifecycle (end, terminal, evidence, projections, quarantine, boot recovery) and `app/engine/live/`, **C** trade strategy and warmup, **D** start admission, bindings, signal-program admission and the source-bar ledger, **E** backtest service, routers, results and the engine core, **F1** strategy registry and the sealed programs, **F2** algorithms and the declarative spec, **G** engine data, execution, framework, indicators, consolidators and the edge package. Slice sections below are the readers' reports, condensed; repro scripts are in the session scratchpad, not in the repo.

## Verdict

**No P0. No P1.** Nothing in the module can place, duplicate or fail to cancel an order, and no gate was found failing open. The nearest misses, all P2:

- **A F-A-1:** `BotTaskRegistry.stop_all` iterates the live bot dict across an `await`; with two or more running bots and a contended Clerk intake fence it raises `RuntimeError: dictionary changed size during iteration` and aborts the rest of service teardown (reproduced against the real SQLite Clerk facade; boot recovery closes the runs afterwards).
- **E-1:** the Strategy Lab "LEAN native statistics" panel treats per-share points as dollars: a 100-share +$2.00/share winner (real $200) shows `total_profit_loss 2.0`, `net_profit 2000.0` (reproduced, re-run by the lead). A backtest display, not live state, so P2; the owner may rate it higher.
- **D-2:** a Start preview or refused Start of a Dry Run leaves a `sim:<sid>` authority behind (activation record, `clerk.db`, `source_bars.sqlite3`, an open runtime) that nothing releases and boot restores. Sim only. The same defect [#3151](https://github.com/tim1016/learn-ai/issues/3151) found (its F2, known-gaps F10 residual) — a pointer.
- **D-12:** source-bar capture is unfiltered, so RTH-only bots retain PRE/POST minutes against a 200,000-row-per-stream cap: at most 960 bars a day, about 208 sessions to a typed crash that could hit a bot holding a position. Owner ruling (no rollover tool exists).
- **G-1 / G-2:** the edge page's SHORT VOL / LONG VOL readout is built on a forward-filled unrealized forward-RV tail, and the "past-only" HF RV feature is not causal intraday. Research page, no order path.
- **C-1 / C-2 / C-3:** three money-path gates are unproven — a mutant of each survives 1,231+ tests (Stop-fence OBSERVE_ONLY branch in both runners; the ENTER-only liveness gate; the Dry Run REJECTED discard). Test gaps, not defects.

**One cut PR** ([#3172](https://github.com/tim1016/learn-ai/pull/3172)): delete-only, 12 files, +6 −69, 14 dead-machinery rows (listed below). Project-scope `ruff check` is clean; 1,562 quick tests and 1,863 more including the slow sealed-program tier (`tests/engine/strategy`, `tests/broker/v2panel`, `tests/contracts`) pass. It touches no sealed program source, golden fixture, clerk store, money trail, OpenAPI snapshot or generated type. One independent reviewer (a fresh subagent, the repo's thermo skill plus a falsification pass over every deletion): **PASS, no majors**; it re-ran the callers by its own greps and AST scans and about 22,000 tests (3,303 on the 182 files that name a touched module). Its minors: the commit moves the research-study code-identity digest (any edit under `app/engine` does), and `BotTaskRegistry.list_bots` is now visibly test-only.

The module is otherwise in good shape. The symbol-level caller search over 3,001 names finds almost nothing dead outside the cut (3 names with no mention anywhere; 15 test-only seams, left under the owner's 2026-09-30 ruling). The largest dead item, `algorithm_pseudocode` + `gotchas` (448 lines of registry prose, F1-C1), cannot go without regenerating the OpenAPI snapshot and Frontend types, so it is a handoff slice. No row from kill lists #2703, #2704, #2724, #2725 or #2727 that this ticket may cut is still on master; the rows that remain are the ones the owner's ◇ addenda ruled to stay.

Of the rest: 149 finding rows across the eight slices, 34 tests that fail the bar (lens 6, none in the cut), 109 feature rows plus 8 registry rows, and the owner rulings and cross-module pointers listed at the end.

## What was run

| Check | Result |
|---|---|
| Slice tests (each reader, host venv, `-rs`) | A 273 passed (+ 543 wider), B 307 (+ 92 + 159), C 82, D 264 (+ 125 + 11), E 198 passed + 4 skipped (+ 110), F1 302 (283 not-slow), F2 165 (+ 106), G 264 passed + 1 skipped (+ 164) |
| Skips and xfails in scope | 5 skips in total: 4 in `tests/routers/test_backtest_runs_endpoints.py:22` (needs an ephemeral `POSTGRES_URL`; the daily workflow sets it, `daily-tests.yml:90`), 1 in `app/engine/tests/test_spy_validation.py:182` (no SPY minute data; G-9). `test_ema_crossover_lean_vs_spec.py` skips on `POSTGRES_URL unset` and then a missing LEAN launcher (F2-9) |
| Sandbox failures | `os.sysconf("SC_SEM_NSEMS_MAX")` from `ProcessPoolExecutor` in `test_option_bot_dry_run.py:1611` (A, C): the Bash sandbox; passes unsandboxed |
| `ruff --isolated` S110,S112,BLE001,E722,T20x,DTZ00x,DTZ011,PGH003/4 over the 156 app files | 20 hits; every one read in context: `BLE001` ×8 (all log a structured `action` or fail closed; the unlogged one is E-3), `DTZ007` ×4 (E-6), `DTZ001` ×1 (G, intentional naive epoch), `T201` ×7 (fixture `regenerate.py` tooling, G-7) |
| Repo-config `ruff check app/ tests/` after the cut | clean |
| Symbol census (`census.py`, 3,001 names across 156 files matched by token across py/ts/md/json/yml/sh/cs/html) | 713 names with no non-test reference outside their own file; 3 with no mention anywhere (`OrderType.STOP_MARKET`, `AccountInstanceBinding.cohort_id` — a read-compat field — and `SourceBarLedger.providers_for`, cut); 15 test-only |
| vulture 60 % over the production app files | 320 lines; every candidate that survived the census was caller-searched by its reader |
| Mutation harness (reader C) | about 36 single-gate mutants of `bot_trade_strategy*.py` against 666 / 1,231 / 4,351 tests; 3 unproven gates (C-1..C-3) |
| `export_openapi_contract.py --check` (reader E) | exit 0 |
| Temporal ban list over the scope | no `utcnow`, `utcfromtimestamp`, naive `now()`, `pd.to_datetime` without `utc=True`, `time(9, 30)` / `time(16, 0)`, or second `mcal.get_calendar`. `datetime.strptime` ×4 on request dates (E-6); hard-coded ETH 04:00-20:00 literal (G-17, known gap); UTC-midnight window anchors (E-8) |
| Plan / PRD / audit / rule-file citations in comments | about 30 lines: `registry.py` "per PRD direction" ×6 (sealed provenance data), edge "Step E / Step 3" and lean_format "Phase 5c/5d/5f" (G), five review-round labels in `bot_trade_strategy.py` (C-15), four labels in D, one rule file at `backtest_run_strategy_view.py:51` (E) |
| Repros re-run by the lead | F2-1 (spec `predictions` → 500; also the TimeOfDay/MACD/FixedContracts probes), E-1 (units), E-2 (`cagr=None`); B-C/A-C/E-C verified by own grep, AST scan of 58 `BotTaskRegistry(` calls |
| After the cut | `tests/engine`, `tests/services/bot_runner` + the touched services tests, the five `tests/routers/test_engine_*`, `tests/broker/v2panel`, `app/engine/tests`: 1,562 passed, 24 deselected (`slow`); then `tests/engine/strategy`, `tests/broker/v2panel`, `tests/engine/test_run_gate.py`, `tests/contracts` including `slow`: 1,863 passed; then the bot-runner, warmup and discard tests again after the `_MINUTE_MS` row: 471 passed |

## Corrections the lead made to the readers' reports

- **Slice G proposed cutting `iv30_atm_50d_trading_basis`, `skew_25d`, `term_slope`, `iv_change`, `iv_vol`, `delta_inversion.strike_and_iv_for_delta`, `spread_model.py`.** The readers were handed the kill-list ticket bodies, not their comments. The owner's ◇ addenda on [#2704](https://github.com/tim1016/learn-ai/issues/2704) (2026-09-30) rule that "the `iv30_constructor` functions" and `delta_inversion.py` stay, and that "`option_spread`, `OPTION_SPREAD_FLOOR`, `DEFAULT_K`, `DEFAULT_ALPHA` and `tests/edge/test_spread_model.py` stay". Those rows are **kept**, not candidates. Only G-C5 (a money-trail type) stays an owner-ruling item.
- **Slice E's `test_legacy_spy_ema_key_is_not_offered_in_the_strategy_picker` (E-C8, F1's table) is not a pure duplicate.** Its survivor `names >= EXPECTED_STRATEGY_KEYS` proves the positive half only; the `"spy_ema_crossover" not in names` half is a tombstone (kind 5) with no survivor. Left for the owner's test-bar call rather than cut as a "duplicate" ([#3153](https://github.com/tim1016/learn-ai/issues/3153)'s review failed a cut on exactly that mistake).
- **Slice D's `_migrate_legacy_binding` (D-C2)** is unreachable by a reasoned argument (`run_admission.py` refuses any stored instance and Resume was removed in #2550), not by a run, on a custody-adjacent record: not cut; handoff with the argument.
- **Slice F1's `algorithm_pseudocode` / `gotchas` (F1-C1)** is dead (verified by the lead from the readers' greps), but it changes the OpenAPI snapshot and generated TS: handoff, not this PR.
- **D-2 is #3151's F2**, not new.

## What went into the cut, and what did not

In the cut ([#3172](https://github.com/tim1016/learn-ai/pull/3172)), one commit on `chore/3157-bot-runtime-engine-dead-machinery`:

| Row | Piece | Why it is dead (the grep) |
|---|---|---|
| B-C1 | `DesiredStateRepo.delete` | `rg '\.delete\(\)'` repo-wide: 0 hits; written for the retired IBKR `live_instances` router |
| B-C2 | `validate_strategy_instance_id` re-export in `desired_state.py` | multi-line-aware scan of every `from app.engine.live.desired_state import (…)`: no user; `desired_state.validate_strategy_instance_id`: 0 |
| B-C3 | `"STARTING"` in `bot_registry_projection.py`'s local `Literal` | never assigned; `BotProcessFact.state` excludes it |
| A-C1 | `BotTaskRegistry.__init__(lifecycle_projector=)` | AST scan of 58 `BotTaskRegistry(` calls in app, tests, scripts: none passes it, no `**kwargs`, no subclass |
| A-C2 | `_clerk_run_count` + `import sqlite3` in `test_bot_runner_ema_resume.py` | `rg -w`: 1 hit (its def) |
| A-C3 | `list_bots` fakes in `test_evidence_router.py`, `test_panel_router.py` | `rg -w list_bots app`: only the registry's own def |
| C-X1 | `bot_trade_strategy._MINUTE_MS` | 1 hit (the def); no importer |
| D-C1 | `SourceBarLedger.providers_for` | `rg -nw providers_for .`: 1 hit (the def) |
| E-C1 | `engine.py` second single-symbol guard, redundant `assert`, unused `fired` | guard at :263-264 already raises and nothing between touches `ctx.symbols`; the assert follows the `.date()` it would guard; `consolidator.update`'s return is unused |
| E-C2 | two `if not data_roots` guards in `engine_backtest_service.py` | `_resolve_lean_data_roots` → `resolve_data_roots` returns `[root]` unconditionally; every test patch returns a one-element list |
| E-C3 | `ResolvedRunConfiguration.warmup_start_date` | written once, read nowhere; the model is not serialized |
| E-C6 | `strategy` of `_validation_analytics`, `data_roots` of `_materialize_missing_bars` | ARG001; bodies never read them; one caller each + one test kwarg |
| E-C7 | `if result.equity_curve else None` in `_aggregate_backtest_response` | an early return makes the `else` unreachable |
| G-C6 | `tests/engine/execution/__init__.py` | empty package, no tests; no import of `tests.engine.execution` |

Not cut, with the reason:

| Row(s) | Reason |
|---|---|
| G-C1 .. G-C4 (edge/volatility functions, `spread_model.py`) | owner ruling ◇, #2704 addenda |
| G-C5 `OrderEvent` seven never-written fields | money-trail type; owner ruling |
| F1-C1 `algorithm_pseudocode` + `gotchas` (448 lines + router + OpenAPI + TS) | wire-contract change: handoff slice |
| F2-C1 `spy_ema_normalized_gap.spec.json`, F2-C2 / E-17 `list_fixtures`, `run_engine_backtest` (uncalled routes) | registered routes; owner ruling on "route with no client" |
| D-C2 `_migrate_legacy_binding`; D-C3 Resume hole-fill ledger half; D-C4 third `AdmissionCustodyCut` element (~15 sites); D-C5 JSONL importer; D-C6 `record_decision_session` | custody-adjacent record argued not run; owner ruling first (#2850/#2859 still describe "a later Resume"); batch only; waits on a volume census; owner ruling |
| E-C4 `TradeRecord` fields, E-C5 `mae_pct/mfe_pct` | collides with E-1's fix; stored payload keys change |
| B-C4, B-C5, B-C6 | edit-cuts with call-site changes (1, 3, 9 sites); hygiene slice |
| E-C8 two tests, T-1..T-6 and the other lens-6 rows | lens 6: handoff, not lens 2 |
| `BotTaskRegistry.deploy/list_bots`, `read_checkpoint`, `desired_state()`, `DesiredStateRepo.write`, `BotLifecycleStateRepo.set_*`, `DecodedArtifactCache.pin/unpin`, `rollback_blocked_*`, `external_start_guard`, `evidence_end_seq`, `SourceBarLedger.close` | test-only seams, kept by the owner's 2026-09-30 ruling |
| sealed set (34 files in `program_sources.py`) and the contract values in `registry.py` | Sacred; any byte change re-qualifies all 8 programs |
## Slice A — bot runner core

7 app files, 3,819 lines: `bot_runner.py` (2,891), `bot_runner_errors.py`, `bot_runtime.py`, `bot_crash_diagnostic.py`, `bot_carryover.py`, `bot_clerk_lifecycle.py`, `bot_dry_run.py`. The non-option tests of `tests/services/bot_runner/`, `tests/_helpers/bot_runner/`, `test_bot_runner_ema_resume.py`, `test_bot_crash_diagnostic.py` were read; the option floors and `test_option_bot_*` were run, not audited (ticket #3150).

Tests: 14 files of the list **273 passed, 0 skipped** unsandboxed (24 s); wider runner-exercising set **543 passed, 0 skipped**. The one sandboxed failure is `ProcessPoolExecutor` → `os.sysconf` in the Bash sandbox (`test_option_bot_dry_run.py:1611`). No `skip`, `xfail`, `importorskip` or Postgres gate in the slice. ruff: the two BLE001 hits (`bot_runner.py:2010`, `bot_crash_diagnostic.py:27`) both log a structured action and fail closed; temporal ban list, `print`, `TODO`, `noqa`, `type: ignore`: 0.

**P0/P1: none.** Start, stop, stop-at-end, crash, shutdown, boot recovery, duty settle, Dry Run restoration and Archive were traced; every broad `except` re-raises, logs a structured `action`, or returns a fail-closed value.

### Features

| Feature | Requirement | Entry point | Reachable | Normal / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| Deploy, start admission, lane gates | #1260, #2155/#2351/#2320, #2269, #2912, ADR 0076 d24 | `deploy_with_admission` (`panel_deploy.py:537`) | yes | typed refusals | implemented | `test_drained_lane_start_gate`, `test_go_live_hold_start_gate` (7), `test_registry_admission`, `test_deploy_only_lifecycle` |
| Failed-launch compensation | #2541, #2559, #2667 | `_activate_binding` except branch | yes | proven → `ActivationFailedCleanupProvenError`; unproven → raw | implemented | `test_deploy_only_lifecycle` |
| Supervision and crash evidence | #1260; #2365/#2314/#2444 | `_supervise` | yes | stream end, feed death, refusal codes, exception name | implemented | `test_bot_runner_ema_resume`, `test_bot_crash_diagnostic`, `test_run_ownership` |
| Operator Stop | Button Rule; #2664 | `stop_after_durable_clerk_stop` | yes | fence, Clerk STOP, cancel (5 s), custody proof; timeout leaves provisional (F-A-4) | implemented | `test_registry_shutdown_ordering`, `test_registry_lifecycle`, `test_canary_rollback_stop` |
| Lane-wide Stop | #2268/#2269 | `stop_every_running_bot` | yes | per-bot isolation | implemented | `test_lane_stop_every_bot` (8) |
| Service shutdown | #1260 | `stop_all` ← `main.py:1150` | yes | raises with 2+ running bots when a STOP commit yields (F-A-1) | **partial** | repro below |
| Owner-set end | #2607 | `start_end_watch` ← `main.py:994` | yes | trade bots retry; Dry Run asked once (F-A-2); watch loop never run by a test (F-A-18) | implemented, loop unverified | `test_bot_end` (41) |
| Boot recovery, lease recovery, duty settle | #1263, ADR 0050, #2589 | `run_boot_recovery`, `settle_dead_runs` | yes | failed sweep keeps Start closed | implemented | `test_boot_recovery`, `test_duty_settle` |
| Dry Run restoration; Archive / Clear | #2582, #2668, #2684; ADR 0052 | `start_dry_run_restoration`, `archive` | yes | per-bot isolation | implemented | `test_dry_run_boot_isolation`, `test_duty_settle` |
| Stop custody checkpoint | #2703 addendum 2 | `record_stop_outcome` | yes | written on every trade stop; read only by tests | implemented, write-only | `test_stopped_run_entries` |
| Dry Run activity journal; decision fence | ADR 0076, #2912, #2410 | `DryRunActivityJournal`, `DecisionFenceFeed` | yes | legacy rows lifted; unseen bar defaults to DECIDE (F-A-6) | implemented | `test_dry_run_activity`, `test_signal_adapter` |
| `log_only` mode | #1260 skeleton | `_run_log_only_bot` | no: prod Deploy sends only `dry_run`/`trade` | tests only | unverified in prod (F-A-7) | 98 of 147 test deploys |
| Carryover `ALLOW` | IBKR era | `require_start_configuration` | refused at three layers (F-A-19) | always 409 | dead by refusal | `test_registry_admission.py:294` |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| F-A-1 | `bot_runner.py:1633` `stop_all` | 5 | **P2, reproduced** | `for managed in self._bots.values()` spans `await commit_stop_before_task_cancel`; a cancelled bot's `_supervise` finally calls `_terminal.reap`, which pops the dict, so the next iteration raises `RuntimeError: dictionary changed size during iteration`. The lifespan `finally` (`main.py:1150`) does not guard it, so the rest of teardown is aborted (sweep, trade-updates, Clerk `close()`/lease release) and later bots get no Clerk STOP; boot recovery closes the runs. Trigger: a contended intake fence at the second or later bot's commit. Repro `A/repro/test_repro_stop_all_real_clerk.py` (real SQLite Clerk, two ema bots): `RuntimeError … bot_runner.py:1633`; the same scenario with `list(self._bots.values())` passes. | fix: iterate `list(...)`; loops at 1516 and 2002 already snapshot | none |
| F-A-2 | `bot_runner.py:2034` `_carry_out_due_end` | 1, 3 | P2 candidate, not reproduced | `end_pass_asked_for_ms` is set after any pass that returns normally; `scheduled_end._stop_runs_at_their_end` swallows a per-bot STOP-write failure ("next pass tries again"); a Dry Run's synthetic authority only runs `start_lease_heartbeat()`, so nothing retries and the run trades past its end until restart. | fix: clear the flag if the run is still ACTIVE after the pass | `test_bot_end.py:613` `passes == 2` |
| F-A-3 | `bot_runner.py:1630-1657` | 5 | P3 | `asyncio.wait(…, timeout=5)` on `_end_stop_tasks` and `stopping` never cancels or awaits stragglers; `_stop_at_its_end` can sit in its 45 s proof wait. | fix | none |
| F-A-4 | `bot_runner.py:1368-1381, 2436-2437` | 3 | P3 | A task outliving the 5 s cancel skips `_prove_stop`/`_record_stop`/`_settle_stopped_run`, and `_supervise` skips `release_after_run_end`; the terminal stays `STOPPED_PENDING_CUSTODY_PROOF` (honest copy), but a Dry Run's synthetic authority is not released until Archive or restart. | leave or fix | none |
| F-A-5 | `bot_clerk_lifecycle.py:66-68` | 3 | P3 | `register_alpaca_duty_run` registers without the admission snapshot when the Clerk lacks `supports_revision_bound_admission`; only test doubles take it. Fails open if a future authority lacks the flag. | fix: raise if snapshot given and flag absent | doubles |
| F-A-6 | `bot_runtime.py:81-86` | 3 | P3 | `evaluation_mode_for` pops with default `DECIDE`; fence closed, unseen bar answers DECIDE (`A/repro/fence_default.py`). Not reachable on the prod path. | fix: default from `gate.is_set()` | none |
| F-A-7 | `bot_runtime.py:123-152`, `bot_runner.py:653/696/784` | 2 | P3 | `log_only` reachable only from tests and legacy bindings; `_run_log_only_bot` never calls `evaluation_mode_for`, so `_captured_modes` grows one entry per bar in that mode. | leave (test seam, owner 2026-09-30; 19 test files) | none |
| F-A-8 | `bot_runner.py:533,575` | 2 | P3 | ctor kwarg `lifecycle_projector` passed by 0 of 58 `BotTaskRegistry(` calls | cut A-C1 | none |
| F-A-9 | `bot_runtime.py:21-25,38`; `bot_runner.py:915-916` | 2, 7 | P3 | `_open_run_gate` default factory unreachable (the only `ManagedBot(` passes `run_gate`); activation rebuilds the same gate inline | fix (field-order change, not delete-only) | none |
| F-A-10 | `bot_runner.py:857-866` | 2 | P3 | `reason: Literal["deploy"]` always `"deploy"` | leave | none |
| F-A-11 | `bot_runner.py:564-568, 2758-2778` | 2, 7 | P3 | prod always passes `{"alpaca"}`; the `None → True` branch is test-only; comment still says IBKR bots are "managed by the host daemon". Live for legacy IBKR bindings on disk. | fix comment | none |
| F-A-12 | `bot_runner.py:869-872` | 7 | P3 | operator-visible detail "Use the legacy host boundary…" names a boundary removed by #1813; no test; route layer already refuses non-alpaca | fix wording | none |
| F-A-13 | `bot_runner.py:5`; `test_registry_shutdown_ordering.py:2` | 7 | P3 | Docstring says "a guard test enforces that boundary"; that test (`test_runner_is_daemon_free_by_construction`) was cut in #2759. Exit-taxonomy block (11-20) says operator stop gives `OPERATOR_STOP`; trade mode records the custody outcome; `SCHEDULED_END`, `FAILED_LAUNCH` and refusal codes missing. | fix docstrings | none |
| F-A-14 | `tests/services/test_bot_runner_ema_resume.py` | 7 | P3 | One test, but the helper library for `test_duty_settle`, `test_run_ownership`, `v2panel/test_rehearsal_clear`; holds a second `_tradable_market_liveness` (also `market.py:36`) and a third `_wait_for`; docstring says "resume", which is gone. | fix: move helpers to `tests/_helpers/bot_runner/` | none |
| F-A-15 | `bot_carryover.py:21`, `bot_binding_repository.py:34` | 7 | P3 | private `_atomic_write_json` imported across modules; second implementation in `research/artifact/store.py:36` | leave | none |
| F-A-16 | `bot_carryover.py:67` | 4 | P3 | `recorded_at_ms: int` has no `MAX_TIMESTAMP_MS` ceiling (`DryRunActivity` has it) | fix | none |
| F-A-17 | `bot_runner.py:2441-2443` | 5 | P3 | `_operation_locks` gains one `Lock` per sid named, never pruned (control routes are secret-gated) | leave | none |
| F-A-18 | `bot_runner.py:1969-1981` | 6, 1 | P3, unverified | `start_end_watch` and `_watch_ends` started by no test | fix: one test | none |
| F-A-19 | `bot_runner_errors.py:195-206`; `bot_carryover.py:154-156`; `paper_deploy_service.py:1038`; `panel_deploy.py:1231` | 2 | P3 | `ALLOW` is refused at the deploy view (`carryover_available=False` always), the panel, and `require_start_configuration`; `STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE` is produced only for a hand-built binding (`test_canary_rollback_stop.py:143`). The field is hash-bound. | **owner-ruling** (money evidence, hash-bound) | canary copy, schema Literal, OpenAPI |
| F-A-20 | `bot_runner.py:2780-2797`; `bot_carryover.py:34-47` | 7 | P3 | `_binding_recovery_candidates` keys on the binding's sid but builds the value from the authority's sid (harmless today); `CarryoverBinding` Protocol declares 7 attributes the module never reads | tidy | none |
| F-A-21 | `bot_runner.py:2078-2090` | 3 | P3 | `_stopped_end_candidates.discard(sid)` at 2086 precedes `record_end_carried_out` at 2090; a failing write is never retried in-process and aborts the look for later sids | fix: discard after the write | none |
| F-A-22 | `docs/known-gaps.md:127-136` | 7 | P3 | See Drift. | fix docs | none |
| F-A-23 | `bot_crash_diagnostic.py:24-28` | 3 | leave | `_safe_message` swallows a failing `__str__` with no log, but the evidence row says "could not be rendered" (visible); pinned by a test | leave | none |
| F-A-24 | test names and docstrings | 7 | P3 | "Resume" in `test_go_live_hold_start_gate.py:5,62`; "pause" in `test_signal_adapter.py:270`; unexplained `P1-1..P1-4` in `test_duty_settle.py:219,247,278,483` | fix | none |

### Cut candidates

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| A-C1 | `bot_runner.py::BotTaskRegistry.__init__` kwarg `lifecycle_projector` (533) and the `lifecycle_projector or` at 575 | balanced-paren scan of every `BotTaskRegistry(` in `app/`, `tests/`, `scripts/`: 58 calls, 0 pass it | nothing (import still used) | none |
| A-C2 | `tests/services/test_bot_runner_ema_resume.py::_clerk_run_count` (179-182) + `import sqlite3` (7) | `rg -w _clerk_run_count`: 1 hit (its def); `sqlite3` used only there | nothing | none |
| A-C3 | fake `list_bots` in `tests/broker/v2panel/test_evidence_router.py:84-85`, `test_panel_router.py:207-208` | `rg -w list_bots app`: only the def at `bot_runner.py:2287` | nothing | none |

Not cuts: `BotTaskRegistry.deploy`, `read_checkpoint`, `list_bots` (3 test files), `desired_state()` (test seams, owner 2026-09-30); `DryRunActivity._lift_legacy_authority` and `_omit_what_is_absent` (decorated validators, tested); `synthetic_runtime_for_projection` (reached by `getattr` at `bot_custody.py:106`); re-exports only tests import (`AdmittedBotStart`, `BootRecoveryIncompleteError`, …); every class in `bot_runner_errors.py` is raised somewhere; the census's own=2 rows were each read and are reachable.

### Tests that fail the bar

None in the non-option tests. Borderline, keep: `test_signal_adapter.py::test_live_market_bar_translates_to_numeric_engine_timestamps` (the "no datetime on the engine bar" rule as a test), `test_bot_runner_ema_resume.py::test_unhandled_error_is_preserved_only_on_immutable_run_evidence` (asserts the receipt file and that `lifecycle_state.json` never carries `crash_diagnostic`). Hand-off to #3150: in `test_option_entry_wait_offered.py`, `test_the_offered_range_leaves_time_for_a_cancel_proof` pins three substrings of a copy constant (kind 2) and `test_the_catalogue_serves_that_reason_…` is an identity pin (kind 1/2). Health: 28 redundant `@pytest.mark.asyncio` markers; 98 of 147 registry `deploy(` calls in tests run `log_only`, so they prove the shared wrapper, not `run_trade_bot` or `run_dry_run_bot`.

### Drift

- `docs/known-gaps.md:127-136` ("Doc paths predating this sweep") claims the math index and `engine-authority-map.md` cite ~30 missing `.py` files "plus `tests/services/test_bot_runner.py`". `docs/math-sources-of-truth.md` no longer exists (#2750); an audit of `engine-authority-map.md` (312 paths) finds 1 missing (`tests/engine/test_single_store_tombstone.py`). Delete the bullet or re-scope it to that one path.
- Citations of the removed `tests/services/test_bot_runner.py`: `known-gaps.md:133` and 11 test docstrings (`bot_runner/conftest.py:2`, `test_dry_run_activity.py:3`, `test_signal_adapter.py:4`, `test_registry_shutdown_ordering.py:5`, `test_registry_admission.py:4`, `test_registry_lifecycle.py:4`, `test_trade_bot_ema_and_mechanics.py:4`, `test_trade_bot_exit_rollback_and_warmup.py:4`, `test_trade_bot_liveness_gating.py:4`, `test_trade_bot_sqlite_decisions.py:4`, `broker/v2panel/test_strategy_catalog.py:10`).
- ADR citations of code that moved: `0063…:84` cites `bot_carryover.py:222` (file has 171 lines); `0063…:146,275` cite `bot_runner.py::drained_lane_start_gate` (gone); `0038…:178,197,211,217` cite `bot_runner.py:471`, `:468`, `:1025`.
- Comments citing plans, PRDs, audits or rule files: 0.

### Not verified

F-A-2 (needs a synthetic-authority fixture with a one-shot `submit_stop_run` failure, then two `carry_out_due_ends` looks). F-A-1 in production (reproduced against the real Clerk facade with a synthetic fold-holder, not through `app.main`'s lifespan). The `_watch_ends` loop (read, never run). Live Alpaca timing. Unbounded awaits under the operation lock (`prove_instance_custody`, `reconcile_for_end`, `release_after_run_end`): any bound lives in the broker adapter or Clerk. Legacy pre-authority Dry Run journals on the owner's disk.
## Slice B — bot lifecycle (end, terminal, evidence, projections, quarantine, boot recovery) and `app/engine/live/`

19 app files (4,439 lines): `bot_end`, `bot_run_terminal`, `bot_run_evidence`, `bot_lifecycle_projection`, `bot_registry_projection`, `bot_decision_quarantine`, `bot_boot_recovery`, and the 12 files of `app/engine/live/`. 23 test files: **307 passed, 0 skipped**; extra evidence 92 + 159 passed. Project ruff clean; no ban-list hit.

**P0/P1: none.** Checked and held: the terminal receipt is create-once and written before the projection; custody proof fails closed (`InstanceCustodyProof.unprovable`, `STOPPED_CUSTODY_UNPROVABLE`, 45 s bound); "Ended" text says "sale put in", never "closed"; every loop is bounded; no swallowed exception.

### Features

| Feature | Requirement | Entry point | Reachable | Normal / empty / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| Owner-set bot end: rules, default, early-close clamp, refusals | #2607, #2641, #2663, #2854 | Deploy form → `bot_end_panel.resolve_deploy_end`; `edit_end` | yes | `ResolvedBotEnd` / `no_end` / `BotEndRefused` 400, 409 | implemented | `test_bot_end.py` (37) + `bot_runner/test_bot_end.py` (41) |
| Stop at the end with custody proof from the Clerk's own pass | #2607 | `_stop_at_its_end` → `prove_end_stop_outcome` | yes (`main.py:990`) | proof / no Clerk → UNPROVABLE / logged, end watch retries | implemented | `bot_runner/test_bot_end.py::test_an_end_stops_proof_*` |
| Create-once terminal receipt + lifecycle projection | ADR 0050, #1712, #2574 | `BotRunTerminalRecorder.finalize*` | yes | receipt then projection / conflict raises / projection refusal not surfaced (B-2) | implemented | `test_bot_run_evidence_preserve_terminal.py` (6) |
| SQLite → file lifecycle projection (CAS, re-check) | ADR 0038 §2 | `AlpacaLifecycleProjector._project` | yes | `RECORDED` / superseded, CAS-refused, authority-changed | implemented | `test_bot_lifecycle_projection.py` (8) |
| Roster row, process fact, Dry Run activity | #2912 | `status`, `process_fact`, `dry_run_activity` | yes | `UNKNOWN` when task and lifecycle disagree | implemented | `test_registry_lifecycle.py` (35), `test_dry_run_activity.py` |
| Decision-bar quarantine: count, throttled log, one receipt per reason | #1827, FR-016 | `bot_trade_strategy.py:189` | yes | receipt once / no sink: log only / sink raises → run fails closed | implemented | `test_bot_decision_quarantine.py` (8) |
| Boot recovery, lease revival, periodic duty settle | #1263, ADR 0050, #2589, #2582 | `main.py:921/939` → `run_boot_recovery` | yes | interrupted evidence + STOPPED intent / Clerk-less: gate stays closed / startup aborts (B-1) | implemented | `test_boot_recovery.py`, `test_duty_settle.py` |
| Durable control files: desired_state, lifecycle_state, create-once claims | ADR 0026/0038, #2607 | `DesiredStateRepo`, `BotLifecycleStateRepo`, `create_*_durable_file` | yes | corrupt → typed error; atomic write + dir fsync | implemented | `test_desired_state.py` (18), `test_durable_append_log.py` |
| Order identity: namespace, `order_ref`, exact ownership match | ADR 0008 §1 | `build_bot_order_namespace`, `order_ref_namespace_matches` | yes | over-cap raises | implemented (namespace); partial (ladder, B-4) | `test_order_identity.py` (14), `test_execution_authority_golden.py` |
| Cutover runner-roster evidence | ADR 0059; cutover runbook | `manage_alpaca_sqlite_clerk.py` → `cutover.py:756` | operator script | empty roster tolerated for a never-legacy account | implemented | `test_cutover.py` |
| Catalog-quarantine candidate input (account registry) | same runbook | `plan_catalog_quarantine` | operator script | gated on a file no code writes (B-5) | unverified | `test_catalog_quarantine.py` (helper-written registry only) |
| Binding-ledger read flip | ADR 0030 S4 | `ACCOUNT_BINDING_LEDGER_READ_ENABLED=true` | **no checked-in config sets it** | parity clean → ledger rows | unverified (parked, `engine-authority-map.md:83`) | `test_account_binding_ledger.py` |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| B-1 | `bot_boot_recovery.py::_repair_candidate` (443) | 3, 5 | P2 | One bot's unreadable `desired_state.json` raises `DesiredStateCorruptError` out of `BotBootRecovery.run()`; the candidate loop catches only `AlpacaLifecycleAuthorityUnavailableError` (:295). `run_boot_recovery` re-raises (`bot_runner.py:2150`), `_boot_alpaca_authority` is awaited bare (`main.py:996`), lifespan closes and re-raises. A bad bot aborts the lane's startup and the good bot sorted after it is never repaired. A corrupt `lifecycle_state.json` is skipped with a warning (:435), its sibling is not. Repro: `B/repro_corrupt_desired_state_boot.py` → `run() RAISED: …DesiredStateCorruptError`. The posture "corrupt control intent refuses, never guessed" may be intended. | owner-ruling: per-bot refusal vs whole-lane abort; at minimum wrap in `BootAuthorityPreparationError` | none |
| B-2 | `bot_run_terminal.py::_finalize` (119), `::replace_provisional_stop` (219) | 3 | P2 | `record_terminal` returns `AlpacaLifecycleProjectionResult`; both callers drop it, so `FILE_CAS_REFUSED` / `AUTHORITY_CHANGED` (8 retries) log nowhere. Self-corrects via `settle_dead_runs`. Not reproduced end to end. | fix: structured `action` log on a non-`RECORDED`, non-superseded status | none |
| B-3 | `bot_lifecycle_state.py::BotLifecycleStateRepo.set_roster/set_phase/retire/record_terminal_outcome/reopen_for_deploy`; fields `on_roster`, `retired_reason`, `replacement_strategy_instance_id`, `carryover_policy`, `updated_by`, `reason`, `schema_version`, `retired_at_ms` | 2, 7 | hygiene | Production writes only through `AlpacaLifecycleProjector._project → repo.update`, reads only `phase`, `active_run_id`, `duty_outcome`, `last_transition_at_ms`, `version`. The five methods: 0 app callers, 4 test files. `on_roster` has no reader and is always `True`, though ADR 0038 §3 says it "stays file-backed". `extra="forbid"`, so a field drop breaks old files. | leave: test-only seams, owner ruling 2026-09-30 | ~7 test files seed state with them |
| B-4 | `order_identity.py::classify_ownership`, `OwnershipRung.INTENT_ID/PERM_ID/EXEC_ID` (53-55, 170-201) | 2 | hygiene, money path | Sole production caller passes literals: `sqlite_clerk_transaction_projection.py:1029-1037` sets `perm_id=None, exec_id=None` and three empty frozensets. Three of four rungs (IBKR `permId`/`execId`) have no production input. Pinned by `test_execution_authority_golden.py:107`, `test_order_identity.py:124-181`. | owner-ruling (money path; keep when unsure) | — |
| B-5 | `account_registry.py::read_account_instance_registry`, `account_binding_ledger.py` (whole), `catalog_quarantine.py::_read_account_strategy_instance_ids` (298) | 1, 2 | hygiene / unverified | No production code writes `instance_registry.jsonl` or `binding_commands.jsonl` (`tests/_helpers/legacy_ibkr_artifacts.py`: "Production deliberately exposes no API that authors these"). The registry is read at `<root>/accounts/<ACCT>/`; Alpaca stores live under `accounts/alpaca/<acct>/`. On a tree with no pre-#1583 registry the ceremony can only answer "no disposable Alpaca catalog bindings were found". Reachable from an offline CLI and a runbook, so not dead by the AGENTS.md rule. | owner-ruling; needs the owner's artifact tree | ~355 lines + helper + 5 test files if cut |
| B-6 | `desired_state.py:11-14,28-30,38,56`; `identity.py:3-10`; `engine/live/__init__.py:1`; `live_state_sidecar.py`, `run_status.py` names | 7 | hygiene | Docstrings describe retired code (`run.py start`, `live_state.json`, "indicator-state sidecar"). `live_state_sidecar.py` holds `fsync_parent_dir` + private `_file_lock` imported across modules; `run_status.py` one private `_atomic_write_json` imported by two services. | fix (comments) | none |
| B-7 | `account_registry.py::bot_order_namespace_for_instance` (56) | 7 | hygiene | Re-spells `f"learn-ai/{sid}/v1"` instead of `build_bot_order_namespace`. 0 production callers; 3 test files use it. | edit-cut | `__all__` entry, 3 test call sites |
| B-8 | `bot_boot_recovery.py::BotBootRecovery.__init__` (`del artifacts_root`, 157) | 7 | hygiene | Required parameter deleted on entry; passed by `bot_runner.py:588` and 8 test sites. | edit-cut | call sites |
| B-9 | `desired_state.py:124 updated_at_ms`; `bot_lifecycle_state.py:63 last_transition_at_ms`, `:66 retired_at_ms` | 4 | hygiene | Bare `int`; temporal-rigor requires `MAX_TIMESTAMP_MS` on every `*_ms` field. | leave or add `Field(ge=0, le=MAX_TIMESTAMP_MS)` | none |
| B-10 | `clerk/sqlite/cutover.py:761` ↔ `cutover_roster.py:109,189` | 3, 7 | hygiene (fails closed) | `str(exc).startswith("runner evidence contains no Alpaca bots")` decides "empty roster is not a refusal" across two modules. | fix: typed subclass | none |
| B-11 | `bot_end.py::bot_end_view_attested_absent_sale` (245), `::bot_end_view_waiting_for_operator` (224) | 7 | hygiene | Headline built with `removesuffix(<words>) + "no order sent"`; for a Dry Run the tail differs and it no-ops: `'…sold at its last priceno order sent'` (`B/repro_removesuffix_dry_run_end.py`). Reachable only if a Dry Run repository reports an attested-absent end sale; no such path found. | leave, or build the headline in one place | none |
| B-12 | `bot_registry_projection.py:27` | 7 | hygiene | Local `Literal` wider than `BotProcessFact.state` (`schemas/broker_bots.py`); `"STARTING"` never assigned. | cut | none |
| B-13 | `order_identity.py::DEFAULT_ORDER_REF_MAX_LENGTH = 60`; 25-char bot-name cap (`deploy_submissions.py:121`) | 1 | unverified | The cap comes from ADR 0008 §1's IBKR `orderRef` limit; no ADR sets it for Alpaca `client_order_id`. | owner-ruling (product limit) | — |
| B-14 | `bot_run_evidence.py::record_terminal` (126), `::record_terminal_receipt` (150) | 2, 3 | hygiene | `run_id is None` branches: both callers always pass a `str`; the branch returns silently with no log. | leave or raise | none |
| B-15 | `durable_append_log.py`; `test_durable_append_log.py` | 6 | hygiene | All seven private helpers reachable; failure paths fail closed. Test docstring says "Failure-injection coverage" but holds one happy-path test; symlink-swap refusal and non-POSIX fallbacks untested. | keep; coverage gap on a custody-adjacent primitive | none |

### Cut candidates

| ID | Target | Evidence | Goes with it |
|---|---|---|---|
| B-C1 | `desired_state.py::DesiredStateRepo.delete` (213-221) | `rg '\.delete\(\)'` repo-wide: 0 hits; every file naming `DesiredStateRepo` has no `.delete(` call. Added for the retired IBKR `routers/live_instances.py`. | nothing; `contextlib`, `fsync_parent_dir`, `_confined_path` stay used |
| B-C2 | `desired_state.py` re-export of `validate_strategy_instance_id` (import 33, `__all__` 47, comment 38) | multi-line-aware scan of every `from app.engine.live.desired_state import (…)` finds no user; `desired_state.validate_strategy_instance_id`: 0; the module never calls it | the import line |
| B-C3 | `bot_registry_projection.py:27` `"STARTING"` literal | never assigned; `BotProcessFact.state` excludes it | nothing |
| B-C4 | `account_artifacts.py:34 _safe_account_path_segment` alias | one importer, `account_binding_ledger.py:19,84` | edit-cut |
| B-C5 | `account_registry.py::bot_order_namespace_for_instance` | 3 test importers | edit-cut |
| B-C6 | `bot_boot_recovery.py` `artifacts_root` ctor param | `bot_runner.py:588` + 8 tests | edit-cut |

### Tests that fail the bar

None. Marginal, all keep: `test_run_status.py::test_atomic_write_json_no_tmp_file_left` (kind 1 tautology; survivor `test_stopped_run_entries.py:384` reads the checkpoint back; money-path-adjacent, #2727 left it); `test_bot_lifecycle_state.py` (5 of 6 prove the test-only API of B-3, owner 2026-09-30); `test_order_identity.py::test_dual_read_allowed_set_owns_both_versions` (kind 3, #2727 ruled the file money-path); `test_deployment_validation_deploy_artifacts.py` (manifest names the spec); `test_nyse_calendar.py` (misfiled, subject `app.lean_sidecar.trading_calendar`); `test_bot_decision_quarantine.py::test_the_panel_view_still_mirrors_every_decision_outcome` (lint-as-test).

### Drift

- `docs/known-gaps.md:131` says `app/engine/live/*` no longer exists after #1813; 12 files remain, 11 live or parked.
- `docs/known-gaps.md:598-602` says "#2607 slice 2's scheduled end and #2640 are the eventual fix"; both are closed and the scheduled end ships. Whether `START_REQUIRES_FLAT_CUSTODY` still holds was not verified.
- `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md:425-442`: example `--account-id PA-EXAMPLE` fails `_ACCOUNT_ID_RE` (hyphen).
- ADRs 0008, 0026, 0030, 0033, 0038 cite deleted modules (decision history; not edited).
- Comments naming a plan/PRD/audit/rule file: 0 (soft: `bot_end.py:3` "grill 2026-09-29", `bot_registry_projection.py:62` "fleet-stress T6").
- Latent, in slice A's file: `bot_runner.py:2790-2796` `_binding_recovery_candidates` keys by `binding.strategy_instance_id` but stores a candidate built from the loop's id; unreachable today (`lifecycle_recovery_candidates` returns `()`).

### Not verified

B-1 at lifespan level is read from `main.py:330-345,996`, not run. B-2 not reproduced end to end. B-5 turns on the owner's artifact tree. B-13: Alpaca's own limit not checked. Windows branches of `durable_append_log.py` read, not executed.
## Slice C — bot trade strategy and warmup

2 app files, 2,606 lines: `bot_trade_strategy.py` (2,224) and `bot_trade_strategy_warmup.py` (382); collaborators read for their contracts (`feed_continuity_policy.py`, `decision_session.py`, `lean_sidecar/closing_bar.py`, `market_liveness.py`, `bot_runtime.py` DecisionFenceFeed, `bot_option_run.py` `_UnderlyingBars`, `engine/strategy/signal_program.py`, `run_replay_proof.py`, `clerk/sqlite/runtime.py:2650-2740`). 10 test files, 82 tests: **82 passed, 0 skipped** (12.8 s); no `slow`, skip or xfail in the slice.

**Mutation evidence.** About 36 single-gate mutants of a copy of the two app files, against a narrow set (666 tests), a broad set (1,231 tests: every test importing the module) and `tests/services` + `tests/broker/v2panel` (4,351 tests, for the Stop fence). The one mutant-run failure (`test_option_bot_dry_run.py::test_with_a_real_search_worker_process…`) is the sandbox (`os.sysconf`); it passes unsandboxed. ruff (brief set): 1 hit (`:491`, C-5); repo config clean on both app files and all 10 test files. Temporal ban list: 0 hits in app and tests. `type: ignore`: 3 in app, 15 in tests.

**P0/P1: none.** No gate was found failing open on current code. The hunt found *unproven* gates (C-1 to C-3): a mutant of the gate survives the suite — test gaps, not defects.

### Features

| Feature | Requirement | Entry point | In checked-in config | Normal / empty / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| Live signal strategy loop | ADR 0042/0043, #1730 | `execute_bot_run` → `run_trade_bot` / `run_dry_run_bot` → `strategy_evaluations` | yes (`panel_deploy.py:537`) | unregistered key → `ValueError` (:817); no clerk → `RuntimeError` | implemented | 82 slice tests; `test_ema_trade_bot_matches_first_lean_round_trip` (Sacred) green, untouched |
| Warmup replay + crash-candidate naming (FR-016) | ADR 0042/0043, #1708, #2314, #2850, #2859 | `replay_warmup_bars` | yes | no `captured_decisions` → no recovery (documented) | implemented | 11 warmup mutants all killed (table W) |
| Sealed warmup lookback | #2841 | `_warmup_lookback_days_for` | sealed yes; unsealed :97-101 test/replay only | seal > registry > floor 5 | implemented | mutants `seal_lookback_ignored`, `floor_ignores_contract` killed |
| Closing-bar rule | #2607 | `_refused_on_the_closing_bar` ← `is_closing_bar` (calendar) | yes | ENTER dropped, EXIT stays due; non-session day → False (Clerk gate backstop) | implemented | `closing_bar_gate_off` → 9 failures |
| Staleness gate | #2300→#2345, #2623, #2912 | `_screen_late_decision` | yes | policy None → default allowance (fail-safe) | implemented | `late_enter_not_refused` → 6; `late_exit_refused_too` → 2 |
| ENTER liveness gate (+ traded asset) | #1671, #2912 | `_entry_liveness_refusal` | yes | UNKNOWN/HALTED/CLOSED block; probe error blocks | implemented (ENTER), **unverified (EXIT exemption, C-2)** | `enter_liveness_gate_off_trade` → 4; `exit_also_liveness_gated` → 0 |
| Stop fence (OBSERVE_ONLY discard) | ADR 0042 | runner branches :995-1004, :2090-2109 | yes | mode captured at yield; held bars judged at handoff | implemented at feed/strategy level, **unverified at runner level (C-1)** | mutant → 0 failures in 4,351 tests |
| Pricing stream (trades another asset) | #2912 | `_pricing_stream_scope` / `_price_decision` | yes | 4 refusal reasons recorded; EXIT never waits | implemented | 19 tests; 2 mutants killed |
| Decision anchor + recovered-bucket fallback | ADR 0059 D5.3, #2909 | `_decision_bar_evidence`, `_retained_decision_bar` | yes | | implemented | `test_recovered_bucket_evidence.py` (outside slice) kills the fallback mutant |
| REJECTED receipt → DISCARD (shares) | #1671 AC6, #2623 | :1107-1120 | yes | strategy stays in position, EXIT re-fires | implemented | `test_rejected_exit_is_rolled_back_and_retried`; mutant → 4 |
| Dry Run REJECTED → discard, no journal row | same | :2172-2178 | yes | | **unverified (C-3)** | mutant → 0 |
| Option decision facts / settlement / journal | ADR 0076, #2913 | `_option_decision_facts`, `_settle_option_*`, `_journal_option_fill` | yes | | implemented (via out-of-slice `test_option_bot_*`) | `discard_helper_commits` kills 17 option tests |
| Before-start evaluations | #2639 | `_before_start_recorder` | yes | narrow `except (sqlite3.Error, OSError)`, logged | implemented | `test_warmup_hands_its_evaluations_…` |
| Extended-hours decision session | ADR 0059 D5.2/D5.3 | `require_decision_session`, window threading, `_market_data_live` | **no**: `use_rth=True` hard-coded (`panel_deploy.py:544,687`, `bot_end_panel.py:37`) | | unverified / owner ruling (C-12) | 8 tests with hand-built bindings only |
| One convergence rule ends warmup | PRD #2857 (open) | none | none | | missing, by design | `grep -i "converge\|spawn"` on both files → 0 |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| C-1 | `run_trade_bot` :995-1004, `run_dry_run_bot` :2090-2109: OBSERVE_ONLY branch (Stop fence) | 3, 6 | P2 (money-path gate unproven) | Both branches replaced by `if False:` → 1,231 tests pass; `tests/services` + `tests/broker/v2panel` 4,351 pass. `grep -rn STOPPED_OBSERVE_ONLY tests` → 0. Feed-side capture is tested; no test consumes the tag in either runner. Without the branch the already auto-discarded candidate falls through to `clerk.execute_for_instance`. | fix: one runner-level test per runner (gate cleared before the decision bar → `clerk.calls == []`, receipt `blocked/STOPPED_OBSERVE_ONLY`) | none |
| C-2 | `run_trade_bot` :1018: liveness gate ENTER-only (#1671 AC3) | 3, 6 | P2 | `if intent.kind is ENTER` → `if True`: 0 failures in 1,231. No test decides an EXIT under UNKNOWN/HALTED/CLOSED liveness. | fix: test (ENTER tradable, EXIT with `unknown_market_liveness` must reach `clerk.calls`) | none |
| C-3 | `run_dry_run_bot` :2172-2178: REJECTED → discard + continue | 3, 6 | P2 | `if False:` → 0 failures. If regressed, a rejected Dry Run entry is COMMITted and journalled as a fill (`simulated:…`, :2191). | fix: test with a rejecting synthetic Clerk (fixture exists in `test_a_dry_run_records_the_asset_it_traded…`) | none |
| C-4 | :968-969, :2064-2065 `len(intents) > 1` guards | 3 | hygiene | Unreachable by construction (`signal_program.py:204` builds 0 or 1 intent); mutant: 0 failures. | leave (money-path invariant) | none |
| C-5 | :491 `_market_data_live` `except Exception` | 3 | hygiene | Fails closed (returns False → CLOSED + non-RTH blocks), structured `action=bot_extended_session_liveness_unknown`; logs `str(exc)` only. Lazily evaluated, only on clock CLOSED + `use_rth=False` (C-12). Both outcomes mutant-proven. | leave; optional `exc_info` | none |
| C-6 | :519-524 `_evaluation_mode_for` `except AttributeError` | 3 | robustness | Also swallows an `AttributeError` raised inside a fence implementation, reading the Stop fence as DECIDE. Only implementer today is a dict pop (`bot_runtime.py:81`); `run_replay_proof` relies on the fallback. | fix: `getattr(feed, "evaluation_mode_for", None)`; drops a `type: ignore` | none |
| C-7 | :1123-1133 "Trade bot effect accepted" / `bot_effect_accepted` | 7, 3 | hygiene | Emitted after the if/elif/else ladder, so also for a REJECTED receipt. No consumer. | fix: log only non-REJECTED, or rename | none |
| C-8 | `signal_program.py:228` `SignalSession.traces.append` (live via `_LiveSignalRuntime.settle`/warmup) | 5 | robustness (low) | Never trimmed; no live reader. 20 sessions (7,800 minute bars): `deployment_validation` 7,800 traces, 7.3 MB traced (about 90 MB per 250 sessions); `ema_crossover_signal` 520 traces, 0.57 MB. File is sealed (every program's `artifact_paths`). Same as F1-6. | owner-ruling (re-qualifies all 8) | none |
| C-9 | `docs/known-gaps.md:551-577` | 7 | hygiene | Cites `warmup.py:165`, now a docstring line; the condition is `names_next` :219 + guards :250-255. The `provenance != "history"` guard landed in `10b5f31b` (2026-09-24, #2314), after the entry's 2026-09-17 evidence. Pinned by `test_retained_tail_join.py::test_history_buckets_are_never_flagged_as_crash_candidates` (passes; the guard mutant is killed only by it). Consumer `run_fidelity_over_bars` has no production caller; the receipt it fed was cut (#2755). Engine-parity half already fixed (#2608). | fix (docs): mechanism closed for new runs, entry stale; close it or restate as "legacy ledgers whose warm-up bars carry no `history` tag" | none |
| C-10 | `docs/known-gaps.md:596-602` | 7 | hygiene | Names "#2607 slice 2 and #2640 are the eventual fix"; both CLOSED (2026-09-30). | fix (docs) | none |
| C-11 | `app/services/run_replay_proof.py` (480 lines) + slice comments :123, :251-258, :1210 | 2, 7 | hygiene | No `app/` caller of `run_fidelity_over_bars`; 14 test call sites. `source_bar_ledger.py:821-823` says "No reader remains since the replay-proof service was cut (#2755)", yet `bot_trade_strategy.py:251-258` still says "The replay proof reads it back from here"; `engine-authority-map.md:86` still lists it as canonical. | cross-module pointer; in slice: fix the stale comment | `ENTRY_DISCARDED`/`_COMMIT_WORTHY_OUTCOMES` lose their only external reader |
| C-12 | extended-hours branch (:410-436, :458-461, `_market_data_live`, :1331-1335) | 1, 2 | owner-ruling | No deploy path sets `use_rth=False`; reachable only by hand-built bindings in tests. AGENTS: an opt-in no config turns on is not dead. | owner-ruling: keep as opt-in or retire across clerk/continuity; not a cut | none |
| C-13 | `run_dry_run_bot` vs `run_trade_bot` | 7 | hygiene | About 150 lines copied; C-1/C-3 show the copy's branches unproven. Dry Run correctly lacks the ENTER liveness gate (synthetic authority excluded from `_LIVE_MARKET_CLOCK_AUTHORITIES`; calendar gate applies to every authority, `runtime.py:2681-2692`), but no comment says so. | leave; add a comment | none |
| C-14 | `_build_signal_strategy` :546-565; `strategy_evaluations` :816-817 | 3, 6 | hygiene | All 8 registered strategies have factory + contract, so :565 cannot fire and the :817 guard fires first (untested). The only test reaches :554 via `"retired-strategy-not-in-this-build"`; its name claims :565. | leave (documented defence in depth); rename test | none |
| C-15 | comments `Direction 2` :648, :883, :1177; `AC #9` :1168; `ruling P1` :335 | 7 | hygiene | Review-round labels with no file on master. `FR-016` (20 hits) is established vocabulary (ADR 0043:324, 0053:85). | fix (cosmetic) | none |
| C-16 | history-narrating comments :119-122, :418-419, :838-857, :1023-1026, :1549-1551 | 7 | hygiene | Describe code that no longer exists. | fix (cosmetic) | none |
| C-17 | `docs/ibkr-integration-authority.md:43`; `docs/audits/live-ema-spy-missed-entry-2026-09-17.md:83` | 7 | hygiene | Stale line pointers (156 = end of `_run_log_only_bot`; 584 = `_drain_bar` docstring; warmup 154/167 = docstring lines). | fix (docs): drop line numbers | none |

Warmup mutants (narrow set, 666 tests): `provenance != "history"` guard removed → 1 test; `captured.covers` removed → 12; every known outcome DISCARDed → 14; later run's start not flattened → 2; every stretch named, not only the first → 7; `ran_before` ignored → 1; final `on_force_flat()` removed → 3; contract lookback ignored → 3; seal lookback ignored → 3; option ENTRY_DISCARDED replayed as COMMIT → 8; `replay_warmup_bars` ignores the lookback helper → 3 (the floor e2e test survives, T-5).

Seam pointers (decided): #2300 closed, P0 #2345 fixed, `_screen_late_decision` present and 6 tests kill its mutant; #2307 closed "BREAKS (P2), no bug filed"; #2417 closed 2026-09-25; PRD #2857: nothing landed in either file, so no warmup branch became unreachable.

### Cut candidates

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| C-X1 | `bot_trade_strategy.py::_MINUTE_MS` (:91) | `rg -nw _MINUTE_MS app/services/bot_trade_strategy.py` → 1 (the definition); no importer; the warmup test file defines its own. Introduced `ce006c96` (#2410), its only user left in review fix `f5f83461`. | nothing | none — **in the cut** |

Not cut: `alpaca_paper_strategy_default_symbol` (caller `strategy_catalog.py:489`); `supported_alpaca_paper_strategy_keys` (callers `strategy_catalog.py:433`, :816; the filter is vacuous today but is the documented single rule); the unsealed branch :97-101 and `_WARMUP_LOOKBACK_DAYS` (reachable only from tests and `run_fidelity_over_bars`; Start refuses unsealed bindings, `bot_start_admission.py:671-687`, `run_admission.py:228`; test-only seam); `pricing_bar_wait_ms`, `option_market` (injection seams); `rollback_blocked_entry/exit` (test-only seam); both `len(intents) > 1` guards (C-4). Earlier kill lists name no live symbol in these two files.

### Tests that fail the bar (lens-6 handoff; verdicts rest on mutant kill sets and the survivor's assertion)

| Test | Kind | Survivor (assertion read) | Verdict |
|---|---|---|---|
| T-1 `test_bot_trade_strategy_discard.py::test_discard_settles_the_staged_candidate_as_discarded` | 1 + 4 (asserts a one-line helper forwards DISCARD to the test's own `list.append`) | Mutant `_discard_evaluation` commits → 26 failures, including `test_trade_bot_liveness_gating.py::test_blocked_entry_is_rolled_back_and_can_re_enter` (asserts `[ENTER, EXIT]` reach the Clerk after a refused entry), `test_trade_bot_exit_rollback_and_warmup.py::test_rejected_exit_is_rolled_back_and_retried` (`["ENTER","EXIT","EXIT"]`, receipts `enter_intent, blocked, exit_intent`), `test_failed_enter_late_fill_2348.py` ×3 | delete; orphans in that file: `_placeholder_trace`, `_evaluation`, several imports, half the docstring; the other test stays (C-14) |
| T-2 `test_trade_bot_exit_rollback_and_warmup.py::test_final_rth_bucket_decides_at_once_but_its_enter_is_not_sent` | 3 | `test_trade_bot_last_bar_enter.py::test_a_regular_hours_enter_decided_on_the_last_bar_is_refused_and_nothing_is_sent` (2 params; real facade; `submitted_legs == []`; last receipt `blocked/CLOSING_BAR/close_ms`) and `test_dry_run_activity.py::test_dry_run_refuses_a_market_enter_decided_on_the_last_bar`; mutants `closing_bar_gate_off` (9) and `drain_scan_off` (59) kill T-2 and the survivor together | delete (medium confidence; money path — keep if unsure) |
| T-3 `test_trade_bot_liveness_gating.py::test_closed_liveness_always_blocks_entry_for_an_rth_only_binding` | 3 + cannot fire | Mutant `use_rth=False` in `_liveness_blocks_entry` survives all 666 tests (the test's `_RTH_SESSION` has no window and blocks regardless); same row is `test_market_liveness.py::test_a_closed_clock_needs_both_the_schedule_and_live_bars_to_admit_an_enter`; in production `use_rth=True` ⇒ `session.window is None`, so the mutant is equivalent | delete |
| T-4 same file `::test_closed_liveness_without_a_declared_window_still_blocks_entry` | 3 | same survivor (`blocks(proven=False, live=True) is True`) | delete |
| T-5 `test_bot_trade_strategy_warmup.py::test_replay_warmup_bars_requests_the_floor_for_an_unregistered_strategy` | 1, 3 vacuous | Mutant `lookback_days=_WARMUP_LOOKBACK_DAYS` survives it (floor == constant). Wiring proved by `test_a_sealed_long_period_deploy_warms_on_the_lookback_its_periods_need` (`recorded_lookback_days == 18`) and two more; the floor by `…floors_an_unregistered_strategy_key_at_the_constant` | delete |
| T-6 `test_trade_bot_liveness_gating.py::test_closed_liveness_with_a_proven_phase_but_no_fresh_bars_blocks_entry` params (True,True), (False,False), (False,True) | 3 | all die together under `market_data_live_always_true`; same table as `test_market_liveness.py::test_only_a_connected_unstale_feed_proves_bars_are_printing` | trim to one param |

Kept after check: `test_ema_trade_bot_releases_backtest_chart_bars` (sole killer of `chart_bars_not_released_live`), `test_log_only_bot_unchanged_after_trade_mode_added` (sole test of `_run_log_only_bot`), `test_a_closing_bar_exit_is_not_sent_and_fires_on_the_next_sessions_first_decision` (only test of "stays due, fires next session"), `test_trade_bot_quantity_plumbed_from_binding`, the 7 `test_warmup_coverage_*` tests (only tests of `ibkr_feed.require_warmup_coverage`; misfiled). Stale names, still valid: `test_rejected_exit_is_rolled_back_and_retried`, `test_blocked_entry_is_rolled_back_and_can_re_enter`.

### Drift

Known-gaps entries naming slice files: 1 (:559, C-9); :596-602 drifted (C-10); §8 :139-146 says "seven sealed programs" (eight). Neither app file holds a temporal literal or `datetime`; the only session boundary read is `is_closing_bar` → canonical calendar; bar timestamps are `int64 ms`; tests use pinned ms constants and `session_close_ms_utc(day)`, including the half-day 2024-11-29. No math here; the Sacred EMA parity test is untouched and green. Plan/PRD/audit/rule-file citations in the app files: 0; unresolvable review-round labels: 5 (C-15). TODO/FIXME without issue number, `noqa`, `print(`: 0.

### Not verified

The Clerk-side layer behind C-1 (whether the Clerk would refuse a post-Stop ENTER on its own): C-1 is "runner-level fence unproven", not "an ENTER would reach the broker". Option EXIT refused at the send: live DISCARDs a REJECTED option EXIT; replay reads the receipt outcome (`exit_intent` ⇒ COMMIT) and special-cases only ENTERs (`_disposition`); for shares a REJECTED receipt is written as `blocked` (test-proved), so they agree; whether an option EXIT can be REJECTED after its `exit_intent` receipt is atomic was not established (candidate, not reproduced; needs a read of `runtime.py` option-EXIT acceptance plus a rejecting-broker test followed by a Resume). `tests/routers`, `tests/installation_migration`, `tests/engine` (other than `engine/strategy`) not in the kill set. C-8: whether any reader of `SignalSession.traces` is planned for the live path is the engine file's owner's question.
## Slice D — start admission, binding authority and repository, signal-program admission, source-bar ledger

6 app files (4,981 lines): `bot_start_admission.py` (824), `bot_binding_authority.py` (819), `bot_binding_repository.py` (824), `signal_program_admission.py` (884), `source_bar_ledger.py` (1,265), `source_bar_store_schema.py` (365). 7 test files (5,073 lines): **264 passed, 0 failed, 0 skipped**; neighbour suites 125 + 11 passed. ruff: two hits (`bot_start_admission.py:399`, `:763`), both log a structured action and return a refused UNKNOWN / RECOVERY_UNCERTAIN fact (fail closed). Temporal ban list, `np.allclose`, `TODO`, `noqa`, `type: ignore`, `print(`: 0. 31 commits since 2026-10-01.

**P0/P1: none.** Examined and not promoted: every `except` fails closed; `RunAdmissionInvariantError` cannot fire (a `None` feed is already UNAVAILABLE); decision and activation share one `start_admission_snapshot` cut and `register_alpaca_duty_run` re-verifies the token; `SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED` defaults to False and is set nowhere, which is the owner-documented warning-only posture (ADR 0043) — a pointer.

### Features

| Feature | Requirement | Reachable | Class | Evidence |
|---|---|---|---|---|
| Start preview and execution share one decision in one custody cut | #1776, #1702 | yes | implemented | `test_bot_start_admission` (31) |
| Boot-recovery and unresolved-intent runtime fact | #1808, #2582, #2620 | yes | implemented | 12 tests |
| Market-data admission fact; traded asset checked at Start | #1383, #1415; #2912 | yes | implemented | tests L247-596, L1083-1138 |
| Binding authority selection (primary, Dry Run, sealed Shadow, unbound orphan) | ADR 0050, #2559, #2589 | yes | implemented, **leaks (D-2, D-3)** | 12 passed, repro |
| Immutable instance, append-only runs, outcomes, build evidence | ADR 0034, #1728 | yes | implemented; `_migrate_legacy_binding` unreachable | 39 passed |
| Signal Program seal at Start | ADR 0043, #2696, #2796, #2841 | yes | implemented; raw `ValidationError` (D-4) | 68 passed |
| Running-build proof (digests, receipt, import drift) | ADR 0043, #1735, #2450 | yes | implemented | tests L246-340, 830-1164 |
| Golden Search coverage at admission | #2696 | yes | implemented | neighbour suite |
| Source-bar ledger (bars, journal, events, startup join) | #1740, #1921, #2410 | yes | implemented | 28 passed |
| Resume hole-fill (`retain_warmup_join`, `warm_floor_ms`, `warmup_join`) | #2314 | **no** | **dead (D-5)** | grep and lifecycle test |
| Schema create, pre-#1921 migration, JSONL import | #1921, #1722 | migration yes; JSONL untested | implemented / unverified | migration test; no JSONL test |
| Retention capacity 200,000 per stream | #1740 | yes | fails closed; no rollover (D-12) | limit test |

### Findings

| ID | Where | Lens | Sev | Evidence | Disposition |
|---|---|---|---|---|---|
| D-1 | `signal_program_admission.py::prove_running_program_build` 306-313 | 3 | P2 | All 8 registrations carry a factory, so NOT_APPLICABLE means an *unregistered* key, and its copy still says "compatibility strategy". `trade` or `dry_run` with such a key passes the build gate, registers a run, then dies at `bot_trade_strategy.py:546`. The panel screens it upstream. Repro: `registrations: 8 | without a signal_program_factory: []`; unregistered key → `NOT_APPLICABLE`. | owner-ruling (money-path gate, no P0/P1 reason) |
| D-2 | `bot_binding_authority.py::_start_custody_guard` 516-530, `_open_locked` 333-371 | 3, 5, 2 | P2, reproduced | A Start preview or refused Start of a Dry Run activates `sim:<sid>`: activation record, `clerk.db`, `source_bars.sqlite3` and a registered open runtime; nothing releases it; preview names are per NY minute; boot restores every activation and keeps each open. Repro: three previews → 3 dirs under `accounts/alpaca/`, 3 activation records, 3 open runtimes, no binding written; the next boot restores two. Sim only (no custody, no money), roster is binding-driven so the operator sees nothing, but unbounded in disk, file handles and boot time. **Same defect as #3151's F2 / known-gaps F10 residual** — a pointer; the F10 entry names the wrong file. | fix in a separate PR |
| D-3 | `bot_binding_authority.py:344` | 3 | P3 | A read of an unactivated Dry Run creates `sim:<sid>/source_bars.sqlite3` (+wal/shm) before returning `SYNTHETIC_ACTIVATION_REQUIRED`; the existing test checks only `clerk.db` and activation. This is F10's "partially provisioned" store. | fix with D-2 |
| D-4 | `build_start_program_seal` L177 vs `bot_start_admission.py` L679 | 3, 5 | P3 | Invalid params raise a raw pydantic `ValidationError`, which `_decision` does not catch (only `SignalProgramSealError`); the `_canonical_tuple` docstring is false for the seal. The panel validates first. | fix; no sealed byte moves |
| D-5 | ledger half of Resume hole-fill (see D-C3) | 2 | owner-ruling | Only caller chain is `warm_through_seam`'s `if retained:`, and `retained` is empty at the one warmup of an instance's one run. | owner-ruling |
| D-6 | `bot_binding_repository._migrate_legacy_binding` 561-581 | 2 | — | Unreachable in production (D-C2). | see D-C2 |
| D-7 | third element of `AdmissionCustodyCut` (`ExitTerms`) | 2 | — | Both consumers discard it: `_stored_terms` (L642) and `_terms` (`bot_runner.py:1215`). | D-C4, low value |
| D-8 | `source_bar_ledger.py::providers_for` 1014; `find_by_market_bar` 914 | 2 | — | `providers_for`: 1 grep hit (its def). `find_by_market_bar`: test-only. | `providers_for` cut (D-C1); leave the other |
| D-9 | `record_decision_session` 813-872, table `source_run_decision_session` | 2, 3 | — | Written once per run, read nowhere; docstring says "No reader remains since… cut (#2755)"; no test calls it. | owner-ruling (D-C6) |
| D-10 | `_migrate_legacy_jsonl_if_needed`, `_read_legacy_rows` | 2, 6 | — | 0 tests. Format existed from #1722 (2026-08-21) until SQLite replaced it days later. | owner confirms, then cut (D-C5) |
| D-11 | `close`, `checkpoint_wal`, `SourceBarCheckpointBusyError` | 3 | P3 | Writer ledgers are never closed in production; the "at shutdown (#1740)" docstring claim is false. No harm (backup uses SQLite online backup, which includes the WAL). | leave (test-only seam) |
| D-12 | `SOURCE_BAR_STREAM_CAPACITY` vs `_open_live(use_rth=False)` | 3, 5 | P2 | Capture is unfiltered, so RTH-only bots retain PRE/POST minutes too. At most 960 bars/day gives about 208 sessions to the cap, then a typed crash, possibly holding a position. The floor test models 390/day and warmup+1. No rollover tool exists. | owner-ruling |
| D-13 | `_seal_checks` 703-837 | 1, 3 | P3 | Omits `quantity`, `carryover_policy`, `broker`, `action_plan`, `traded_asset_acknowledgement` despite "Every agreement"; repro keeps PROVEN. | leave; fix the docstring |
| D-14 | `_insert_bar_locked` 1064-1072 | 4, 3 | P3 | Exact redelivery of any retained identity is absorbed silently; temporal-rigor asks that live idempotence be surfaced. Tested contract. | leave |
| D-15 | `bot_start_admission.py:671`, `signal_program_admission.py:559-568` | 2 | P3 | `new_run_binding` never sets `sealed_program`, so the sealed branches have no production caller. | leave |
| D-16 | `external_start_guard` (`bot_runner.py:584`, `bot_binding_authority.py:177-180`) | 3 | P3 | An injected guard replaces the Clerk custody proof; 0 production callers, 40 test sites. | test-only seam, leave |
| D-17 | `PrimaryAccountBindingAuthority.account_id = "real_paper"` L175 | 7 | P3 | Wrong default for shadow and live; no reader. | leave |
| D-18 | `_configure` L1168 | 3 | P3 | `journal_mode=WAL` result unchecked (the fleet store checks it). | leave |
| D-19 | `launch_reason: Literal["deploy"]` | 7 | P3 | 8 tests pass `"resume"` against that annotation. | edit tests to `"deploy"` |

### Cut candidates

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| D-C1 | `source_bar_ledger.py::SourceBarLedger.providers_for` (1014-1021) | `rg -n -w providers_for .`: 1 hit (the def); no string dispatch | nothing | none |
| D-C2 | `bot_binding_repository.py::_migrate_legacy_binding` (561-581, call at 359) | `record_launch` has one production caller (`bot_runner.py:896`, reason `"deploy"`); `run_admission.py:268-282` refuses any stored instance; `project_process_fact` never returns ABSENT | second half of `test_legacy_binding_is_lifted_without_rewrite_then_migrated_on_resume`; `_read_legacy_binding` and `decode` stay | low-medium (binding records are custody-relevant) |
| D-C3 | Resume hole-fill, ledger half: `RetainedWarmupJoin`/`WarmupJoinOutcome` (179-225), `retain_warmup_join`, `record_warmup_join`, `_insert_join_locked`, `warm_floor_ms`, `warmup_join`, `_has_warmup_join_table`, `backfill` delivery, table `source_run_warmup_join` (schema 149-164). Cross-slice: `retained_tail_join.py`, the `if retained:` branch of `startup_join.py`, `build_warmup_join`, `WarmupJoinView`, `BotPanelView.warmup_join`, `panel_data_source.py:276-300,590`, `RESUME_HOLE_*` reasons | ADR 0026 s5 ("resume is deleted"); ADR 0045 amendment (#2550 removed Resume); `rg 'launch_reason="resume"' app` 0; `_supervise` created in one place; `recent_closed_bars` raises on a second call; ledgers are per instance; Frontend never reads `warmup_join` apart from generated types | ledger tests (`retain_warmup_join` ×3, `warm_floor_ms` ×2, `warmup_join` ×6), panel projection tests, an ADR 0053 note | medium; waits on other readers' cuts and **owner ruling first** (#2850/#2859 on Oct 3 still describe "a later Resume"; `replay_warmup_bars` is the same family) |
| D-C4 | third element of `AdmissionCustodyCut` | sites: `bot_start_admission.py` (87, 497, 511, 642), `bot_binding_authority.py:530`, `bot_runner.py:1215`, `runtime.py:800`, `active_protocol.py:78` (`exit_terms_for_instance`), 2 test doubles, 6 yield sites | — | none behaviourally, but about 15 sites for one dict read: batch only |
| D-C5 | JSONL importer (`source_bar_store_schema.py` 26-27, 279-358; `source_bar_ledger.py` 39, 391) | no test; format lived days | — | waits on `find <volume>/accounts -name source_bars.jsonl` |
| D-C6 | `record_decision_session` (813-872), its table, the call at `bot_trade_strategy.py:259` | written, never read | — | owner-ruling |

Not cuts (live): `_RECORDS_UNREADABLE`, `sealed_shadow_access`, `_end_restorer`, `_open_locked` (used inside the file by reachable callers); `_omit_absent_option_order_terms` and `_only_an_option_plan_carries_option_order_terms` (pydantic-decorated); `evidence_end_seq()` and `events(evidence_end_seq=)` (test-only seams, consumer cut in #2755). No row from #2703/#2704/#2724/#2725/#2727 naming these files is still on master.

### Tests that fail the bar

| Test | Kind | Survivor (assertion read by reader) | Action |
|---|---|---|---|
| `test_signal_program_admission.py::test_seal_signal_semantics_come_from_the_registry_contract` (295) | 3, 1 | L120 asserts the fresh seal's two hashes equal the committed legacy seal, covering every field compared | delete |
| `…::test_ema_registered_defaults_are_covered_for_live_admission` (176) | 3 | L152 asserts the same `parameters_match_validated_settings is True` on the same params | delete |
| `test_bot_binding_repository.py::…migrated_on_resume` (568), second half | 5 | first half stays | edit with D-C2 |
| `launch_reason="resume"` in `test_bot_binding_repository.py` L202, 226, 250, 256, 283, 325, 594 and `test_signal_program_admission.py` L385 | 5-adjacent | same assertions hold with `"deploy"` | edit |

Keep: `test_signal_program_admission.py` L319, L338 (weak, assert only `UNPROVEN`); `test_signal_program_crash_replay.py` (until D-C3 is ruled); `test_signal_program_retention_floor.py::…covers_warmup_plus_one_open_cycle` (docstring concedes it "only guards absurd inputs"; see D-12); ledger `test_close_*` ×2 (test-only seam).

### Drift

- known-gaps F10 (`docs/known-gaps.md:247-252`) is still open and should name `bot_binding_authority.py:344-352` and `:516-530`.
- `docs/architecture/engine-authority-map.md:71` is stale: names `reconstruct_legacy_program_seal` (removed, ADR 0043 amendment 2026-09-27), says "Start/Resume" three times, and says UNCOVERED is refused "unless … `account_mode="paper"`" while `run_admission.py:223` refuses `bot.mode != "dry_run"`.
- ADR 0053:85 names `append_backfill` (now `retain_warmup_join`) and a Resume that #2550 removed, with no "Resume removed" amendment (0045 has one).
- Stale "Start/Resume" or "resumed run" wording: `bot_binding_repository.py:153, 534, 537`; `source_bar_ledger.py:184, 479, 482, 560, 832`; `test_signal_program_admission.py:915, 1148-1151`.
- Comments citing an audit or ruling label (4): `signal_program_admission.py:114` and `:279` ("H11"), `source_bar_ledger.py:143` ("ruling P4"), `bot_start_admission.py:504` ("WP2").
- `bot_binding_repository.py:34` imports private `_atomic_write_json` from `app.engine.live.run_status`.

### Not verified

Whether any clerk volume still holds `source_bars.jsonl` (D-10). D-12 uses 960 bars/day as an upper bound; the live feed's extended-hours minute count was not measured. **Backup blind spot (cross-slice, to confirm):** `recovery._snapshot_source_bar_ledger` snapshots `accounts/alpaca/<clerk account_id>/source_bars.sqlite3`, but every non-Dry-Run ledger lives under `paper:<sid>`, `shadow-evidence:<sid>` or `live-evidence:<sid>`, so `source_bars` in the manifest looks always null for a real account — found by grep, not reproduced. D-5 argued from the Start path only. Bodies past line 130 of `test_signal_program_crash_replay.py` not read. D-1 reachability grepped in `app/` and `scripts/` only.
## Slice E — backtest service, routers, results and the engine core

15 app files (5,421 lines): `engine_backtest_service.py`, `backtest_run_strategy_view.py`, routers `engine.py`, `strategy.py`, `spec_strategy.py`, `backtest_runs.py`, `engine/engine.py`, `run_gate.py`, `results/*`. 22 test files: **198 passed, 4 skipped** (all four are `test_backtest_runs_endpoints.py:22`, ephemeral `POSTGRES_URL`; the daily workflow sets it, `daily-tests.yml:90`). Extra 110 passed on statistics, spec-router and evaluation-boundary tests. `export_openapi_contract.py --check` exits 0 (no contract drift on these routes).

**P0/P1: none.** The closest is E-1, rated P2 because it is a backtest report, not live state; the owner may re-rate.

### Features

| Feature | Requirement | Entry point | Reachable | Normal / empty / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| Engine backtest workflow (gate, core, aggregate, persist) | #1999, #1941, #2445, ADR 0049 | `execute_engine_backtest` from the jobs engine worker | yes | ok / zero bars → `success=false` / refusal → envelope | implemented | warmup_gate, lake_materialization tests |
| Sync `POST /api/engine/backtest` | #1999 | `routers/engine.py::run_engine_backtest` | registered, **no client** (E-17) | same service | implemented, uncalled | `rg "/engine/backtest"`: OpenAPI snapshot and docs only |
| Warmup → evaluation boundary | PRD #1925/#1926 | `engine._enter_evaluation` | yes | primed / warmup short → fails closed | implemented | test_engine_warmup_gate, summary_run |
| One-backtest gate and queued cancel | #1957, #1990 | `run_gate.one_backtest_in_flight` | always | queue / re-entrancy / release on raise | implemented | test_run_gate (11) |
| Fill modes incl. `decision_minute_open` | #2599 | `FillModel`, engine loop | yes | each mode | implemented | test_engine_fill_modes |
| Closing-bar rule | #2607 | `_drain_staged_signal_program` | yes | ENTER skipped / EXIT next session / last-day synthetic exit | implemented (known-gaps P2 "holds overnight" open) | test_engine_closing_bar_rule |
| Refused decision bars reported | #2909 | `QuarantinedDecisionBar` | yes | short bucket decided / mis-clocked refused | implemented | test_engine_quarantined_decision_bars |
| Cash dividends in LEAN-compat profile | #2903, #3000 | `_profile_dividends` | **opt-in**: `compatibility_profile="us-equity-raw-ibkr-v1"` | ex-date held / none | implemented; escape path E-7 | test_engine_dividends |
| Marked-equity statistics | #2447, #2448 | `statistics.summarize` | yes | ok / `equity<=0` raises | implemented, defects E-2, E-5 | test_statistics, statistics_basis |
| Python "LEAN statistics" panel | #2424, #2447 | `compute_lean_statistics` | yes (Strategy Lab deep dive) | ok / no trades → absent | **partial, wrong units** (E-1) | repro; no test computes its numbers |
| LEAN native reproduction | oracle v1 | `reproduce_lean_total_performance` | yes (sidecar) | 66 numeric + 25 formatted values match | implemented | test_lean_statistics |
| Equity envelope v2 | ENG-006 | `equity_downsample.*` | yes | caps, extrema, staircase | implemented | test_equity_downsample + golden ENG-006 |
| Strategy-view replay | #2639 D13, #2800 | `GET …/backtest-runs/{id}/strategy-view` | yes | ok / 409 with reason | implemented (DB read faked) | test_backtest_run_strategy_view (15) |
| Run history list / detail / notes | PRD #1929 | `routers/backtest_runs.py` | yes | — | **unverified locally** | 4 skips |
| Engine chart; strategy catalogue; LEAN source; spec backtest + fixture | #2771 etc. | `/api/engine/{chart,strategies}`, `…/lean-source`, `/api/spec-strategy/*` | yes (clients named in E report) | 400 mapping etc. | implemented | chart, strategies, spec_strategy_coverage tests |
| Spec fixture list; options strategy analyze | — | `GET …/fixtures`; `POST /api/strategy/analyze` | list **no client**; analyze yes | analyze: 500 for any failure (E-16) | implemented | tests only / `PolygonService.cs:227` |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| E-1 | `results/lean_statistics.py::compute_lean_statistics`, `build_daily_equity` (555-948); `engine_backtest_service._format_trade_record` (359) | 4, 3, 1 | **P2, reproduced** (owner may re-rate) | `TradeRecord.pnl = trade.pnl_pts` (per-share points) is read as dollars; quantity ignored. One 100-share +2.00/share winner (real $200): `trade.total_profit_loss 2.0`, `runtime.fees 0.0` (hard-coded L943), `runtime.net_profit 2000.0` (`pnl_pct × start_capital`), `average_win_rate 2e-05` (expected 0.002). Also `total_orders = n_trades` (L946), arithmetic annualization (L809) vs the compounding oracle path (L293), UTC daily buckets (L571-583). Strategy Lab labels the panel "LEAN native statistics" (`strategy-lab-deep-dives.component.ts:62-72`). No test computes its numbers; the header cites `verify.py`, not in `git ls-files`. Re-run by lead: same output. | fix (handoff); owner ruling on the label | Python-side `native_statistics` in golden `engine-lab-compatibility-95-96-v1/expected/pair.json` (golden test asserts only the LEAN side) |
| E-2 | `results/statistics.py::compute_portfolio_statistics` (582-608) | 3 | P2, reproduced | `cagr`, `calmar` assigned only inside `if max_dd > 0`: a monotonic +9% run gives `cagr=None, calmar=None`; with one 1% dip `cagr=0.09`. A test comment (`test_engine_stats_extended_fixtures.py:174`) already works around it. Re-run by lead. | fix | none |
| E-3 | `routers/spec_strategy.py` 254, 189 | 3 | P2 | 254: `except Exception` returns `data source unavailable: {exc}` with no log (290-292 uses `logger.exception`). 189: fixture-list `except` logs only the path and silently drops the fixture. | fix | none |
| E-4 | `schemas/engine_backtest.py` `from_date/to_date`; `SpecBacktestRequest.start_date/end_date` | 5 | P2, reproduced | `to_date="9999-12-31"` accepted; `iter_dates` walks every day → `OverflowError` after 21.1 s with the engine gate held. `/chart` has `require_schedulable_end` (#2771); `/backtest` and spec route do not. `from_date > to_date` accepted (fails closed). | fix | none |
| E-5 | `schemas/engine_backtest.py` `commission_per_order`, `slippage_per_share`, `initial_cash`; same on `SpecBacktestRequest` | 5 | P2, reproduced | `ge=0` admits `Infinity` (JSON); `initial_cash=0` accepted, then every run fails "equity at timestamp 0 must be greater than zero" (`max_drawdown` reports a sample index as "timestamp"). | fix | none |
| E-6 | `engine_backtest_service.py` `_apply_overrides` (240, 243), `_parse_iso_date` (369) | 5 | P2, reproduced | Schema pattern admits `2024-02-30`: `auto_fetch=False` → 200 `success=False 'day is out of range for month'`; `auto_fetch=True` → 400. | fix: validate calendar date in schema (422) | the DTZ007 sites |
| E-7 | `engine_backtest_service.py` 910, `_profile_dividends` 460-499, `replay_engine_run` 579 | 3 | P2, traced | `_reader_and_engine` runs outside the try that ends at 908; "factor file changed/gone under the receipt" escapes as a raw 500 (replay: 500 not 409). | fix | none |
| E-8 | `_validation_analytics` (1076) | 4 | P2 | Date-anchored window endpoints built as UTC midnight; `et_midnight_ms` used for every other anchor. | fix | none |
| E-9 | `statistics.py::validate_statistics` (495-542), `validate_trade_log` `invalid_win_rate` (482) | 3 | P2 | Cannot fire on production input: `summarize` feeds `_finite_or_none`-sanitised values (L716), `win_rate` is always in [0,1]; it only logs. | fix: delete with its tests | `TestValidateStatistics*` |
| E-10 | `engine_backtest_service.py` 1426 | 3 | robustness | `symbol=… if strategy.ctx.symbols else "SPY"` unreachable (engine refuses no-symbol at engine.py:261) but would persist a plausible wrong ticker. | fix | none |
| E-11 | `engine.py` 404-408, 417, 635-640 | 3 | robustness | Runtime guards written as `assert` (stripped under `-O`). | leave | none |
| E-12 | `test_engine_best_effort_analytics.py::test_a_lean_statistics_failure_leaves_the_run_intact` | 6 | P2, reproduced | The monkeypatched `compute_lean_statistics` is never reached (`object()` bar has no `start_ms`; same `except` swallows it): `monkeypatched … called: False`. | fix the stand-in | none |
| E-13 | `test_backtest_runs_endpoints.py:94`, `test_engine_warmup_gate.py:135` | 4, 6 | hygiene | `pytest.approx` with default tolerances. | fix | none |
| E-14 | `routers/backtest_runs.py` 43-45, 101-121; `schemas/backtest_runs.py:211` | 5 | robustness (not reproduced) | `_view_cache` (8 views) has no TTL; key does not include the lake state a replay reads; `notes`, `{run_id}` unbounded. | fix / leave | none |
| E-15 | docs/comments (see Drift) | 7 | hygiene | 9 stale statements. | fix | — |
| E-16 | `routers/strategy.py` 32-37 | 5 | hygiene (#3155's) | `except Exception` → 500 with exception text for every failure. | leave | none |
| E-17 | `routers/engine.py::run_engine_backtest`, `routers/spec_strategy.py::list_fixtures` | 2 | owner ruling | No Frontend, Backend, script or test client (tests call the handler). In no kill list. Removal regenerates the OpenAPI snapshot and Frontend types (serial merge). | owner-ruling, not cut | OpenAPI snapshot, 2 doc mentions |
| E-18 | `tests/fixtures/golden/lean-statistics-oracle-v1/`, `engine-lab-compatibility-95-96-v1/` | 4 | hygiene (Sacred) | Neither in `golden/manifest.json` (56 entries). First has a fixture-local manifest + regen script but no `attribution.md`; second has `attribution.md`. | owner-ruling | none |
| E-19 | `statistics.py:3` vs `:191` | 7 | hygiene | Header formula is absolute drawdown; code and function docstring fractional. | fix | none |
| E-20 | `EngineBacktestRequest` `AliasChoices` `start_date/end_date` | 7 | pointer, live not dead | Docstring says "removed in PR (iii)" but `strategy-lab-runner.service.ts:320-321` still posts them. | leave; fix docstring | none |
| E-21 | `routers/engine.py:21`; three "parse YYYY-MM-DD → 400" copies (`_parse_iso_date`, `spec_strategy._parse_date`, `routers/research_runs.py:155`) | 7 | hygiene | Cross-module private import; duplicate utility. | fix | — |

### Cut candidates

`engine.py` is parity-proven core, so a thermo reviewer is needed.

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| E-C1 | `engine/engine.py::_run` 294, 296-301 (second single-symbol guard + `assert`) and 377-380 (`fired = consolidator.update(...)` / `if fired is not None: pass`) | Guard at 263-264 already raises; nothing between touches `ctx.symbols`; `rg "Phase 1 engine supports a single symbol only"` 1 hit, no test; `fired` assigned and tested, never used | unreachable raise; keep bare `consolidator.update(minute_bar)` | none |
| E-C2 | `engine_backtest_service.py` 570-571, 846-850 `if not data_roots: raise …` | `resolve_data_roots` returns `[root]` unconditionally (`policy_store.py:86-88`); message names `LEAN_DATA_ROOT`/`LEAN_DATA_CACHE` which no app code reads; 0 tests | both guards | none (empty list fails closed as zero bars) |
| E-C3 | `ResolvedRunConfiguration.warmup_start_date` (172), writer (287), docstring clause (163-166) | `rg warmup_start_date .`: only these 3 lines; model never dumped | field, expression | none |
| E-C4 | `TradeRecord.trade_type`, `.cumulative_pnl_pct`, `.indicator_snapshot`; `_format_trade_record` (354, 361, 363); `cum_pnl` loop (998-1002) | One constructor; one consumer reading `.pnl/.pnl_pct/.*_timestamp`; only reader is one test assertion (`test_engine_trade_ms_timestamp.py:75`) | 3 fields, loop; edit that test | low; collides with E-1's fix |
| E-C5 | `statistics.py::PortfolioStatistics.mae_pct/.mfe_pct` (110, 618), `summarize` keys (710), docstring | repo-wide `mae_pct|mfe_pct`: only `statistics.py`; literal `None` | 2 fields, 2 keys | stored `statistics` dict loses two always-null keys |
| E-C6 | `_validation_analytics` param `strategy` (1035) + caller (1181); `_materialize_missing_bars` param `data_roots` (667) + caller (882) | ruff ARG001 at both; bodies never read them; 1 caller each + 1 test kwarg (`test_engine_best_effort_analytics.py:56`) | 2 params | none |
| E-C7 | `_aggregate_backtest_response` 1251-1255 `… if result.equity_curve else None` | return at 1228-1230 makes the `else` unreachable | conditional | none |
| E-C8 | `test_engine_strategies_endpoint.py::test_legacy_spy_ema_key_is_not_offered_in_the_strategy_picker`; `test_engine_lake_materialization.py::test_flag_on_run_reads_the_lake_tree_and_nothing_else` | see tests | the 2 tests | none |

### Tests that fail the bar

| Test | Kind | Survivor (assertion read) | Action |
|---|---|---|---|
| `test_engine_strategies_endpoint.py::test_legacy_spy_ema_key_is_not_offered_in_the_strategy_picker` | 5 tombstone | `test_all_registered_strategies_have_algorithm_and_gotchas` asserts `names >= EXPECTED_STRATEGY_KEYS` | delete |
| `test_engine_lake_materialization.py::test_flag_on_run_reads_the_lake_tree_and_nothing_else` | 3, 5 (flag-era; one store since #1893; `total_trades==0` also holds for the failure envelope) | `test_run_without_evaluated_bars_reports_missing_data` asserts `success False`, exact error, 0 trades, `run_verdict None` | delete (medium confidence) |
| `test_engine_best_effort_analytics.py::test_a_lean_statistics_failure_leaves_the_run_intact` | 4 (patch never reached) | none covers that branch | **fix, do not delete** |

Kept (borderline): `test_a_complete_session_records_no_refused_decision_bar`, the two `…advertises…lean_twin` tests, `test_a_run_replays_on_its_code_whatever_data_receipt…`.

### Drift

- `docs/known-gaps.md:76-80`: "`force_flat_at` deleted… three remain" — the field is gone (0 hits), the count "three" cannot be matched; there are now 5 date-string request fields in this slice (the first two accepted by ADR 0022's 2026-10-09 amendment). L97-105 "clock constants, about twelve": this slice has 0. L575-602 "Backtest vs reality" still true (pointer).
- Stale statements (E-15): `routers/engine.py:3-9` ("single strategy `spy_ema_crossover`", "`/api/backtest` pipeline"), `_resolve_lean_data_roots` docstring, `EngineBacktestRequest` docstring, `run_gate.py:17-20`, `statistics.py:3`, `lean_statistics.py:4,701`, `test_engine_strategies_endpoint.py` header (`orb`, a `__main__` runner), `test_engine_lake_materialization.py` (six `flag_on_run_*` names; L103 names a test that does not exist).
- Comment citing a rule file: 1 (`backtest_run_strategy_view.py:51` "numerical-rigor.md"). `type: ignore` ×2.
- Same stale "LEAN_DATA_ROOT or LEAN_DATA_CACHE" text at `app/research/ml/generate_prediction_set.py:164` (outside the slice).

### Not verified

Postgres-gated route tests (list/filter, detail + parity verdicts, >500-trade truncation, notes round trip): the Podman socket is not available in the sandbox; unverified locally. E-1 panel not rendered in a browser. E-7 not driven end to end. LEAN's per-slice order not re-derived from `references/`. `persist_engine_response_sync` is stubbed in every service test.
## Slice F1 — strategy registry, signal-program machinery and the sealed programs

19 app files (4,740 lines): `registry.py` (2,568), `program_sources.py` (280), `signal_program.py` (409), `base.py` (274), `params.py`, `decision_explanation.py`, `normalized_gap.py`, `signal_intent.py`, `strategy_view.py`, `__init__.py`, and 8 `programs/*.py`. 19 test files (4,323 lines): **302 passed, 0 skipped** in 210 s (two `slow`: receipt `--check` 55 s, corpus regeneration 44 s); `-m "not slow"`: 283 passed, 19 deselected, 15 s (the PR tier, no skips). Context runs 241 + 2 passed. ruff (brief set) 0 hits; `--select F,ARG,ERA,B`: only `B024`/`B027` on the abstract `Strategy` hooks and one false-positive `ERA001`. `type: ignore` ×11 (8 on `build=lambda` in `registry.py`, 3 in `base.py`). 27 commits since 2026-10-01.

**P0/P1: none.** Nothing in this slice can place, duplicate or fail to cancel an order, and no gate fails open.

### Sealed set — UNTOUCHABLE

`program_sources.py::DECLARED_PROGRAM_SOURCE_PATHS` is the union of 16 tuples: **34 files**, all existing. In this slice, 14 of 19 files are sealed: `base`, `decision_explanation`, `normalized_gap`, `params`, `signal_intent`, `signal_program`, and the 8 `programs/*.py`. Also sealed (other slices): `algorithms/_rsi_range_base.py` + 8 algorithms, `indicators/{base,ema,rsi,sma,macd,adx,supertrend}.py`, `consolidators/trade_bar_consolidator.py`, `data/trade_bar.py`, `lean_sidecar/trading_calendar.py`, `utils/timestamps.py`. Hashed by the #2450 anchor and bound to `app/data/signal_program_build_receipts.json`; the `--check` test passes at HEAD.

Not sealed by bytes, but not free: **`registry.py` contract values** (`signals`, `exit_eligibility`, `parameter_units`, `validated_settings`, `validated_symbols`, `golden_trace_root`, `program_version`, all of `numerical_provenance`) reach `ConfiguredSignalProgramSeal.semantic_hash()` (`schemas/signal_program_seal.py:296-345`), so editing a provenance string re-seals every bot; `description`, `algorithm_pseudocode`, `gotchas`, `strategy_view`, `deploy_code`, `lean_*` are outside it. `program_sources.py` must keep zero imports; `strategy/__init__.py` and `programs/__init__.py` must add none (their laziness lets the anchor hash first). The trace-corpus JSON and the receipts file are golden. `strategy_view.py` is free to edit.

Waiting on a re-qualification: `TradeBar.volume: int` is still true at `trade_bar.py:45` (known-gaps line 82 open); `9364bd70` (2026-10-09) re-qualified the eight programs and did not take the annotation fix.

### Registry

8 keys; `supported_alpaca_paper_strategy_keys()` returns the identical 8; deployability is `signal_program_factory is not None`.

| Key / code | Category | Program version | Decision clock | Lookback d | LEAN twin | Refusal | Note |
|---|---|---|---|---|---|---|---|
| `ema_crossover_signal` / ema | prod | ema-crossover-signal/v1 | 15 min fixed | 5 | yes (4 params) | – | schema v2 at 5/10/5, v3 otherwise; atol 1e-9; 5 parity ids |
| `ema_previous_crossover` / emap | prod | …/v2 | 15 fixed | 5 | yes (8) | – | atol 1e-6 (ENG-010 fills) |
| `sma_crossover` / sma | prod | sma-crossover/v1 | `resolution_minutes` | 7 | none | – | self-consistency only |
| `rsi_mean_reversion` / rsi | prod | rsi-mean-reversion/v1 | `resolution_minutes` | 5 | yes (policy param) | – | provenance understates ENG-009 (F1-5) |
| `deployment_validation` / dv | harness | deployment-validation/v1 | 1 min | 1 | yes | – | `experimental_notice` |
| `spy_strategy_a` / sa | prod | spy-strategy-a/v1 | `resolution_minutes` | 9 | none | MACD fast<slow | description claims TradingView (F1-4) |
| `spy_strategy_b` / sb | prod | spy-strategy-b/v1 | `resolution_minutes` | 7 | none | MACD fast<slow | |
| `spy_strategy_c` / sc | prod | spy-strategy-c/v1 | `resolution_minutes` | 7 | none | – | |

Consumer check: every `SignalProgramContract` field is read in `signal_program_admission.py`; every `ViewCheck`, `ViewValue`, `ViewGate` field in `app/services/strategy_view.py`; every `Comparison` member by an algorithm; every registry helper and constant is reachable (no unused `_*_VIEW`). `StrategyRegistration` fields no registration ever sets: `hidden_params`, `catalog_visible` (always True), `supported_resolutions` (always `{"minute"}`); `algorithm_pseudocode` and `gotchas` are set but read by nothing rendered.

### Features

| Feature | Requirement | Entry point | Reachable | Normal / empty / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| Registry dispatch / catalog | ADR 0043, #1730 | `_STRATEGY_REGISTRY` → `GET /api/engine/strategies`, deploy, runner | yes, 8 keys | unknown key → 404 / "not live-executable" | implemented | `test_every_registration_constructs_its_params_with_a_symbol`, endpoint tests |
| Staged decision cycle | #1725, #1730 | `SignalSession` / `SignalProgram` | yes, all 8 | wrong width or out-of-order bar quarantined | implemented | discard_safety ×8, exit_and_fill ×16, mode_parity ×8 |
| Decision-clock enforcement and quarantine surfacing | #1733, #1827, #2909 | `advance` → runner `surface_quarantines` | yes | retained, throttled warning, one receipt | implemented | `test_signal_program_decision_clock.py` (11) |
| Sealed build identity | ADR 0043 d2, #1728, #1735, #2450 | `program_sources.py`, `SignalProgramContract` | yes | drift fails closed | implemented | matrix closure tests; `--check` exit 0 |
| Parameter-resolved seals | #2696, #2796 | `signals_for` etc. | yes | default dump byte-identical to pre-#2696 | implemented | resolved-series and validated-settings tests |
| Warmup lookback follows periods | #2841 | `resolved_warmup_lookback_days` | yes | bar longer than a session → refusal | implemented | repo sweep + reader sweep (F1-9) |
| Deploy refusal of impossible params | #2841 | `params_refusal` | Deploy only | elsewhere the constructor raises | partial (F1-7) | `test_paper_deploy_service_symbol_profile.py:164` |
| Strategy view / decision explanation | #2639 | `strategy_view` | yes | missing check → raw-id wording; unrenderable row logged | implemented | declarations test, explanation test ×16 |
| Deploy code | #2551 | `validate_deploy_codes` at import | yes, 8 unique | duplicate raises `StrategyCatalogError` | implemented | `test_deploy_submissions.py:100` |
| LEAN twin descriptors | #2696, #2904 | `lean_*` fields | yes (ema, emap, rsi, dv) | unrepresentable params → "unavailable" | implemented | `test_parity_companion.py` (not run) |
| `algorithm_pseudocode`, `gotchas` | "shown in the frontend picker" (`registry.py:353-360`) | `GET /strategies` | served, never rendered | n/a | partial, dead read chain | F1-1 |
| `hidden_params`, `catalog_visible` | registry capability | `public_params_schema`, 9 consumer sites | no registration sets either | n/a | unverified, no producer | F1-2 |
| `supported_resolutions` daily | `registry.py:349-351` | backtest service, grid-search preflight, Frontend | no registration declares daily | daily always refused | unverified | F1-3 |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| F1-1 | `registry.py::algorithm_pseudocode`, `gotchas` (353-361 + 16 blocks, 448 lines); `routers/engine.py:72-81,128-129` | 2 | hygiene | Only renderer, `lean-engine/strategy-detail-tab`, was deleted in `5f227f8b` (#2754, 2026-09-30). Remaining readers: a TS interface (`strategy-lab.models.ts:42-43`) and generated `broker.types.ts:28253,28261`. Router comment `engine.py:73` ("rendered in the frontend strategy picker") is false. The unrendered prose has drifted (Strategy A: "Warmup is 200 bars" vs a 50-bar sealed series). | cut, cross-stack: handoff (F1-C1) | doc-pin test; OpenAPI and TS regeneration |
| F1-2 | `registry.py::hidden_params` (365), `catalog_visible` (368), `public_params_schema` / `hidden_params_present` (413-443) | 2 | hygiene | `hidden_params`: last registration removed in `75f74581` (2026-10-06), whose message says "The hidden-parameter mechanism stays". `catalog_visible=False`: last registration removed in `c35884b6` (2026-08-28). 9 consumer sites read an always-default value; 4 tests monkeypatch `hidden_params`, 1 replaces `catalog_visible`. | `hidden_params`: leave (decided). `catalog_visible`: owner-ruling | `_reject_hidden_params`, `engine_chart_service.py:46`, 4 tests |
| F1-3 | `registry.py::supported_resolutions` (352) | 2 | hygiene | `rg "supported_resolutions=" app`: 0. Consumers `engine_backtest_service.py:816`, `grid_search/service.py:290`, `engine.py:127`, OpenAPI, Frontend `grid-search-spec-editor.component.ts:177`, `strategy-lab-config.store.ts:122,361,565`; their `'daily'` branch cannot fire. | owner-ruling | cross-stack, serial OpenAPI regen |
| F1-4 | `registry.py:2099` (`spy_strategy_a` description) vs `:2032-2043` | 4, 7 | P2 | Description says "Validated against TradingView via trade-by-trade CSV reconciliation." The same contract's `reference` and `tests/fixtures/golden/spy-strategy-a-signal/v1/attribution.md:12` say none exists; no receipt anywhere in the repo. Rendered on the Strategy Validation page (`strategy_validation_manifest.py:86`). Gating reads flag events, so not P1. | fix (reword; outside the seal) | none |
| F1-5 | `registry.py:1655-1686` (`rsi_mean_reversion` provenance) | 4 | hygiene | `reference` says no LEAN reconciliation exists, and `validated_against`, `parity_fixture_ids`, tolerances are empty. ENG-009 exists: `docs/references/reconciliations/rsi-mean-reversion-lean-2026-09-01.md`, `strategy-parity/ENG-009/v1`, `test_rsi_mean_reversion_lean_golden.py` (2 passed). Hashed into the seal. | owner-ruling (re-seals every rsi bot) | none |
| F1-6 | `signal_program.py:216-230` (`SignalSession.traces`) | 5 | robustness | `settle()` appends every trace; only `engine.py:551` and `research/golden_search/proof.py:367` read it. The live adapter clears `consolidated_bars` and `signal_intents` per bar "because long-lived" (`bot_trade_strategy.py:716-720,749`) and never clears `traces`. Measured 0.59-0.68 KB pickled per trace: about 6.5k per year at 15 min, about 98k for DV at 1 min. File is sealed. | owner-ruling; bundle with the next seal re-qualification | none |
| F1-7 | `registry.py::params_refusal` (373), `_macd_periods_refusal` (446) | 5 | P2 | Enforced only at `paper_deploy_service.py:152`; Engine Lab and Grid Search skip it. `macd_refusal.py`: `schema accepted: 30 26`, `params_refusal → macd_fast (30) must be less than macd_slow (26).`, `initialize raised: ValueError fast_period (30) must be < slow_period (26)`. Backtest turns that into a failed response: fails closed. | fix (handoff) | none |
| F1-8 | `registry.py:655,1507,1665,1860,2036,2231` | 4, 7 | hygiene | Six provenance `reference` strings say "Polygon live data is unavailable, so per PRD direction". Sealed data. | leave; fold into F1-5's ruling | none |
| F1-9 | `registry.py::_warmup_lookback_days_holding` (138-183) | 4 | verified | Not a second calendar: the session span comes from the canonical `session_windows_ms_utc`; the hand counts are weekday arithmetic. Repo test sweeps every minute 2024-07..2027-12; reader's `sweep_lookback.py`: 47 cadences × 15 bar counts, 566 sized points, 0 fails. | leave | none |
| F1-10 | `registry.py:1890-1943` (DV description and gotchas) | 4, 7 | hygiene | Copy says "09:45 ET … 15:45 ET"; the code uses open+15 / close-15 (`deployment_validation.py:44-62`), 12:45 on early closes. Only the text is wrong. | fix with F1-4; the gotchas half dies with F1-C1 | none |
| F1-11 | `test_registry_signal_program_identity.py:335-408` | 3 | robustness | The evidence lint scans `validated_against` and `canonical_implementation` only; `parity_fixture_ids` (7) unscanned (all 7 resolved by hand). | fix | none |
| F1-12 | `registry.py::build` (330) | 7 | hygiene | `lambda p: factory(p).strategy` + `type: ignore` in all 8 entries; derivable. | leave | none |
| F1-13 | `signal_program.py:343-345` | 7 | hygiene | Docstring says the module is in "`artifact_paths` (`app.engine.strategy.registry`)"; the tuples have lived in `program_sources.py` since #2450. Sealed. | leave | none |

### Cut candidates

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| F1-C1 | `algorithm_pseudocode` + `gotchas` read chain (waits on nothing; must merge alone because of the OpenAPI snapshot and generated types) | `rg -n "\.algorithm_pseudocode\|\.gotchas" PythonDataService/app`: `routers/engine.py:128,129` only. `rg -n -i "gotchas\|algorithm_pseudocode" Frontend/src --glob '!**/broker.types.ts'`: `strategy-lab.models.ts:42,43` and two unrelated "gotchas doc" strings in `strategy-preflight.component.html`; no template, store or component reads them. `rg -n -i "gotchas\|pseudocode" Backend Backend.Tests`: a `Backend/CLAUDE.md` section title only. `git show 5f227f8b` deletes `strategy-detail-tab.component.html`, the one template that printed both fields. Neither field is in `SignalProgramContract`; `registry.py` bytes are outside the 34-file set. | `registry.py`: two fields + comments (353-361) and 16 blocks (448 lines at 1118-1204, 1344-1418, 1544-1596, 1705-1752, 1900-1943, 2102-2149, 2297-2349, 2452-2491); `routers/engine.py` 72-81, 128-129; generated `contracts/openapi/python-data-service.openapi.json` (`StrategyInfo` ≈ line 54340) and `Frontend/src/app/api/broker.types.ts:28253,28261`; `strategy-lab.models.ts:42-43`; tests `test_all_registered_strategies_have_algorithm_and_gotchas`, `EXPECTED_STRATEGY_KEYS`, the `gotchas` / `next_bar_open` lines in `test_deployment_validation_is_registered_with_fixed_rule_metadata` | low; wire shape loses two fields, only typed reader is an optional TS field; not money path |

Nothing else to cut: `hidden_params` decided kept; `catalog_visible` and `supported_resolutions` are owner rulings; `rollback_blocked_entry/exit` is a test-only seam (documented at `bot_trade_strategy.py:758-767`); `collect_consolidated_bars` (`engine.py:252`), `signal_intents` (`bot_trade_strategy.py:720`), `lean_twin_program_version` (`lean_sidecar_persistence.py:579`) are live; program-key constants are used inside their own program files. No row in #2703/#2704/#2724/#2725/#2727 names a file in this slice.

### Tests that fail the bar (lens-6 handoff, not part of the lens-2 cut; money-path-adjacent follow "when unsure, keep")

| Test | Kind | Survivor (assertion read) | Disposition |
|---|---|---|---|
| `test_registry_signal_program_identity.py:183 test_registry_signal_series_periods_match_the_constructed_indicators` | 3, 1 | `algorithms/test_ema_crossover_signal_lengths.py:390 …[overrides0]` builds the program and asserts `series[name].period == indicator.period` and `warmup_bars == _samples_until_ready(indicator)`; the doomed test builds `ExponentialMovingAverage("EMA5", 5)` from the same literals | delete |
| `test_signal_program_qualification_matrix.py:180 test_exclusions_and_artifact_paths_are_disjoint` (×8) | 3 | `:156 test_artifact_digest_matches_its_signal_decision_closure` asserts `digested == closure - exclusions`; a path in both breaks equality | delete |
| `…matrix.py:193` second assertion | 3 | `SignalProgramContract.__post_init__` (`registry.py:320-322`) and `:216` | trim assertion |
| `test_signal_program_decision_clock.py:364 test_a_runtime_built_without_a_sink_still_drains_and_logs` | 3 | `:213 test_refused_decision_bar_is_logged_by_the_runner_with_the_clock_it_expected` uses `_runner_runtime()` with no sink and asserts 1 warning, its fields and `take_quarantine() is None` | delete |
| `test_signal_program_session_boundaries.py:205, 235, 341` (24 cases) | 3, 1 | `advance` reads only width and last close (`signal_program.py:197-202`); only `deployment_validation` reads the calendar, and its half-day behaviour is `tests/engine/test_deployment_validation_strategy.py::test_on_closed_bar_emits_exit_at_half_day_barrier`; `last_end == close_ms` is an identity after `% width == 0` | delete; owner call because #1730's AC text lists them. Keep `:269` |
| `test_signal_program_mode_parity.py:277` `paper_traces` leg | 4 | `dry_run_traces` runs the identical `_replay_runner` over an identically built program | trim to two replays |
| `test_spy_strategy_c_signal_program.py:140 test_discarded_exit_does_not_corrupt_in_position_custody` | 3 | `…exit_and_fill_semantics.py:424 test_suppressed_exit_reproposal_matches_declared_exit_eligibility[spy_strategy_c]` | delete; owner may keep (discard safety) |
| `test_signal_program_build_receipt_generation.py:310 test_current_git_provenance_reads_the_paths_last_commit_and_dirtiness` | 4 | re-runs the same two git commands against the live checkout; dirty flag depends on the working tree | delete; keep `:299` |
| `tests/test_engine_strategies_endpoint.py:140 test_legacy_spy_ema_key_is_not_offered_in_the_strategy_picker` | 1, 5 | name says "not offered", assertion is `"ema_crossover_signal" in names`, implied by `names >= EXPECTED_STRATEGY_KEYS` | slice E (listed there as E-C8) |

Partial duplicates kept (they also pin private custody state): EMA and DV `test_discarded_countdown_exit_reemits_on_the_next_eligible_clock`; `test_sma_discarded_exit_stays_reproposable` (only discard test on real SMA math); `test_short_bucket_decision_bar.py::test_the_cell_replays_over_every_bucket` (control for the 1,612 count). In-force lints kept: `test_every_exclusion_carries_a_non_trivial_reason`, `test_dry_run_and_paper_share_the_same_evaluation_stream`, `test_every_registered_strategy_decouples_signal_from_traded_asset`. A PR that edits a sealed byte or a `golden_trace_root` passes the PR tier; only the daily run and runtime admission (`PROGRAM_BUILD_UNPROVEN`, fail closed) catch it — the owner-accepted tiering.

### Drift

- `known-gaps.md` line 82-90 `TradeBar.volume` still open and true; line 141 says "all seven sealed programs" (eight since 2026-10-05); lines 143-146 say "the residual factory-less pair is exactly the next bullet" but that bullet is struck through (resolved 2026-08-28).
- `scripts/run_signal_program_build_qualification.py:75-90` describes `ema_crossover_2_bps` and `spy_ema_crossover` as registered entries with `None` contracts; both deleted 2026-08-28.
- `tests/engine/strategy/test_spy_strategy_c_signal_program.py:2-12` names `test_sma_signal_program.py::test_validated_sma_settings_corpus_has_a_pinned_trace_root` (file gone) and claims a golden-trace test the file does not hold. `test_signal_program_build_receipt_generation.py:38` names a test collapsed into the matrix. `test_registry_signal_program_identity.py:419` "only `spy_orb` was covered" (retired).
- Stale counts: `test_signal_program_decision_clock.py:209`, `test_signal_program_mode_parity.py:124`, `test_signal_program_session_boundaries.py:37` ("six"); `registry.py:1841` ("seven"); `programs/__init__.py:6`, `params.py:6` ("eleven"); `test_signal_program_exit_and_fill_semantics.py:367` ("seventh"). `base.py:104` "future consumers" on `signal_intents`, which has live callers.
- Comments citing plans/PRDs: `registry.py:655,1507,1665,1860,2036,2231` ("per PRD direction", sealed provenance data, F1-8); `test_signal_program_qualification_matrix.py:1` "PRD issue #1730" (issue number, allowed). The one external path, `Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs` (`registry.py:488`), is QuantConnect-side.
- Pointer for the Frontend broker reader: `lean-validation-template.ts:23` still maps the retired `spy_ema_crossover`.

### Not verified

F1-C1 not built (OpenAPI/TS types not regenerated, Frontend tests not run); "no reader" rests on the greps and the deletion commit, and a computed-key lookup would evade them (none found). F1-4: whether an off-repo TradingView reconciliation of Strategy A exists. F1-6: figure is pickled size × trace count, not RSS. F1-9: the holiday-structure claim checked only through 2027. Session-boundary redundancy argued from the code and a caller grep, no mutation run. Not run: `test_parity_companion.py`, most of `tests/broker/v2panel`.
## Slice F2 — strategy algorithms and the declarative strategy spec

28 app files (6,594 lines): `app/engine/strategy/algorithms/*` (11), `spec/*` (5), in-package `spec/tests/*` (12); 6 spec fixtures; 11 test files. **165 passed, 0 skipped** (in-package `spec/tests` collects 40 tests under `pytest.ini` `testpaths`); 106 more passed on neighbouring strategy tests. `-m slow test_ema_crossover_lean_vs_spec.py`: 1 skipped, `POSTGRES_URL unset` (F2-9).

**Every file in `algorithms/` is a sealed artifact path** (`program_sources.py:35,64,93,120,138,162,199,235`); no cut touches them.

**P0/P1: none.** Nothing in the live bot runtime imports `StrategySpec` or `SpecAlgorithm`; importers are research runs, walk-forward, baselines, the spec/research routers and `engine_chart_service` (`build_indicator`).

### Features

| Feature | Requirement | Entry point | Reachable | Normal / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| EMA Crossover Signal | LEAN `SpyEmaCrossoverAlgorithm.cs`; #1730, #2696, #2639 | registry key `ema_crossover_signal`, bot warmup, backtests | yes | unready → `ready=False`; NaN tunables refused | implemented | 75 slice tests; spec parity `test_spec_spy_ema_parity` (6 trades, non-vacuous) |
| EMA Previous Crossover | #2902 | registry key | yes | stale or unseen cross → no entry | implemented | 38 tests (LEAN golden test outside slice, not run) |
| SMA crossover; RSI mean reversion | internal; #1736 | registry keys | yes | first ready bar seeds / unready HOLD | implemented | spec parity 11 / 6 trades |
| Deployment validation | #1672, #1730 | registry key | yes | barrier from `session_open/close_ms_utc` ±15 min (early close handled) | implemented | `test_deployment_validation_strategy.py` |
| SPY A/B/C via `_rsi_range_base.py` | ENG-008, #1700 | registry keys | yes | unready HOLD | implemented | `test_strategies_abc.py`, ENG-008 fixtures |
| `BuyAndHoldStrategy` | Phase 5g.2 cross-run pair | `resolve_strategy_class("BuyAndHoldStrategy")` only | no registry, route, UI, json or ts name | n/a | test-only | sole consumer `tests/lean_sidecar/test_cross_runner.py` |
| `rollback_blocked_entry/exit` (6 strategies) | custody declaration | `tests/_helpers/signal_program.py:363`; live adapter no longer calls them | tests only | n/a | test-only seam, leave | owner 2026-09-30 |
| `SpecAlgorithm` + primitives (`IndicatorComparison`, `FreshCross`, `BarsSinceEntry`, `IndicatorBetween`, `PnLPercent`, `DrawdownFromPeak`, `PredictionComparison`, `DifferenceBps`) | `strategy-spec-layer.md`, #2446, #2445, #2599 | `POST /api/spec-strategy/backtest`, research runs, baselines | yes | zero bars → `ZERO_BARS_EVALUATED`; missing prediction row raises | implemented | 51 tests; goldens |
| `TimeOfDay`, `PnLPoints`, `BarProperty`, nested `LogicNode` | spec conditions | `_build_leaf`; UI catalog offers the first three | yes (UI) | bad HH:MM or tz fails only at run time (F2-2) | **unverified** | `rg TimeOfDay tests app/engine`: no behavioural test; BarProperty test vacuous (F2-6) |
| `FixedContracts`, `OPTION_TEMPLATE`, `pyramiding != 1` | Phase 2 placeholders | `SpecAlgorithm.__init__` | no config | OPTION_TEMPLATE/pyramiding → 400; FixedContracts refused only when an entry fires | partial by design | `probe_router.py` |
| Schema boundary (single-symbol, `extra=forbid`, unknown kind, undeclared refs, prediction-set id path safety) | schema docstring | `StrategySpec` validators | yes | rejected with descriptive errors | implemented | `test_spec_round_trip.py`, `test_spec_predictions.py` |
| Live fields `submit_mode`, `bar_source_descriptor`, `decision_columns` | schema comments claim a live-runtime consumer | none | no reader in `app/` | accepted and ignored | **missing consumer** (F2-5) | `rg`: `schema.py` only |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| F2-1 | `routers/spec_strategy.py:258`, `evaluator.py:120-125` | 5 | P2, reproduced (lead re-ran) | A spec with `predictions` posted to `/backtest` returns HTTP 500 `{"success":false,"error":"spec 'probe' declares predictions (['p']) but no prediction_set was provided to SpecAlgorithm"}`: the router catches only `NotImplementedError`, the route never passes a prediction set, so every such spec is unrunnable there. Fails closed; caller fault shown as server fault. | fix: catch `ValueError` for a 400, or refuse `spec.predictions` early | none |
| F2-2 | `schema.py:169-173` TimeOfDay, `:112-113` MACD, `primitives.py:338-343` | 5 | robustness, reproduced | Schema accepts any `after`/`before`/`tz` string and MACD `fast >= slow`; they fail inside `engine.run` as 200 `success:false` with raw Python text (`invalid literal for int() with base 10: '45am'`, `'No time zone found with key Mars/Phobos'`, `MACD 'm': fast_period (8) must be < slow_period (5)`). | fix: validate in schema | none |
| F2-3 | `evaluator.py:396-409`; comment `app/research/runs/runner.py:452-455` | 3 | robustness, reproduced | `FixedContracts` passes the constructor, refused only when an entry fires; an entry that never fires returns `200 success:true total_trades:0`. The runner comment claiming the constructor raises is false. | fix | none |
| F2-4 | `schema.py:408-411`, `evaluator.py:355-369` | 3 | hygiene | `snapshot_at_entry` ids are validated but narrow nothing; `snapshot_at_exit` read nowhere. | leave: persisted ledgers dump `snapshot_at_exit: []` and `extra="forbid"` would reject them | none |
| F2-5 | `schema.py:124-127, 449-462` | 3, 7 | hygiene, reproduced | Comments claim "consumed only by the live runtime" and "deploy/start reject ibkr_paper_delayed"; no reader exists. A spec with `ibkr_paper_delayed` + `submit_mode: shadow` runs to completion (200, 40 trades). `alpaca-live-authority.md:108` notes it (E8); no `known-gaps.md` entry. | leave (sealed fixtures carry the fields); fix comments | none |
| F2-6 | `spec/tests/test_spec_extra_primitives.py:212` | 6 | hygiene | Passes with the `BarProperty` condition removed: both runs give 0 trades. | delete | see tests |
| F2-7 | `deployment_validation.spec.json` (sealed) | 4, 7 | hygiene | Named "Deployment Validation" but encodes `TimeOfDay 09:45–15:45` + `BarsSinceEntry>=3`, not two-green-bars; the literal 15:45 is the #1672 failure class. Runnable via `/backtest`; no live path executes it. | leave (sealed) | none |
| F2-8 | `docs/references/deployment-validation-consecutive-green.md:79` | 7 | hygiene | Claims the deploy schema "still records `strategy_spec_path`"; found only in that line, CONTEXT.md:901, ADR 0020. | fix doc | none |
| F2-9 | `tests/integration/parity/test_ema_crossover_lean_vs_spec.py` (Sacred, no change proposed) | 3 | robustness | Skip chain: POSTGRES_URL unset (observed) → launcher unreachable → no `/lean-data` → no pinned digest. No CI or daily workflow starts a LEAN launcher, so it cannot fire in automation. Compensating non-skipping evidence: `test_spec_spy_ema_parity`, `test_spec_spy_ema_2_bps_parity`, cross-engine cells, ENG-007/009. | owner-ruling: state which test proves "live EMA equals LEAN" | none |
| F2-10 | `engine.py:562` contract vs `evaluator.py` | 3 | latent | `SpecAlgorithm` has no `on_force_flat`; no caller passes `evaluation_start_ms` with a spec. | leave | none |
| F2-11 | `tests/fixtures/golden/spy-ema-difference-bps/` | 4 | hygiene | Attribution + generator + test, but no `manifest.json` row; `test_golden_manifest.py` checks entries→files, not dirs→entries. | owner-ruling | none |
| F2-12 | `schema.py:330-340` OptionTemplatePosition, `:313-316` FixedContracts | 2 | hygiene | Phase 2 placeholders the evaluator always refuses; removal turns 400 into 422 and edits the OpenAPI contract, so not delete-only. | owner-ruling | OpenAPI snapshot, `broker.types.ts`, one test |
| F2-13 | `self.signal_program = None` in 6 algorithm files | 7 | hygiene | Repeats `Strategy.__init__` (base.py:194); **all six files sealed**. | leave | n/a |
| F2-14 | `ema_crossover_signal.py:97-133` vs `ema_previous_crossover.py:95-113, 211-239` | 7 | hygiene | `_finite`, `_decision_bar_count`, `_gap_checks`, `DEFAULT_*` copied between the two. | leave (sealed, EMA Sacred) | n/a |
| F2-15 | `deployment_validation.py:378,380,394,445,471` | 7 | hygiene | `strftime` where siblings use `display_time`. Log only. | leave (sealed) | n/a |
| F2-16 | `primitives.py:323,325` | 3 | hygiene | `range_pct`/`body_pct` give 0 when `close<=0`; ingestion validates, so unreachable. | leave | n/a |
| F2-17 | `schema.py:498` | 7 | hygiene | Engine-layer schema lazily imports `app.research.ml.artifact`. Layer inversion, working. | leave | n/a |
| F2-18 | `docs/references/baselines.md:11` | 7 | hygiene | Claims the closing fill is never drained; not reproduced (`probe_eod.py`: hold-to-end spec logs 1 closed trade; lake path not run). | owner-ruling: re-verify | none |

### Cut candidates

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| F2-C1 | `app/engine/strategy/spec/fixtures/spy_ema_normalized_gap.spec.json` (41 lines) | `rg -nw spy_ema_normalized_gap`: the fixture + `test_spec_router.py:71` (expected-name set) only; not in `strategy_validation_manifest.json`, not in the Frontend's 3 bundled fixtures, no test loads it; reachable only via `GET /fixtures`. `DifferenceBps` stays covered by `ema_crossover_2_bps.spec.json`. | the literal at `test_spec_router.py:71` | low |
| F2-C2 | `routers/spec_strategy.py::list_fixtures` (172-192) + `FixtureListItem` (96-101) + `test_fixtures_list_endpoint` (63-83) | Frontend fetches only `fixtures/spy_ema_crossover`; the list URL appears in generated `broker.types.ts` and the test only. Same route as E-17. | OpenAPI snapshot, `broker.types.ts` regen | low-medium (contract regen, serial merge) |

Not cuts: `BuyAndHoldStrategy` (test-only seam; rewriting `test_cross_runner.py` is not delete-only); `rollback_blocked_*`; `output_dir` emitters (parity matrix); every `algorithms/` file (sealed); `submit_mode`, `bar_source_descriptor`, `decision_columns`, `snapshot_at_exit` (sealed fixtures, persisted ledgers); exhaustiveness guards in `evaluator.py` and `_build_leaf`.

### Tests that fail the bar (assertions read; probes in `F2/`)

| Test | Kind | Survivor | Action |
|---|---|---|---|
| `spec/tests/test_spec_manage_rules.py::test_survival_rule_order_first_match_wins` | cannot fail for the named property | probe P1: still passes reversed; both rules `CLOSE_ALL`. Survivor `test_profit_target_fires_on_overshoot` | delete |
| `test_spec_manage_rules.py::test_survival_rule_does_not_fire_while_flat` | trivial | probe P2: passes with `PnLPercentPrimitive.evaluate` forced True (survival not evaluated while flat, `evaluator.py:312`) | delete |
| `test_spec_extra_primitives.py::test_bar_property_filter_blocks_low_range_bars` | cannot fail | 0 trades with and without the filter | delete |
| `test_spec_extra_primitives.py::test_prediction_comparison_routed_by_build_leaf` | duplicate, private name | `tests/engine/strategy/spec/test_evaluator_predictions.py::…exact_row_under_exact_bar_close_lookup` builds `SpecAlgorithm` with a `PredictionComparison` entry and asserts `_in_position is True` | delete |
| `tests/engine/strategy/spec/test_difference_bps_operand.py::test_difference_bps_rejects_zero_denominator` | duplicate | `test_difference_bps_matches_decimal_golden_fixture` fixture row `{"left":"1.00","right":"0.00","error":"division_by_zero"}` | delete |
| `tests/engine/strategy/spec/test_schema.py::test_strategy_spec_bar_source_defaults_to_live_runtime_source` | trivial | asserts a default equals its own constant, for a field nothing reads | delete + unused import |
| `spec/tests/test_spec_router.py::test_fixtures_list_endpoint` | copy pin | n/a | delete with F2-C2, or drop one literal with F2-C1 |

Borderline, keep: `test_backtest_rejects_an_unknown_fill_mode_naming_the_modes` (proves 400 not 500); `test_rejects_client_id_as_strategy_field` (pins the ADR decision); `test_the_14_30_bar_was_a_fresh_cross_that_failed_both_filters` (#2639 owner case); `test_trailing_stop_resets_between_trades` (weak, zero margin, non-vacuous). Hygiene: `test_spec_round_trip.py:63` `except Exception` where `ValidationError` is meant; `test_spec_predictions_runtime.py` docstring promises more than its one test asserts.

### Drift

- `known-gaps.md`: 0 stale entries for the slice; F2-5 is missing (only in `alpaca-live-authority.md:108`).
- Stale docstrings (unsealed): `spec/schema.py:8-18` ("survival == []" claimed enforced), `:449-463`, `:547` ("BarField" not in the union), `spec/__init__.py:3-17`, `primitives.py:25,374` and `spec/__init__.py` name `SpyEmaCrossoverAlgorithm` (C# reference only), `test_spec_predictions_runtime.py:1-4`. Sealed (do not touch): `algorithms/sma_crossover.py:359`.
- Docs: `strategy-spec-layer.md:10-16` lists 3 of 6 shipped fixtures; `baselines.md:11`; `deployment-validation-consecutive-green.md:79`.
- Pointers: `engine.py:205`, `run_gate.py:18`, `cross_runner.py:1-6` name a `POST /api/lean-sidecar/.../cross-reconcile` route that does not exist; `Frontend/.../canonical-fixtures.ts:7-12` claims CI catches TS/JSON drift (no test compares them); `spec-strategy-runner.component.spec.ts:360` says "the same three the Python service ships" (Python ships 6).

### Not verified

LEAN-side proof of the EMA/RSI/SMA twins (`tests/integration/reconciliation/*lean_golden.py`, cross-engine cells) not run. `baselines.md` limitation reproduced with synthetic bars only. Callers outside this repo of `GET /api/spec-strategy/fixtures` not visible. `golden/manifest.json` completeness beyond F2-11 is a substring scan. Persisted-ledger impact of removing `snapshot_at_exit` inferred, no stored ledger opened. `test_signal_program_qualification_matrix.py` not run against a trial edit.
## Slice G — engine data, execution, framework, indicators, consolidators and the edge package

45 app files (5,607 lines) under `app/engine/{data,execution,framework,indicators,consolidators,edge}/**`; 31 test files + 10 fixtures. **264 passed, 1 skipped** (`app/engine/tests/test_spy_validation.py:182`, no SPY minute data, G-9); 164 more passed on `tests/edge`, indicator/volatility/golden-manifest fixtures, `test_indicator_parity.py`, `test_insight_framework.py`, `test_iv30_router.py`, `test_portfolio.py`, `test_engine_fill_modes.py`. ruff: 1 hit (`trade_bar_consolidator.py:57` DTZ001, an intentional naive epoch for ET wall-clock arithmetic; `ruff.toml` does not select DTZ).

**Sealed set (never in a cut)**: `consolidators/trade_bar_consolidator.py`, `data/trade_bar.py`, `indicators/{adx,base,ema,macd,rsi,sma,supertrend}.py`.

**P0/P1: none.** Closest, both reproduced and rated P2: G-1 (the edge route forward-fills the unrealized forward-RV tail, so the page's SHORT VOL / LONG VOL readout rests on stale data) and G-2 (the "past-only" HF RV feature is not causal on an intraday index). Both feed a research page with no order path.

### Features

| # | Feature | Requirement | Entry point | Reachable | Class | Evidence |
|---|---|---|---|---|---|---|
| 1 | Minute reader: lake-admitted, RTH/extended filter, half days | ADR 0049, #3037/#3038/#3044, #2451 | `LeanMinuteDataReader` | yes | implemented | `test_lean_format_session_filter.py`, `test_lake_admission.py` |
| 2 | Daily reader: multi-root merge, warm-history re-verify | #3037 | `LeanDailyDataReader` | yes | implemented | `test_lake_admission.py` (7) |
| 3 | Availability preflight, `MissingSessionsError`, unreadable ≠ missing | PRD #1926, #2445, #2475, #2489 | `check_availability` | yes | implemented | `test_availability_calendar.py` (18) |
| 4 | Decoded-artifact cache (bounded LRU, residency, metrics) | #3038, #3048 | `DecodedArtifactCache` ← `golden_search/worker.py:204`, `research/benchmark.py:797` | research workers only | implemented; `pin/unpin` test-only | `test_decoded_artifact_cache.py` |
| 5 | Cash dividends from LEAN factor files | #2903, #3000 | `dividends_from_factor_*` | yes | implemented | `test_engine_dividends.py` + golden |
| 6 | Compat-fixture receipt, data-root resolution | #3000, ADR 0049 | `policy_store.*` | yes | implemented | `test_policy_store.py` (7) |
| 7 | LEAN-format writers for sidecar staging | ADR 0070 | `write_lean_*_zip` ← `staging.py` | only `synthetic` and recorded-`fixture` runs | unverified (no checked-in config selects them) | `test_bar_store_path_injection.py` |
| 8 | Fill models (4 modes), commission seam, slippage | #2599, #2601, #2607, #2909 | `FillModel`, `ExecutionConfig` | yes | implemented | `test_fill_model.py` (13), `test_fill_model_fee.py` |
| 9 | `SetHoldings` sizing (LEAN buffered, fee-aware) | LEAN parity | `LeanSetHoldingsSizing` ← `cross_runner` | yes | implemented | `test_sizing.py` (20 pinned, atol=0) |
| 10 | `Portfolio`; fill-mode name parser; TradeBar consolidation incl. short bucket | #2449, #2599, #2909 | engine | yes | implemented | golden `lean-consolidator-short-bucket`, `test_portfolio.py` |
| 11 | EMA / SMA / RSI | LEAN | registry | yes | implemented | golden IND-001..003, `test_indicator_parity.py` |
| 12 | ADX / MACD / Supertrend | Wilder / Appel / Pine | SPY A/B/C | yes | implemented, no reference-derived golden (G-7) | hand micro-tests; pandas-ta 1e-9 for MACD/Supertrend, ADX loose (<1.0 pt) |
| 13 | Insight framework (emit, score, summary) | LEAN Alpha port | `ctx.emit_insight` | yes | **partial**: computed and persisted, nothing reads it (G-11) | `test_insight_framework.py` |
| 14 | Edge: realized-vs-IV series | ADR 0071 | `POST /api/edge/realized-vs-iv/series` ← Frontend `/edge/realized-vs-iv` | yes (`main.py:1409`) | **partial**: G-1, G-2; page defaults to mock data until Compute | `tests/edge/*` |
| 15 | Edge: IV30 routes (`iv30_atm_50d`) | ADR 0071 | `POST /api/edge/iv30/{vix-style,parametric}` | yes | implemented; G-4 | `test_iv30_router.py` |
| 16 | Edge trade simulator / spread model | none in force (consumer `trade_simulator.py` cut by #2755) | none | no | **missing consumer** (orphan `spread_model.py`) | C-3 |

### Findings

| ID | path::symbol | Lens | Sev | Evidence | Disposition | Fix orphans |
|---|---|---|---|---|---|---|
| G-1 | `app/routers/edge.py:162-166` `vrp_signal(iv=iv_trd252.ffill(), rv=rv_hf_fwd.ffill())` | 3 | P2, reproduced | The unrealized forward-RV tail (last W days) is forward-filled before `vrp_z` is built, while `vrp_forward` (:161) stays NaN; the comment at :143 says "No silent forward-fill". Frontend turns `vrp_z` into the SHORT VOL / LONG VOL / NO TRADE readout (`realized-vs-iv.component.ts:74-80`). Signal is also built on forward (oracle) RV, which `labels_oracle/__init__.py` says never feeds signals. `repro_edge_ffill.py`: 21 tail bars, `vrp_forward None = 21`, `vrp_z None = 19`, last `vrp_z -0.167`. | fix: do not ffill `rv`; mask `vrp_z` where `rv_hf_fwd` is NaN | none |
| G-2 | `edge/features_realtime/hf_realized_vol.py:124-150` | 4 | P2 / robustness, reproduced | "Realtime guarantee" false on an intraday index: the day's whole RV² (current day included) is assigned to every bar of the date. `repro_hf_causality.py`: value at 09:15 ET `0.1236` with later bars, `0.1105` with them dropped. Golden RV-002 pins this day-level ffill. Display consumers only. | fix (docs: "valid at session close"), or shift one session if an intraday decision consumes it | none |
| G-3 | `hf_realized_vol.py:1-30` | 7 | hygiene | Docstring says `r_overnight = ln(open_first_d / close_last_{d-1})`; code (112-117) and the golden oracle use first **close**; `open` is required (:96) but unused. | fix docstring; drop `open` from required columns | none |
| G-4 | `iv30_constructor.py:44-70` `iv30_atm_50d`; `routers/iv30.py:303-309` | 3 | P2 | "Returns None if no straddling expiries exist" is false: with all expiries on one side it returns the nearest expiry's IV flat (pinned by `test_iv30_atm_50d_extrapolates_when_no_straddle`); the route's `sigma is None` branch says "should not happen", so a non-30-day number can be returned as `iv30_act365`. | fix: return `None` or a flag when not bracketed | none |
| G-5 | `edge/spread_model.py:1-14` | 4, 7 | hygiene | Header formula (Madhavan-Smidt) does not match the code (`max(floor, k·iv·sqrt(T)·(1+α|δ−0.5|)·S)`); constants have no source; provenance `NONE — pending`. | see G-C3 | tests |
| G-6 | provenance blocks `realized_vol.py:6`, `hf_realized_vol.py:6` | 4, 7 | hygiene | Both say `Validated against: NONE`, but manifest has RV-001 and RV-002 active and hashed, validated by `test_volatility_fixtures.py` (passes). `delta_inversion.py` has **no** provenance block. | fix (docs) | none |
| G-7 | `app/engine/tests/fixtures/golden/{adx_14,macd_12_26_9,supertrend_10_3}/`; `tests/fixtures/golden/lean-set-holdings/` | 4 | robustness | None is in `golden/manifest.json`, so `test_golden_manifest.py` never hash-checks them. The three in-package outputs are the port's own output (`regenerate.py` runs the port), not reference output. Independent oracles exist in the tests (pandas-ta). Known-gaps §5 "golden-fixture coverage gap" covers the class. | owner-ruling (register, or relabel as regression snapshots) | none |
| G-8 | `execution/sizing.py:26`; `lean-set-holdings/attribution.md:38` | 7 | hygiene | Cite `tests/engine/execution/test_sizing.py`; the file is `tests/engine/test_sizing.py`; `tests/engine/execution/` holds only an empty `__init__.py`. | fix path; G-C6 | none |
| G-9 | `app/engine/tests/test_spy_validation.py:161-191` | 3, 6 | robustness | `@pytest.mark.slow` and self-skips unless `LEAN_DATA_ROOT` or `<repo>/../Lean/Data/equity/usa/minute/spy` holds zips. CI and daily set no `LEAN_DATA_ROOT`; on this Mac `/Users/inkant/Lean/Data` is an empty directory. The 63-trade LEAN reference `spy_lean_trades.csv` is a Sacred golden fixture whose parity test has nowhere to run. Same caveat for the EMA parity test (F2-9). | owner-ruling: stage SPY data for the daily run, or accept as local-only | none |
| G-10 | `data/polygon_bars.py:63` `volume=int(raw["volume"] or 0)` | 4, 5 | robustness, reproduced | Truncates fractional-share volume (`1234.567891` → `1234`) and turns a vendor null into 0. Reached only via `lean_sidecar/polygon_canonical.py:295` (fixture replay). The lake path keeps exact `Decimal`. | fix: use `parse_volume` as `lean_format` does | none |
| G-11 | `framework/insight_manager.py:143-225` `get_summary` → `engine.py:482,497` → `engine_backtest_service.py:1372` → `backtest_runs/engine_payload.py:189` | 1, 2 | robustness | Per-run insight analytics (calibration, accuracy by hour, magnitude bias) are computed and persisted as `insight_summary_json`; no Frontend or Backend code reads them. The docstring "powers the Insight Analysis panel in the Engine Lab UI" is false. Removal is a contract change (OpenAPI field, DB column). | owner-ruling | n/a here |
| G-12 | `edge/__init__.py:4`; `labels_oracle/forward_rv.py:9`; `hf_forward_rv.py:9` | 3 | hygiene | Three docstrings claim a "CI grep guard/test" that `features_realtime` never imports `labels_oracle` or uses `.shift(-N)`; no such test or workflow step exists. Holds by inspection only. | add a short test, or delete the claim | none |
| G-13 | `data/policy_store.py:60-85`, `:55-57` | 7 | hygiene | Docstrings describe `[reference?, policy cache]`, "when the flag is on"; the function returns `[lake_root]`, the flag is gone (#1893). | fix docstring | none |
| G-14 | `framework/__init__.py:1-11` | 7 | hygiene | Claims "Phase 2: AlphaModel interface + built-in alpha models"; only `insight*.py` exist (`framework/alpha/` cut by #2752). | fix docstring | none |
| G-15 | `consolidators/trade_bar_consolidator.py:61-83` `_floor_to_period_ms` (SEALED) | 4 | robustness, reproduced | In the repeated 01:00-02:00 ET hour on fall-back day the floor lands on the first occurrence (an hour early); the docstring claim "numerically identical to flooring raw UTC ms" is false there. No NYSE bar exists in that hour. | leave (sealed; unreachable for equity sessions) | none |
| G-16 | `data/lean_format.py:302-313` `iter_committed_dates`; `availability._parse_minute_zip` (:186), `_read_daily_dates` | 5 | robustness | Calls module `read_committed_bytes` instead of `self.admission`, the seam #3044 added for spawned workers. No worker calls it today. | fix if a worker ever needs availability | none |
| G-17 | `hf_realized_vol.py:71` `(hour >= 4) & (hour < 20)` | 4 | robustness | Hard-coded ETH 04:00-20:00 ET in session logic; wrong on early-close days. Instance of open known-gap "clock constants". | pointer | none |
| G-18 | `indicators/base.py:59-79,137-153` `update` (SEALED) | 3 | robustness | Stale or duplicate timestamp is dropped and returns `False`; engine callers ignore it; no log or counter. Documented LEAN dedupe. | leave (sealed) | none |
| G-19 | `data/lean_format.py:136-194,396-444`; writers `:623-793` | 5 | robustness | Readers skip wrong-width rows and do not check monotonic/unique `ms`; writers do not enforce "bars belong to `trading_date`". Documented skip (#2451); the lake publication gate is the real boundary-1 check. | leave (pointer #2451) | none |
| G-20 | `execution/commission.py:1-9` | 7 | hygiene | Says the seam keeps `app.engine.*` free of `app.research.*`; the file imports `app.research.parity.ibkr_commission`. | fix docstring | none |
| G-21 | `edge/confidence.py:36-46,64-76` | 1 | hygiene | Docstring "the UI banner reads `reason`": the route reads only `.confidence`; `reason`, `health_score`, `variance_contribution_synthetic` read by tests only. | test-only, leave; fix docstring | none |
| G-22 | `execution/order.py:19-22,66-67,80-81` `OrderType.{LIMIT,STOP_MARKET}`, `Order.limit_price/stop_price` | 2 | hygiene | Only `Order(...)` constructor in app is `Portfolio.submit_market_order` (MARKET); `fill_model.py:140` and `engine.py:396-398,633` raise or skip on anything else. `STOP_MARKET`: 0 references anywhere. #2752's message says "OrderType.LIMIT … stay". | pointer #2752; leave | none |

### Cut candidates

Rule applied (AGENTS.md "Keeping and cutting"): (a) pinned by a number-asserting test → keep even if uncalled; (b) not validated and uncalled → candidate; (c) wired to a route, job or registry → live. **Correction by the lead:** reader G saw only the #2704 ticket body; the owner's ◇ addenda on that ticket (2026-09-30) rule that the `iv30_constructor` functions, `delta_inversion.py`, `option_spread` and its constants, and their tests stay even when nothing calls them. G-C1 to G-C4 are therefore kept, not candidates; only G-C5 (a money-trail type, owner-ruling) and G-C6 remained, and G-C6 is in the cut.

| ID | Target | Evidence | Goes with it | Risk |
|---|---|---|---|---|
| G-C1 | `edge/features_realtime/iv30_constructor.py::iv30_atm_50d_trading_basis` (73-89) — **KEPT: owner ruling ◇ (#2704 addendum, 2026-09-30: "the `iv30_constructor` functions … stay")** | `rg -w` repo-wide: 2 hits in `docs/references/iv-rv-basis-alignment.md:65,77` + the `vrp.py:52` docstring; 0 tests, scripts, routers. #2704 row E. Wraps `convert_iv_act365_to_trading252`, which stays (RV-003). Provenance `NONE`. | edit the `vrp.py:52` docstring and two doc rows | low |
| G-C2 | `iv30_constructor.py::skew_25d, term_slope, iv_change, iv_vol` (92-109) + docstring items 2-4 — **KEPT: same ◇ ruling (the `delta_inversion`/`iv_change`/`iv_vol` tests are named in it)** | `rg -w`: only `tests/edge/test_iv30_and_vrp.py`; 0 app/scripts/routers. #2704 row E names `iv_change`, `iv_vol`. | 4 tests + 4 import names | low-medium; `iv_change` has pinned values (atol 1e-12) — **owner-ruling** for that one |
| G-C3 | `edge/spread_model.py` (whole file, 85 lines) — **KEPT: ◇ addendum 2 ("`option_spread`, `OPTION_SPREAD_FLOOR`, `DEFAULT_K`, `DEFAULT_ALPHA` and `tests/edge/test_spread_model.py` stay")** | `rg -w` for the 3 functions + 4 constants outside `app/engine/edge/` and `tests/edge/`: only Frontend display strings and `scripts/pr_shard_durations.json` ids. Consumer `trade_simulator.py` deleted by #2755. #2704 row E, left by #2752/#2755 with no stated reason. Its tests recompute the expected value from the same constants. | `tests/edge/test_spread_model.py` (126 lines, 13 tests); `pr_shard_durations.json` rows | **owner-ruling**: options math with number-asserting tests; judged (b) because the "numbers" are the formula restated |
| G-C4 | `delta_inversion.py::strike_and_iv_for_delta`, `VolSurfaceFn`, `brentq` import (23, 42-84) — **KEPT: ◇ ruling ("`delta_inversion.py` … stays")** | #2704 row A. Only `test_strike_and_iv_for_delta_with_flat_surface` calls it, asserting `395<K<405` and `sigma==0.20`, not a number. `strike_for_delta_constant_vol` stays (a). | that test + 1 import | **owner-ruling** |
| G-C5 | `execution/order.py::OrderEvent` fields `recorded_fee, execution_source, fill_model, source_bar_close_ms, exec_id, client_order_id, exec_time_ms`, ctor params, assignments (112-177) | The only app constructor, `fill_model.py:189`, passes none of the seven; an AST scan of `OrderEvent(...)` in app/tests/scripts finds 0 passing them; repo-wide `recorded_fee|source_bar_close_ms|execution_source`: `order.py` plus ADR/CONTEXT prose naming other classes. Vestige of the IBKR receipt writer (#1679/#1813). `order.py` not sealed. | nothing | **money-trail adjacency** (a fill type): owner-ruling; thermo if cut |
| G-C6 | `tests/engine/execution/__init__.py` (empty package, no tests) | `ls` shows `__init__.py` only; no import of `tests.engine.execution` | the file | none — **in the cut** |

Not cuts: `DecodedArtifactCache.pin/unpin`, `_Entry.pins`, `pinned_bytes` (test-only seam; production never pins, so the docstring is untrue of it); `Insight.group`, `InsightType.VOLATILITY`, `Insight.weight/group_id`; `policy_store.BarSource`; `OrderType.STOP_MARKET` (G-22); and every other census row, caller-searched and live (`write_lean_*_zip` via `staging.py`, `iter_committed_dates` ×3, `availability.*`, `dividends_from_factor_*`, `policy_key` ← `engine_chart_service.py`, `polygon_bar_to_trade_bar`/`group_by_trading_date` ← `polygon_canonical.py:295`, `parse_fill_mode`/`REQUEST_FILL_MODES` ← `schemas/grid_search.py:61`, `grid_search/models.py`, `backtest_runs/records.py:42`).

Edge classification: `confidence.*`, `vrp.*`, `threshold_events.*`, `realized_vol.*`, `hf_realized_vol.*`, `forward_rv.*`, `hf_forward_rv.*`, `iv30_atm_50d`, `variance_interpolated_iv` are (c) or (a); `delta_inversion.strike_for_delta_constant_vol` is (a) (`test_strike_for_delta_50d_atm_at_zero_rates`, independent closed form `K=S·exp(σ²T/2)`, rtol 1e-9).

### Tests that fail the bar

| Test | Kind | Survivor | Action |
|---|---|---|---|
| `tests/edge/test_realized_vol.py::test_yang_zhang_k_constant_matches_paper` (outside the slice list) | 1 trivial / 4 | computes `expected_k` itself and asserts `0 < k < 1`; only other YZ test is `…zero_when_ohlc_constant` | delete, or pin YZ to a hand-computed number |
| `app/engine/tests/test_execution_config.py::test_short_market_fill_applies_slippage_down` | 3 | `test_fill_model.py::test_short_slippage_subtracts_from_fill_price` asserts `Decimal("99.98")`; config pass-through proven by the kept `test_long_market_fill_applies_slippage_up_and_commission` | delete |
| `test_execution_config.py::test_slippage_and_commission_change_pnl_versus_zero_costs` | 3 | same arithmetic as `test_fill_model.py::test_{long,short}_slippage_*` + `test_commission_propagates_to_order_event` | delete |
| `app/engine/tests/test_spy_validation.py::test_spy_ema_crossover_matches_lean_reference_trades` | parity test that has never run vs Sacred fixture | skips on every reachable machine (G-9) | **owner-ruling**, not a cut |
| `app/engine/tests/test_{adx,macd,supertrend}.py::test_golden_fixture_regression` | guard hole | `pytest.skip("… not yet generated")` fires if the CSVs vanish instead of failing; ADX message names `regenerate_adx_fixture.py`, which does not exist | change skip to fail |

Kept: `test_commission_reexport.py` (identity check is the named parity test in `commission.py`'s provenance), `test_sizing.py::test_fixture_present_and_nonempty` (guards an empty parity loop), `test_polygon_bars.py`.

### Drift

- known-gaps open and accurate: `TradeBar.volume: int` vs lake `Decimal` (§5); golden-fixture coverage gap (§5); clock constants (§5, G-17). Missing entries worth one line: G-1/G-2, G-7.
- `docs/references/iv-rv-basis-alignment.md:65,77` names `iv30_atm_50d_trading_basis` (G-C1).
- Comments citing a plan or phase label (rule: ADR or issue/PR number only), 11 lines: `edge/vrp.py:15,76` ("Step E of the IV-ownership plan"), `hf_realized_vol.py:1,25` ("Step 3 of IV-RV alignment"), `data/lean_format.py:73,706,713` ("Phase 5f/5c/5d"), `framework/__init__.py:9-10`, `execution/fill_model.py:55-56` ("(Step 3)", "(Step 5)"), `app/engine/tests/test_execution_config.py:3` ("PR 1 of the execution-realism roadmap").
- `data/lean_format.py:25-27` says the import "self-liquidates when #1840 retires the policy-store writers"; #1840 is CLOSED (2026-08-31), the writers remain for LEAN staging.
- `ZoneInfo("America/New_York")` constructed in 43 app files beside `app/utils/timestamps.py:21`.

### Pointers (decided already)

`TradeBar.volume: int` vs lake `Decimal` (sealed, open gap). #2704 rows already cut by #2752/#2755 (`InsightScore.get_score`, `submit_limit_order`, `limit_penetration`, `edge/{calibration,regime_drift,regime_strategy_eval,trade_simulator,regime_features}`, `TradeBar.period_seconds`). #2704 keep-note for `AverageDirectionalIndex.plus_di/minus_di` and `Supertrend.upper_band/lower_band` confirmed (`test_adx.py:104-105`). `rollback_blocked_entry/exit`: read reflectively by `tests/_helpers/signal_program.py:363`. `TradeBar(time=, end_time=)`, `Order(time=)`, `OrderEvent(time=)` aliases: no production caller, `trade_bar.py` sealed, test-only seam. Frontend `components/edge/{cross-asset,regimes,drawers}` run on `EdgeMockDataService` since #2755 cut their routes (tagged `synthetic`).

### Not verified

SPY parity against LEAN could not run (G-9). Owner reading of "a test proves its numbers" for `iv_change`, `strike_for_delta_constant_vol`, the `spread_model` tests. Seal impact of the cut PR (`test_signal_program_qualification_matrix.py` not run). G-1 reproduced against the route function, not over HTTP. Row 7 reachable in code, not from any checked-in UI or config. G-1's Frontend side read, not executed.
## For handoff slicing (#3159)

**Fix slices** — each PR-sized, none touches the sealed set unless noted.

1. **Runner shutdown and Stop** — A F-A-1 (`stop_all` snapshot, the only reproduced P2 in the runner; one line), F-A-3 (unawaited stragglers), F-A-4 (a task outliving the 5 s cancel never gets its custody proof re-driven), F-A-21 (discard-before-write on an end candidate), F-A-2 (Dry Run end asked once; not reproduced — needs a one-shot failing STOP write fixture), F-A-5 (admission snapshot dropped when the Clerk lacks the flag).
2. **Prove the three unproven money-path gates** — C-1, C-2, C-3 (one runner-level test each; the mutation commands are in slice C), F-A-18 (`start_end_watch` never run by a test), B-15 (durable-append-log failure-injection test claimed, one happy-path test present).
3. **Boot recovery and terminal projection** — B-1 (corrupt `desired_state.json` aborts the lane's startup; owner ruling on per-bot refusal vs whole-lane abort first), B-2 (log a non-`RECORDED` projection), B-10 (typed exception instead of `str.startswith`).
4. **Dry Run preview leak and ledger capacity** — D-2/D-3 (pointer to #3151 F2 / known-gaps F10; release the `sim:<sid>` authority on a refused or previewed Start, and do not create `source_bars.sqlite3` on a read), D-12 (owner ruling: RTH filter before retention, or a rollover tool), D-4 (typed refusal for invalid seal params), D-1 (copy of the NOT_APPLICABLE build gate).
5. **Backtest request boundary** — E-4 (unbounded `to_date` holds the engine gate for 21 s then `OverflowError`; reuse `/chart`'s `require_schedulable_end`), E-5 (`Infinity` and `initial_cash=0`), E-6 (calendar-date validation in the schema; removes the four DTZ007 sites), E-7 (factor-file drift escapes as a raw 500; 409 in replay), E-3 (unlogged `except` and a silent fixture drop), E-10, E-8; the spec route's F2-1 (predictions → 500), F2-2 (TimeOfDay/MACD validated in the schema), F2-3 (`FixedContracts` constructor refusal), F1-7 (`params_refusal` on Engine Lab and Grid Search).
6. **Statistics** — E-1 (units: points vs dollars, hard-coded fees, `total_orders = n_trades`, arithmetic annualization, UTC buckets; owner ruling on the "LEAN native" label first; sequence E-C4 after it), E-2 (`cagr`/`calmar` only inside `if max_dd > 0`), E-9 (`validate_statistics` cannot fire), E-12 (test patches a function the code never reaches), E-13.
7. **Edge page** — G-1 (do not forward-fill `rv`; mask `vrp_z`), G-2 ("valid at session close", or shift one session), G-4 (`iv30_atm_50d` returns a flat number when not bracketed), G-3 and G-6 (docs), G-12 (add the missing "never imports `labels_oracle`" test or delete the claim), G-10 (`int(raw["volume"] or 0)` truncates fractional volume on fixture replay).
8. **Sealed-set work waiting on a re-qualification** — bundle: `TradeBar.volume: int` (known-gaps §5), F1-6 / C-8 (`SignalSession.traces` never trimmed: about 90 MB per 250 sessions at DV's 1-minute clock), F1-5 + F1-8 (provenance strings that understate ENG-009 and say "per PRD direction"), F2-13/F2-14/F2-15 (sealed duplicates).

**Cross-stack wire-contract cut slice** (needs Frontend type edits and a serial OpenAPI regeneration): F1-C1 (`algorithm_pseudocode` + `gotchas`, 448 lines in `registry.py`, `routers/engine.py:72-81,128-129`, `strategy-lab.models.ts:42-43`, generated types), E-17 / F2-C2 (`run_engine_backtest`, `list_fixtures`: no client), F2-C1 (`spy_ema_normalized_gap.spec.json`), G-11 (insight analytics computed and persisted, read by nothing), F1-2 / F1-3 (`catalog_visible`, `supported_resolutions` daily branch), F2-12 (Phase 2 placeholders in the spec schema, 400 → 422).

**Hygiene slice** (edit-cuts and docs): B-C4/B-C5/B-C6, B-6/B-7/B-8, D-C4 (about 15 sites for one dict read), D-19 (`launch_reason="resume"` in 8 tests), A F-A-9/F-A-14/F-A-24, E-C4/E-C5 after E-1, C-6/C-7/C-15/C-16/C-17, G-8/G-13/G-14/G-20, the docs and comments listed under each slice's Drift (the `tests/services/test_bot_runner.py` citations in 11 test docstrings and `known-gaps.md:133`; `engine-authority-map.md:71` and :86; ADR 0053:85 and 0063/0038 line pointers; `iv-rv-basis-alignment.md`; known-gaps entries at :131, :141-146, :551-577, :596-602, :127-136, :247-252, :76-80).

**Test-bar slice** (lens 6; money-path-adjacent rows follow "when unsure, keep"): C T-1..T-6, D's two deletes, E's three, F1's nine (about 36 collected cases), F2's seven, G's five; plus the two guard holes where `pytest.skip("not yet generated")` should fail (G).

**Owner rulings** (none decided here):

- D-5 / D-C3: is Resume hole-fill (ledger half, plus `retained_tail_join.py`, `startup_join`'s `if retained:`, `WarmupJoinView`, `RESUME_HOLE_*`) dead? Resume was removed in #2550, yet #2850/#2859 describe "a later Resume".
- D-9 / D-C6 `record_decision_session`, D-10 / D-C5 JSONL importer (needs `find <volume>/accounts -name source_bars.jsonl`), D-C2 `_migrate_legacy_binding`.
- B-1 corrupt control intent: per-bot refusal vs whole-lane abort. B-4 money-path ownership ladder with three rungs fed by literals. B-5 the pre-#1583 account registry feeding catalog quarantine (needs the owner's artifact tree). B-13 the 25-character bot-name cap inherited from IBKR `orderRef`.
- A F-A-19 / carryover `ALLOW` (refused at three layers, hash-bound). C-12 the extended-hours decision session (no deploy path sets `use_rth=False`; keep as opt-in or retire across clerk and continuity).
- E-1 the "LEAN native statistics" label. E-17 routes with no client. E-18 / F2-11 / G-7 golden fixtures not in `golden/manifest.json` (`lean-statistics-oracle-v1`, `engine-lab-compatibility-95-96-v1`, `spy-ema-difference-bps`, the three in-package indicator goldens, `lean-set-holdings`).
- F2-9 / G-9: which test proves "live EMA equals LEAN" when `test_ema_crossover_lean_vs_spec.py` and `test_spy_validation.py` skip on every reachable machine (no CI or daily workflow starts a LEAN launcher or stages `Lean/Data`).
- F1-4 (Strategy A's description claims a TradingView reconciliation no repo file holds), F1-5 (RSI provenance understates ENG-009; re-seals every rsi bot).
- G-C5 the seven never-written `OrderEvent` fields (money-trail adjacent). G-22 `OrderType.LIMIT/STOP_MARKET` (#2752 said "stay").

**Cross-module pointers** (not rows of this module):

- Backup blind spot (D, by grep, not reproduced): `recovery._snapshot_source_bar_ledger` snapshots `accounts/alpaca/<clerk account_id>/source_bars.sqlite3`, but every non-Dry-Run ledger lives under `paper:<sid>`, `shadow-evidence:<sid>` or `live-evidence:<sid>`, so `source_bars` in a manifest looks always null for a real account. Needs a Clerk DB fixture to confirm; clerk/fleet recovery readers.
- `app/services/run_replay_proof.py` (480 lines, no app caller; the receipt it fed was cut in #2755) and the comment at `bot_trade_strategy.py:251-258` that still says "The replay proof reads it back from here" (C-11); #3158/#3155 are not the owners — it is outside every chosen module.
- `routers/strategy.py` (E-16) belongs with [#3155](https://github.com/tim1016/learn-ai/issues/3155); `app/research/ml/generate_prediction_set.py:164` carries the same stale "LEAN_DATA_ROOT or LEAN_DATA_CACHE" text as E-C2's guards; `lean-validation-template.ts:23` still maps the retired `spy_ema_crossover` ([#3158](https://github.com/tim1016/learn-ai/issues/3158)); Frontend `components/edge/{cross-asset,regimes,drawers}` run on mock data since #2755 cut their routes.
- `engine.py:205`, `run_gate.py:18`, `cross_runner.py:1-6` name a `POST /api/lean-sidecar/.../cross-reconcile` route that does not exist; `canonical-fixtures.ts:7-12` claims CI catches TS/JSON drift and no test compares them.
- Hand-off to [#3150](https://github.com/tim1016/learn-ai/issues/3150): two copy pins in `test_option_entry_wait_offered.py`.
- Latent, unreachable today: `bot_runner.py:2790-2796` `_binding_recovery_candidates` keys by the binding's sid but stores a candidate built from the loop's own sid (A F-A-20 / B).

**Waits-on rows:** none of this ticket's rows waits on another module's cut. D-C3 and F1-C1 each need a ruling or a serial regeneration first.
