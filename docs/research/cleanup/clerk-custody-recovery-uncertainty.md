# Clean-up findings — Alpaca clerk custody: recovery, uncertainty, reconcile and cutover

- **Ticket:** [Clean-up — Alpaca clerk custody: recovery, uncertainty, reconcile and cutover](https://github.com/tim1016/learn-ai/issues/3148), a module ticket of [Map — Robustness and cleanliness](https://github.com/tim1016/learn-ai/issues/3146).
- **Read at:** `origin/master` `862b50d3` (2026-10-13 21:17 CDT). No open PRs touched this scope.
- **Scope:** 41 Python files — 22 in `PythonDataService/app/broker/alpaca/clerk/sqlite/` (about 15,300 lines) and 19 under `PythonDataService/tests/broker/alpaca/clerk/sqlite/`. The file list is in the ticket body.
- **Depth:** all seven lenses.

## Verdict

**Nothing to cut. No cut PR was opened.** Caller search finds no dead machinery in the 22 app files. No P0 or P1 was found. The scope's 628 tests pass in 23.7 s with zero skips and zero xfails.

**One defect, P2: [F1](#findings).** The reconciliation sweep's backoff overflows after 1,024 consecutive failed passes, `run()` raises `OverflowError`, and the sweep task dies with nothing to restart it until the process restarts. Reproduced with no broker. It is a fix for the handoff, not a cut.

Eight further findings are `leave-with-reason`, `owner-ruling`, or pointers to a ticket that owns the file.

About 3,800 of the scope's lines were added since 2026-10-01, 45% of it in two new files for one feature (#2854, "an operator settles an unknown closing order"): `unknown_order_attestation.py` (1,560 lines) and `unknown_exit_orders.py` (185 lines). The rest of the scope is stable: nine app files and ten test files have no commit since 10-01.

## What was run

| Check | Result |
|---|---|
| `pytest` on the 18 in-scope test files plus the helper (`-rs`, host venv, default sandbox) | **628 passed in 23.74 s**; no skips, no xfails |
| `pytest` on `test_runtime.py`, `tests/services/bot_runner/test_run_ownership.py` and two `tests/broker/fleet` lane-quiet files (exercise the sweep and lane quiet from outside the partition) | 108 passed. Run once in the sandbox: 3 failed with `PermissionError` on `socket.bind`, a sandbox restriction; rerun with the sandbox off: all 108 passed |
| Throwaway repro of the sweep loop with production defaults (`interval_s` 15.0, `max_backoff_s` 300.0), a pass stubbed to fail, no repo, no broker | `OverflowError: int too large to convert to float` after **1,024** failing passes (F1) |
| `ruff --isolated` `S110,S112,BLE001,E722,T201,T203,DTZ001-DTZ007,DTZ011,PGH003,PGH004` on the 22 app files | 2 findings, both `BLE001` in `reconciliation_sweep.py` (F3). Tests (19 files, `S110,S112,BLE001,E722,T201,DTZ003-005,DTZ007`): clean. A first run linted nothing because the shell did not split the file list; the numbers here are from the rerun that did lint all files |
| Temporal ban-list scan (`isoformat`, `datetime`, `timedelta`, `get_calendar`, `utcnow`, `9:30`, `16:00`, …) on the 22 app files | 0 hits. These files carry `int` ms only |
| Module importer search for each of the 22 app files across `PythonDataService`, `scripts`, `docs`, `Frontend` | every module has at least one live importer (table below) |
| Symbol-level caller search: 650 top-level functions, classes, constants and public methods in the 22 files, matched by identifier across 3,900 repo files | 0 symbols with no use outside their own file **and** no use inside it |
| Intra-file reachability from names with an app or script caller | 15 units flagged, all model artifacts: 12 constructors or `__post_init__` (reachable when their class is) and three names those constructors use (`DEVELOPER_RESET_REGISTRY_FILENAME` L45, `_LEASE_HEARTBEATS_PER_TTL` L87, `_is_alpaca_order_id` L220) |
| Every `*_REASON_CODE` constant in `uncertainty_causes.py` has a writer outside the policy and cause files | 36 of 37 do; the 37th (`EXECUTION_COVERAGE_INCOMPLETE_REASON_CODES`) feeds two SQL-placeholder constants that `fee_evidence.py` and `economic_projection.py` (the orders ticket's files) read |
| Operator CLI `manage_alpaca_sqlite_clerk.py` subcommands that reach this scope: `cutover-initialize`, `cutover-plan`, `cutover-apply`, `catalog-quarantine-plan`, `catalog-quarantine-apply`, `dev-reset`, `backup`/`verify`/`restore`/`rebuild`/`reset` | every subcommand is named in `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` and covered by `test_cutover_cli.py` or `test_recovery_tooling.py` |
| Docs that name this scope's symbols: backticked identifiers and file names checked against the code | 23 stale names in 8 docs (F7) |

Importers per module:

| Module | Live importers |
|---|---|
| `catalog_quarantine.py` | `scripts/manage_alpaca_sqlite_clerk.py` |
| `cutover.py`, `cutover_initialization.py`, `recovery.py` | `app/services/alpaca_live_graduation.py`, the CLI, `tests/services/test_alpaca_live_graduation.py` |
| `dev_reset.py`, `developer_reset_registry.py` | the CLI, `clerk/active_runtime.py`, `sqlite/repository_lifecycle.py`, `tests/broker_configuration/test_developer_reset.py` |
| `historical_execution_recovery.py`, `recovery_execution.py`, `recovery_policy.py` | `routers/alpaca_clerk_sqlite.py` (prepare and confirm routes), `sqlite/runtime.py`, `services/broker_v2_panel/sqlite_panel_source.py`, the Frontend bot panel shell |
| `lane_quiet.py` | `clerk/fleet_boot.py`, `clerk/active_protocol.py`, `routers/internal_fleet.py`, `app/main.py` |
| `reconcile.py`, `reconciliation_sweep.py`, `stream_health_sync.py` | `clerk/active_runtime.py`, `clerk/active_authority.py`, `clerk/shadow_broker.py`, `services/sqlite_clerk_compat.py` |
| `residue_discharge.py`, `unknown_exit_orders.py`, `unknown_order_attestation.py`, `exit_recovery.py`, `recovery_status.py`, `uncertainty*.py` | other `clerk/sqlite/` modules, `runtime.py`, `projections.py`, `schemas/alpaca_clerk_sqlite.py` |

## Feature table

"Entry point" is established by caller search. "Reachable" means a checked-in path leads there. A feature is *implemented* when a named test I ran exercises the path. No end-to-end browser run was done, so the Frontend leg is *caller search plus a Frontend spec that mocks the service*, not a live run.

| Feature | Requirement | Entry point | Reachable in checked-in config | Normal / empty / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| Ordered account reconciliation and effect-operation recovery (`reconcile.py`) | Module docstring; ADR 0050 (named in the sweep); #2362, #2369 (comments in the file) | `reconcile_account` from the sweep, boot recovery `facade.recover()` in `active_authority.py`, operator "reconcile now" | Yes | ✓ / ✓ / ✓ | **implemented** | `test_reconcile.py::test_working_own_exit_never_spends_failure_time_or_escalates`, `::test_exit_failure_budget_survives_interruption_without_counting_downtime[mirror_rebuild]`, `::test_exit_failure_budget_pauses_across_weekend_and_early_close` |
| Reconciliation sweep with execution-lease heartbeat and revival | ADR 0050; #2589 (duty-settle hook) | `ReconciliationSweep.start()` (`app/main.py:980`); `start_lease_heartbeat()` from `active_authority.py` and `active_runtime.py`; `revive_now()` from the panel write path | Yes | ✓ / n/a / ✓ — a pass error logs and retries with capped backoff; lease loss attempts fenced revival. **But the loop itself dies after 1,024 straight failed passes (F1)** | **partial** — loop lifetime is unbounded in the design and bounded in the code | `test_runtime.py` and `tests/services/bot_runner/test_run_ownership.py` (108 passed, unsandboxed); repro script for F1. No in-scope test drives the loop past a few failing passes |
| Uncertainty episodes: raise, refresh, resolve; capability answers `NEW_EXPOSURE` / `REDUCTION` (`uncertainty*.py`) | ADR 0048; #1798 | `raise_uncertainty` and capability reads from custody paths | Yes | ✓ / ✓ / ✓ | **implemented** | `test_uncertainty.py::test_raise_uncertainty_is_idempotent`, `::test_new_exposure_uses_the_canonical_attributed_quantity_boundary_fixture`, `::test_cause_encoders_emit_the_unique_sorted_order_required_by_decoders` |
| Stream-health account hold on its own fixed cadence (`stream_health_sync.py`) | #1777 WP4 finding S10 | `StreamHealthHoldSync.start()` from `active_runtime.py` | Yes | ✓ / ✓ / ✓ | **implemented** | `test_stream_health_sync.py::test_a_single_bad_sample_never_raises_the_hold`, `::test_a_sustained_outage_raises_within_two_ticks`, `::test_an_unchanged_outage_appends_nothing_further` |
| Operator discharges a stranded attributed residue (`residue_discharge.py`) | #2381, #2305, #2343, #2344 | panel action `discharge_attributed_residue` | Yes | ✓ / ✓ / ✓ | **implemented** | `test_residue_discharge.py::test_flat_broker_discharges_the_residue_and_resolves_the_exit_fences`, `::test_broker_holding_the_shares_refuses_and_writes_nothing`; `test_late_fill_after_residue_discharge.py::test_a_closing_orders_fill_that_arrives_on_the_stream_after_its_position_was_discharged_is_not_credited` |
| Operator settles an unknown closing order (`unknown_order_attestation.py`, `unknown_exit_orders.py`) | #2854 | panel action `settle_unknown_exit_order` (Frontend `bot-toolbar.component.ts`, `bot-panel-shell.component.ts`) | Yes | ✓ / ✓ / ✓ | **implemented** (module); Frontend leg *caller search + mocked spec* | `test_unknown_order_attestation.py::test_an_order_alpaca_filled_is_taken_up_its_fill_credited_once_and_the_exit_ends_flat`, `::test_the_same_settlement_asked_for_again_returns_its_receipt_and_does_nothing_twice`; `test_unknown_order_settlement_crash.py::test_a_settlement_the_process_dies_in_is_finished_by_a_retry_or_the_clerks_passes` |
| Recovery capability catalog and its execute-time recheck (`recovery_policy.py`, `recovery_execution.py`, `recovery_status.py`, `exit_recovery.py`) | ADR 0045; ADR 0047; #1752 | `recheck_recovery_action` from the bot-page projection and the execute route | Yes | ✓ / ✓ / ✓ | **implemented** | `test_recovery_policy.py::test_a_flatten_presented_under_a_pass_is_refused_at_commit_once_the_pass_no_longer_stands`, `::test_settling_is_refused_outside_its_state` |
| Paper-only recovery of one exact missing execution (`historical_execution_recovery.py`) | Named in ADR 0060 | routes `…/historical-execution-recovery/prepare` and `…/confirm`; Frontend `bot-panel-shell.component.ts` | Yes | ✓ / ✓ / ✓ | **implemented** (module); Frontend leg *caller search + mocked spec* | `test_historical_execution_recovery.py::test_historical_exact_execution_recovery_prepares_confirms_and_replays` |
| Account half of a draining lane's quiet answer (`lane_quiet.py`) | #2154, ADR 0063 Decision 2 | `internal_fleet` lane-quiet route via `fleet_boot.py` | Yes | ✓ / ✓ / ✓ | **implemented** | `test_lane_quiet.py::test_an_empty_account_is_quiet_only_after_two_empty_reads`, `::test_an_order_placed_by_hand_blocks_quiet`; `tests/broker/fleet/test_lane_quiet_transport.py` |
| Live-authority cutover: plan, apply, initialize (`cutover.py`, `cutover_initialization.py`) | ADR 0059; ADR 0047; `docs/references/alpaca-live-authority.md` | CLI `cutover-*`; `app/services/alpaca_live_graduation.py` | Yes (runbook-referenced) | ✓ / ✓ / ✓ | **implemented** | `test_cutover.py::test_initialize_serializes_concurrent_attempts_for_one_account`; `test_cutover_live.py::test_a_never_legacy_account_graduates_end_to_end`, `::test_an_account_must_be_flat_and_order_free_to_graduate`; `test_cutover_cli.py::test_main_plan_and_apply_require_the_exact_confirmation_token` |
| Offline backup, verify, restore, rebuild, reset (`recovery.py`) | ADR 0047 | CLI `backup` / `verify` / `restore` / `rebuild` / `reset` | Yes (runbook-referenced) | ✓ / ✓ / ✓ | **implemented** | `test_recovery_tooling.py::test_interrupted_backup_keeps_previous_verified_publication`, `::test_activation_requires_matching_database_and_referenced_artifacts` |
| Bounded quarantine of pre-SQLite runner catalogs (`catalog_quarantine.py`) | Runbook `alpaca-sqlite-clerk-recovery-and-cutover.md` | CLI `catalog-quarantine-plan` / `-apply` | Yes (runbook-referenced) | ✓ / ✓ / ✓ | **implemented** | `test_catalog_quarantine.py::test_apply_rolls_back_an_earlier_move_when_a_later_move_fails`, `::test_apply_refuses_changed_catalog_without_moving_anything` |
| Paper-only developer clean-slate reset (`dev_reset.py`, `developer_reset_registry.py`) | ADR 0059 (D10: refuses live) | CLI `dev-reset` | Yes (runbook-referenced) | ✓ / ✓ / ✓ | **implemented** | `test_dev_reset.py::test_reset_refuses_unreadable_current_bot_before_moving_any_authority`, `::test_reset_clears_canonical_paper_bots_and_preserves_other_accounts` |

One feature is *partial* (the sweep, F1). None is *missing* or *unverified*.

## Findings

| # | `path::symbol` | Lens | Severity | Evidence | Disposition | What a fix orphans |
|---|---|---|---|---|---|---|
| F1 | `reconciliation_sweep.py::ReconciliationSweep.run` (L372-392; the line is `self._interval_s * (2**consecutive_failures)`, L386) | 3 | **P2** by the repo's labels (fails closed: new exposure stays blocked; reconciliation, stuck-EXIT re-drive and hold resolution stop until a process restart). The owner may raise it to P1 if the panel keeps showing a healthy sweep while the task is dead — not verified here | `consecutive_failures` counts every pass that is not a success — an error, or `verdict == "stale"`, which `reconcile.py` returns when the broker answer is an error (L942), or incomplete (L1433, L1466, e.g. a multi-leg parent whose answer never names its legs). The delay is `interval_s * 2**n` and only then capped by `min`. At `n = 1,024` the int exceeds float range and `float * int` raises `OverflowError` **outside** any `try`, so `run()` ends. Reproduced: 1,024 failing passes, then `OverflowError`. Production defaults (15 s interval, 300 s cap) reach 1,024 straight failures after about 85 hours of continuous stale — a long weekend of broker unavailability, or one unattributable order that keeps every pass stale. Nothing supervises the task: `start()` re-creates a finished task but is called once, from `app/main.py:980`; the lease-heartbeat task keeps running, so the lease still looks healthy | **fix** — cap the exponent before the multiply, for example `2 ** min(consecutive_failures, 10)`; add a test that runs 1,100 failing passes. Small, money-path adjacent (one reviewer if it ships alone) | nothing |
| F2 | `reconciliation_sweep.py::ReconciliationSweep.stop` (L141), `stream_health_sync.py::StreamHealthHoldSync.stop` (L159) | 3 | robustness | `with suppress(asyncio.CancelledError): await task` also swallows a cancellation delivered to the caller of `stop()` while it awaits the cancelled task. Callers: the runtime's `close()` (`active_runtime.py:282`, taps in `_ordered_taps` order), the failed-composition cleanup (`active_runtime.py:568`) and the opening cleanup (`active_authority.py:540`, which re-raises the original exception afterwards). All are teardown; none relies on the stopper being cancellable | **leave-with-reason** — shutdown-only. Fix with `asyncio.gather(task, return_exceptions=True)` if a second caller appears | nothing |
| F3 | `reconciliation_sweep.py:280`, `:409` | 3 | hygiene | `ruff BLE001`. Both handlers log with a structured `action` and `exc_info` (the first via `_log_orphaned_revival_failure`); the pass handler returns `False` and the loop retries with backoff. Not silent | **leave-with-reason** | nothing |
| F4 | 19 files under `tests/broker/alpaca/clerk/sqlite/` | 7 | hygiene | 92 `# noqa: F811`, 5 `# noqa: F401` and 57 `# type: ignore` in tests; none in the 22 app files. The `F811` set is the fixture re-import idiom | **leave-with-reason** — a refactor with no defect behind it (map: out of scope). A conftest move would remove them | nothing |
| F5 | `schema.py:629-632` (owned by the store-plumbing ticket) | 2 | hygiene | Comment states a fresh file "creates the v9 table and drops it moments later" — construction of a table the next statement drops | **pointer** to [Clean-up — Alpaca clerk custody: store plumbing](https://github.com/tim1016/learn-ai/issues/3149) | — |
| F6 | `docs/references/alpaca-live-authority.md:48` | 1 | owner-ruling | "a `LiveDeactivationRecord` mirroring disarm is the follow-up." The symbol appears nowhere else in the repo; arming was re-founded as a cash budget since | **owner-ruling**: the follow-up is a *missing* feature the doc promises — build it, or drop it and edit the sentence | the sentence |
| F7 | docs naming symbols that no longer exist (list below) | 7 | hygiene | 23 backticked names across 8 docs do not appear in any code file | **pointer** to the docs wave (not charted). Several of the docs restate code, which AGENTS.md says goes | the docs themselves |
| F8 | `docs/known-gaps.md`, section "Fleet account retirement — verified 2026-09-21" | 1 | owner-ruling | Two open P2 entries: no recovery ceremony for an account released with `lane_confirmation: absent`, and a resurrection-capable window for a lane unreachable for the whole drain. Both are deliberate and procedure-bounded, and both sit on `lane_quiet` and the drain handover | **owner-ruling** (build-or-drop) for the missing ceremony; the second is bounded by procedure. Pointer to [Clean-up — Fleet control plane](https://github.com/tim1016/learn-ai/issues/3153) | — |
| F9 | tests added since 2026-10-01: +101 test functions in 7 files | 6 | — | Added: `test_unknown_order_attestation.py` +48, `test_late_fill_after_residue_discharge.py` +23, `test_recovery_policy.py` +20, `test_unknown_order_settlement_crash.py` +6, `test_crash_restart.py` +2, `test_residue_discharge.py` +1, `test_uncertainty.py` +1. Every name states an outcome. Four read in full as suspects (`test_every_durable_write_of_a_settlement_is_a_window_these_tests_cover`, `test_an_attestation_names_an_alpaca_order_id_only_as_alpaca_spells_it`, and the two `…names_every_notice_that_would_allow_it` tests): none fails the bar — the last two assert at different layers (offer vs refusal) and compare against `_REASON_POLICIES`, not a literal | **nothing to fix here** — the other 97 were judged by name and by passing, not read line by line | nothing |

### F7 — stale names in docs

| Doc | Names with no match in code |
|---|---|
| `docs/references/alpaca-live-arming.md` L10-14 | `live_arming_gate.py`, `sqlite/arming_refresh.py`, `live_arming_admission.py`, `arming_status`, `sessions_used`, `arming_policy.py` |
| `docs/references/alpaca-live-envelope.md` L129-130, L151, L401 | `account_net_cash_spent_usd`, `tests/fixtures/alpaca/fill_visibility/paper-btcusd-2026-09-28.json`, `attribution.md` |
| `docs/architecture/engine-authority-map.md` L53-54, L71, L92 | `docs/references/ema-crossover-2-bps.md`, `tests/engine/test_single_store_tombstone.py`, `reconstruct_legacy_program_seal` (lives only in ADR 0043), `tests/fixtures/test_alpaca_account_day_pnl_fixture.py` |
| `docs/references/two-bots-one-symbol-2469.md` L137, L298, L305 | `tests/fixtures/alpaca/orders/orders.json`, `pattern_day_trader`, `daytrade_count`, `daytrading_buying_power`, `tests/fixtures/alpaca/account/account.json` |
| `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` L191 | `reconcile.reconcile_uncertain_order` |
| `docs/references/alpaca-sqlite-clerk-source-guarantees.md` L10 | `after_order_id`, `before_order_id` |
| `docs/references/alpaca-live-authority.md` L48 | `LiveDeactivationRecord` |
| `docs/references/alpaca-sqlite-clerk-lease-heartbeat-cadence.md` L22 | `numerical-rigor.md` (a rule-file citation) |

Some of these are test-fixture paths the doc cites for an outside fact; check each before cutting a line.

## Pointers, not findings

- The earlier dead-code cut has landed in this scope: no `EXECUTION_CORRECTED` string remains in `app/` or `tests/`, and `SCHEMA_MIGRATIONS` in `schema.py` starts at v13. See [Kill list — dead code in the Alpaca clerk](https://github.com/tim1016/learn-ai/issues/2701) and [Census — clerk store schema versions and correction rows](https://github.com/tim1016/learn-ai/issues/2736).
- `known-gaps.md` §9 "F17 — `prepare_safe_flatten` enablement" is the v2 panel's: [Clean-up — v2 panel read model](https://github.com/tim1016/learn-ai/issues/3151).
- The `uncertainty` reason codes whose writers are option-event files (`OPTION_EVENT_*`, `OPTION_SHARE_TRADE_*`) belong to [Clean-up — Alpaca clerk option legs and margin](https://github.com/tim1016/learn-ai/issues/3150).

## Cleanups that could not be completed

None. There was nothing to cut. (Not "waits on another module's cut": every module in the scope has a live importer today.)

## Limits of this read

- The 101 tests added since 10-01 were judged by name and four body reads; the 527 older tests were not reread (the earlier test kill lists #2717–#2719 already judged them).
- The Frontend leg of the two operator actions was traced by caller search and by the component specs, which mock the service. No browser run was done.
- Symbol search matches identifiers by name across the repo, so a common method name can hide a dead method behind an unrelated caller. That biases toward keeping.
- F1's production timing is arithmetic from the defaults (15 s interval, 300 s cap), not an observed outage. The repro runs the real `run()` with the pass stubbed.
- Postgres-gated tests: none are in this scope.
