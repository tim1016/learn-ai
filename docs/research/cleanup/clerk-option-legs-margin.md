# Clean-up findings — Alpaca clerk option legs and margin

- **Ticket:** [Clean-up — Alpaca clerk option legs and margin](https://github.com/tim1016/learn-ai/issues/3150), a module ticket of [Map — Robustness and cleanliness](https://github.com/tim1016/learn-ai/issues/3146).
- **Read at:** `origin/master` `862b50d3` (2026-10-13 21:17 CDT). No open PR touched this scope.
- **Scope:** 40 app files (17,872 lines) and 58 test files (27,887 lines; 54 test modules and 4 fixture or harness modules, 1,053 test functions). Every line is new since 2026-10-01: 188 non-merge commits, no file in scope existed at `01f58a7b`. The file list is in the ticket body.
- **Depth:** all seven lenses.

## Verdict

**Five small pieces of dead machinery, cut in one delete-only PR** (49 lines out, 3 changed). Each is a leftover of [#2913](https://github.com/tim1016/learn-ai/issues/2913)'s option work: a method, a function, a constant, a method, and a parameter that nothing in the repo calls, with no test naming them. **No P0 or P1 found.** No defect that changes a number or an order.

The scope is healthy where it was checked: 1,668 in-scope tests pass with no skips and no xfails, 965 neighbouring service, v2 panel and fixture tests pass, `ruff` at project scope is clean, no temporal ban-list hit, no silent handler, every module has a live importer, every cache and outbound call read is bounded.

Six other findings (F2-F7) are `leave-with-reason`, `fix` for the handoff, or an owner ruling that is already tracked. The two that matter most:

- **F2** — a parameter, `live_shadow`, is passed through four files and read by none of them. The cut would have to edit `clerk/sqlite/runtime.py`, which is [store plumbing's](https://github.com/tim1016/learn-ai/issues/3149), so it waits.
- **F4** — three tests in `test_held_option_quotes.py` assert a measured wall-clock time against a bound, the class [#3126](https://github.com/tim1016/learn-ai/issues/3126) and [#3133](https://github.com/tim1016/learn-ai/issues/3133) fixed elsewhere. Not seen to fail.

## What was run

| Check | Result |
|---|---|
| `pytest` on the 58 in-scope files (`-rsxX`, host venv, default sandbox) | **1,668 passed in 64.7 s**; no skips, xfails or errors |
| Same, on the cut branch | **1,668 passed in 77.9 s** |
| `pytest` on 27 neighbouring files that exercise the service and panel half of the scope from outside the ticket's test partition (`tests/services/test_option_*`, `tests/services/bot_runner/test_option_*`, `tests/broker/v2panel/*option*`, `tests/broker/fleet/test_option_model_inputs_operation.py`, four option golden-fixture tests, the hold-acknowledgement route test, the qualification-bundle script test), unsandboxed | **965 passed in 120 s**; no skips. Again on the cut: **965 passed in 88.9 s** |
| `ruff check app/ tests/` (project config, project scope) on the cut | All checks passed |
| `ruff --isolated` `S110,S112,BLE001,E722,T201,T203,DTZ001-DTZ007,DTZ011,PGH003,PGH004,F401,F811,F841,UP040` on the 40 app files | 8 findings, all `BLE001` (F5). The 58 test files with `S110,S112,BLE001,E722,T201,DTZ003-005,DTZ007,F841`: clean. A first run linted nothing because zsh did not split the file list; the numbers here are from the `xargs` rerun, which linted all 98 files |
| `ruff --isolated` `ARG001-ARG005` on the 40 app files | 2 findings: `option_plan_taken(live_shadow)` (F2) and `_why_a_real_account_cannot_end(conn)` (cut) |
| Symbol-level caller search: 973 functions, classes, constants and methods in the 40 files, matched by identifier across 7,384 tracked files | 10 with no use anywhere (4 dead, 6 are pydantic `model_validator` methods the decorator registers) and 4 used only by tests (F3) |
| Intra-file reachability from names with a live caller (validators and dunders treated as reachable with their class) | 8 unreachable units: the 4 dead and the 4 test-only. Nothing else |
| Importer search for each of the 40 modules (dotted path and `from … import`) | Every module has at least one importer in `app/`; table below |
| Temporal ban-list scan (`isoformat`, `datetime`, `timedelta`, `get_calendar`, `utcnow`, `strptime`, `ZoneInfo`, `09:30`, `16:00`, …) on the 40 app files | 24 lines, all prose or an import, one inbound vendor date converted at the boundary, one calendar-backed day loop (lens 4 below) |
| Provenance blocks: `Validated against:` in the 40 files; any `NONE` | Present in the 12 files that carry a formula; none says `NONE` (lens 4 below) |
| `docs/known-gaps.md`: entries naming this scope | none |
| Docs naming an in-scope module: backticked names and paths checked against the code | 5 docs; the names that do not resolve are ADR table shorthand or another module's (Pointers) |
| Tests added since 10-01 against the test bar, by AST shape (no assertion, `assert True`, tombstone, long-literal equality, identical bodies) | Nothing fails the bar at shape level; 13 no-assertion shapes read, all assert through a helper or by not raising (lens 6 below) |

Importers per module (all 40 have a live importer in `app/`):

| Module | Live importers |
|---|---|
| `held_option_quotes.py` | `active_authority.py`, `active_runtime.py`, `shadow_authority.py`, `market_liveness.py` |
| `kept_option_quotes.py` | `active_authority.py`, `exit_estimate.py`, `held_option_quotes.py`, `shadow_authority.py` |
| `option_effects.py` | `active_protocol.py`, `kept_option_quotes.py`, `simulated_*_orders.py` and 12 more |
| `option_position_money.py` | `budget_projection.py` |
| `simulated_multi_leg_orders.py` | `simulated_option_orders.py` |
| `simulated_option_expiry.py` | `shadow_broker.py`, `synthetic_broker.py` |
| `simulated_option_orders.py` | `shadow_broker.py`, `simulated_option_expiry.py`, `synthetic_broker.py` |
| `simulated_option_settlement.py` | `shadow_broker.py`, `simulated_option_expiry.py`, `broker_port_guard.py`, `deadline_keeper.py` and 6 more |
| `sqlite/account_option_requirement.py` | `entry_buying_power.py`, `account_picture_read.py`, `budget_deploy.py` |
| `sqlite/entry_buying_power.py` | `runtime.py`, `lane_summary.py`, `option_buying_power_views.py`, the qualification bundle script |
| `sqlite/legs_out_of_step.py` | `multi_leg_evidence.py`, `reconcile.py` |
| `sqlite/multi_leg_evidence.py` | `order_evidence.py` |
| `sqlite/option_admission.py` | `runtime.py`, `scheduled_close.py`, `order_terms.py` |
| `sqlite/option_close.py` | `exit_resolution.py` |
| `sqlite/option_decision_fills.py` | `active_protocol.py`, `runtime.py` |
| `sqlite/option_entry.py` | `decision_receipts.py`, `enter.py`, `entry_buying_power.py`, `entry_outcome.py` and 12 more |
| `sqlite/option_event_acknowledgement.py` | `option_events.py`, `runtime.py`, the clerk route and schema |
| `sqlite/option_event_books.py` | `bot_history.py`, `budget_projection.py`, `exit_resolution.py`, `exit_watchdog.py` and 9 more |
| `sqlite/option_event_folds.py` | `bot_history.py`, `budget_projection.py`, `fee_evidence.py`, `folds.py` and 5 more |
| `sqlite/option_events.py` | `deadline_keeper.py`, `reconcile.py`, `recovery_execution.py`, `runtime.py` |
| `sqlite/option_order_evidence.py` | `claimed_broker_io.py`, `multi_leg_evidence.py`, `order_evidence.py`, `reconcile.py` |
| `sqlite/option_order_legs.py` | `simulated_multi_leg_orders.py`, `bot_history.py`, `entry_buying_power.py`, `folds.py` and 8 more |
| `sqlite/option_owner_ending.py` | `option_events.py`, `recovery_policy.py`, `runtime.py` |
| `live_option_bots.py` | `broker.py`, `runtime.py`, `config.py`, `option_deploy.py` |
| `multi_leg_option_orders.py` | `option_effects.py`, `config.py` |
| `option_buying_power_judged.py` | `runtime.py`, `config.py`, the clerk route |
| `option_fees.py` | `option_effects.py`, `simulated_option_expiry.py`, `option_admission.py`, `fees.py` and 2 more |
| `services/bot_option_run.py` | `bot_trade_strategy.py`, `option_deploy.py` |
| `services/broker_v2_panel/option_deploy.py` | `panel_deploy.py` |
| `services/broker_v2_panel/option_structure_panel.py` | `broker_v2_panel.py`, `option_deploy.py` |
| `services/option_buying_power_views.py` | the clerk route, the qualification bundle script |
| `services/option_ending_views.py` | `bot_history.py`, `sqlite_clerk_transaction_projection.py` |
| `services/option_model_inputs.py` | `main.py`, `internal_fleet.py`, `fleet_option_model_inputs.py` and 6 more |
| `services/option_model_inputs_client.py` | `option_model_inputs.py` |
| `services/option_preview_out_of_range.py` | `option_structure_panel.py`, `option_structure_preview.py` |
| `services/option_preview_views.py` | `exit_estimate_read.py`, `option_preview_words.py`, `option_structure_preview.py` |
| `services/option_preview_words.py` | `option_deploy.py`, `option_structure_panel.py`, `option_preview_out_of_range.py`, `option_preview_views.py` |
| `services/option_search.py` | `main.py`, `decay.py`, `worker.py`, `bot_option_run.py` |
| `services/option_structure_preview.py` | `option_deploy.py`, `option_structure_panel.py` |
| `services/option_watch.py` | `market_liveness.py`, `bot_option_run.py`, `option_structure_panel.py`, `option_structure_preview.py` |

## Feature table

"Entry point" is established by caller search. "Reachable" means a path leads there from the checked-in configuration; three settings are off in the checked-in configuration and are named in F6. A feature is *implemented* when a test I ran names the path. The tests were judged by name; their bodies were read only where a finding below says so. No end-to-end browser run and no live Paper run was done.

| Feature | Requirement | Entry point | Reachable in checked-in config | Normal / empty / failure | Class | Evidence |
|---|---|---|---|---|---|---|
| One bought option entered on a Dry Run, Live Shadow, Paper (`option_admission.py`, `option_entry.py`, `option_decision_fills.py`) | ADR 0076 decisions 2, 5, 7; #2913 | `work_out_option_entry` from `clerk/sqlite/runtime.py` through the bot runner | Dry Run, Live Shadow and Paper: yes. Live: only with `ALPACA_LIVE_OPTION_BOTS_ENABLED` | ✓ / ✓ / ✓ | **implemented** | `test_option_entry_and_exit.py::test_an_entry_buys_the_picked_contract_at_its_ask_and_the_bot_holds_it`, `::test_a_contract_already_expired_is_refused`; `test_paper_option_orders.py::test_a_paper_account_sends_one_bought_option_as_a_limit_order_that_rests`; `test_option_entry_fences.py::test_a_decision_sent_again_for_more_contracts_is_a_conflict_and_the_books_keep_the_first` |
| One order of several legs (`multi_leg_option_orders.py`, `option_order_legs.py`, `legs_out_of_step.py`, `multi_leg_evidence.py`) | ADR 0076; #2913 | same, with a plan of two to four legs | Dry Run and Live Shadow: yes. Paper and Live: only with `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED` (F6) | ✓ / ✓ / ✓ | **implemented** on simulations; **unverified on a real account** until D3 | `test_several_legs_dry_run.py::test_every_catalogue_structure_enters_and_counts_its_exact_leg_ratios`, `::test_every_catalogue_structure_is_refused_whole_when_one_leg_has_no_quote`; `test_several_legs_paper.py::test_the_entry_sends_one_parent_and_reads_every_listing_with_one_account_read`; `test_multi_leg_evidence.py::test_a_final_uneven_fill_holds_only_this_bot_and_allows_a_confirmed_reduction`; `test_several_legs_leave_one_leg_unchanged.py::test_a_one_leg_option_enter_is_stored_with_the_facts_and_the_hash_it_had_before_several_legs` |
| One closing order per structure (`option_close.py`) | ADR 0076 decisions 16 and 27 | `exit_resolution.py` | yes | ✓ / ✓ / ✓ | **implemented** | `test_multi_leg_close.py::test_a_structure_closes_once_in_one_market_order_and_counts_every_leg`, `::test_a_close_with_one_unpriced_leg_records_no_leg_fill`; `test_option_close_fences.py::test_an_exit_whose_own_quote_has_no_bid_is_not_sold_at_the_bid_its_entry_saw` |
| Held and kept option quotes: a held contract keeps its mark (`held_option_quotes.py`, `kept_option_quotes.py`) | ADR 0076; #2913 | `HeldOptionQuotes.drive` from `market_liveness.py`; reads from the exit estimate and close fences | yes | ✓ / ✓ / ✓ | **implemented** | `test_held_option_quotes.py::test_the_newest_quote_of_a_held_contract_is_kept_and_again_fifteen_seconds_on`; `test_kept_option_quotes.py::test_the_newest_quote_alone_prices_a_close_and_an_older_bid_prices_nothing_once_a_newer_quote_has_none`, `::test_a_whole_day_of_hand_overs_for_fifty_contracts_leaves_a_keep_and_a_mark_read_cheap` |
| Simulated option orders, several-leg orders, settlement and expiry (`simulated_*.py`) | ADR 0076 (what a simulation fills, and what it does not claim) | `synthetic_broker.py`, `shadow_broker.py` | yes (every Dry Run and Live Shadow) | ✓ / ✓ / ✓ | **implemented** | `test_synthetic_broker_option_orders.py::test_an_opening_order_is_filled_in_full_at_the_ask_of_the_quote_bound_to_it`; `test_simulated_multi_leg_orders.py::test_one_missing_quote_refuses_the_whole_structure_and_records_no_leg`; `test_simulated_option_settlement.py::test_a_contract_is_worth_its_exercise_value_rounded_down_to_the_cent`; `test_dry_run_option_expiry.py::test_a_call_in_the_money_is_settled_in_cash_at_its_exercise_value_and_the_bot_holds_no_shares` |
| Option events: exercise, assignment, expiry booked from the account's non-trade activities, with holds (`option_events.py`, `option_event_*.py`) | ADR 0076 decision 14; Alpaca "Non-Trade Activities for Option Events" | the account keeper's round; `reconcile.py`; panel acknowledgement route | yes | ✓ / ✓ / ✓ | **implemented** | `test_option_event_booking.py::test_the_books_hold_the_shares_and_cash_alpaca_documents_after_each_exercise_and_assignment`; `test_option_event_holds.py::test_a_row_that_cannot_be_read_is_held_for_and_nothing_of_it_is_booked`; `test_option_events_on_an_account.py::test_a_stock_only_accounts_poll_asks_alpaca_nothing_and_writes_nothing`; `tests/routers/test_option_event_hold_acknowledgement_route.py` (neighbour) |
| The owner ends an expired contract nothing will settle (`option_owner_ending.py`) | ADR 0076 (owner ending); #2913 | panel action through `option_events.py` | yes | ✓ / ✓ / ✓ | **implemented** | `test_option_owner_ending.py::test_a_dry_run_contract_with_no_price_is_ended_at_the_owners_word_and_what_it_cost_is_the_loss`; `test_scheduled_close_meets_option_events.py::test_under_a_missed_close_an_expired_contract_is_never_written_off_and_the_owner_ends_it_at_their_word` |
| Account option requirement: what the account's holdings need, netted across its bots (`account_option_requirement.py`) | PRD #3095 A1, B1; ADR 0076 decision 26 | `entry_buying_power.py`, `account_picture_read.py`, `budget_deploy.py` | yes | ✓ / ✓ / ✓ | **implemented** | `test_account_option_requirement.py::test_the_accounts_holdings_are_each_contract_netted_across_its_bots_at_its_recorded_multiplier`, `::test_a_first_spread_on_a_flat_account_adds_its_whole_requirement` |
| The entry's comparison with Alpaca's option buying power (`entry_buying_power.py`, `option_buying_power_judged.py`) | PRD #3095 D1; ADR 0076 decision 26 | the Clerk's entry path; the comparison route | Recording: yes. **Judging: only with `ALPACA_OPTION_BUYING_POWER_JUDGED`** | ✓ / ✓ / ✓ | **implemented** (recording); judging **partial by design** — waits on [#3103](https://github.com/tim1016/learn-ai/issues/3103) | `test_entry_buying_power.py::test_a_fall_of_exactly_the_premium_paid_is_consistent_and_the_fee_provision_raises_nothing`, `::test_a_fall_that_is_not_the_cost_and_not_above_the_set_aside_is_over_reserved_and_holds_nothing`, `::test_an_account_read_that_fails_after_the_listing_answered_is_recorded_as_failed` |
| Option fees: the per-contract, SEC and TAF fee model (`option_fees.py`) | Alpaca's published fee schedule; ADR 0076 | `option_effects.py`, `simulated_option_expiry.py`, `option_admission.py`, `fees.py` | yes | ✓ / ✓ / ✓ | **implemented** | `test_option_fees.py::test_a_sale_owes_the_sec_fee_on_its_whole_premium_not_on_its_price`; `tests/fixtures/test_alpaca_option_fees_fixture.py` (golden, neighbour) |
| Live-account fence: no option order that opens reaches a Live account while the setting is off, and a close always does (`live_option_bots.py`) | ADR 0076 decision 24 | the account Clerk, Deploy and the Alpaca port | yes (off unless a deployment turns it on) | ✓ / ✓ / ✓ | **implemented** | `test_option_live_setting.py::test_with_the_setting_off_a_live_clerk_refuses_the_plan_and_says_what_to_do`; `test_option_live_setting_closes.py::test_a_contract_bought_while_live_was_open_is_closed_by_its_bot_after_live_is_switched_off`; `test_broker_option_orders.py::test_an_order_whose_every_leg_closes_is_sent_to_a_live_account_while_live_is_switched_off` |
| Deploy, preview and review of an option structure (`option_deploy.py`, `option_structure_panel.py`, `option_structure_preview.py`, `option_preview_*.py`, `option_buying_power_views.py`) | ADR 0076; PRD #3095 B1, B2 | v2 panel deploy and preview routes | yes | ✓ / ✓ / ✓ | **implemented** (neighbour tests) | `tests/broker/v2panel/test_option_deploy_money_path.py`, `test_option_budget_review.py`, `test_option_structure_preview_route.py`; `tests/services/test_option_structure_preview.py`, `test_option_preview_words.py` |
| The option bot's run: search, entry window, exit by the clock (`bot_option_run.py`, `option_search.py`, `option_watch.py`) | ADR 0076; #2913 | `bot_trade_strategy.py`; `main.py` for lane teardown | yes | ✓ / ✓ / ✓ | **implemented** (neighbour tests) | `tests/services/bot_runner/test_option_bot_dry_run.py`, `test_option_bot_entry_outcome.py`; `tests/services/test_option_search.py::test_closing_a_lane_leaves_no_real_worker_whatever_it_was_doing_and_lets_its_process_end` |
| Model inputs: the rate and declared dividends a preview prices with, asked of the coordinator on a fleet Clerk (`option_model_inputs.py`, `option_model_inputs_client.py`) | ADR 0076; #2913 | `internal_fleet.py`, `fleet_option_model_inputs.py`, the preview | yes | ✓ / ✓ / ✓ | **implemented** (neighbour tests) | `tests/services/test_option_model_inputs.py`, `test_option_model_inputs_client.py`; `tests/broker/fleet/test_option_model_inputs_operation.py` |
| Option orders on Paper through their whole life, with the evidence kept | PRD #3095 D2, D3 | `qualify-option-orders-on-paper.md` runbook; the qualification kit | the kit: yes; the run: owner-driven | recordings ✓; the run itself is open | **partial by design** | `test_paper_option_recordings.py::test_each_order_goes_pending_new_then_new_then_a_fill_at_the_price_its_own_order_states`; D2 [#3102](https://github.com/tim1016/learn-ai/issues/3102) closed, D3 [#3103](https://github.com/tim1016/learn-ai/issues/3103) open |

One feature is *unverified on a real account* (the several-leg order), and one is *partial by design* (judging, and the Paper run). None is *missing*.

## Findings

| # | `path::symbol` | Lens | Severity | Evidence | Disposition | What a fix orphans |
|---|---|---|---|---|---|---|
| F1 | five symbols, below | 2 | hygiene | each has no mention anywhere in the repo besides its own definition: no caller, no test, no doc, no Frontend reference | **fix — cut in [the delete-only PR](https://github.com/tim1016/learn-ai/issues/3150)** | `closing_order` import in `option_entry.py`; `asked_option_order`, `evidence_of_the_order_asked` and `BrokerOptionOrder` imports in `multi_leg_evidence.py` (both functions keep other callers) |
| F2 | `option_effects.py::option_plan_taken` parameter `live_shadow` | 2 | hygiene | `ruff ARG001`. The body reads `real_account` only; the docstring names `real_account` and says a Live Shadow is `False`. The value is passed by `option_admission.py::work_out_option_entry` (L302, L311) and by four sites in `clerk/sqlite/runtime.py` (L1113, L1220, L2029, L2085, each `live_shadow=self.authority_kind == "shadow"`) | **fix (cut), waits on [#3149](https://github.com/tim1016/learn-ai/issues/3149)**: the chain ends in `runtime.py`, which that ticket owns; a cut here would leave four call sites passing an unknown keyword | the `live_shadow` parameter of `work_out_option_entry` and five keyword arguments |
| F3 | `held_option_quotes.py::HeldOptionQuotes.read_books` (40 test uses), `kept_option_quotes.py::KeptOptionQuotes.last_stated` (30), `sqlite/legs_out_of_step.py::bot_is_under_legs_out_of_step` (10), `option_search.py::OptionSearchLane.worker_pid` (12), `held_option_quotes.py::reset_held_option_quotes_for_testing` (12) | 2 | hygiene | each has no caller in `app/`, `scripts/` or the Frontend; the tests call it as their observation point. `last_stated`'s last use inside its own class went with `c0bc6b7a` (#2913, which added `stated_mark`); it now answers the same question without the whole-quote rule | **leave-with-reason** — not delete-only: removing one means rewriting the tests that prove held-quote, custody and worker lifecycle behaviour through it. A handoff slice may point those reads at `stated_mark`, `repo.active_uncertainty` and `_worker_pid` and then delete the five | the five, once the tests are moved |
| F4 | `tests/broker/alpaca/clerk/test_held_option_quotes.py` (L791 and L825 `longest_pass_s < 0.25`; L1028 `took_s < 1.0`, two parameter cases) | 6 | robustness | three tests, four cases, assert a measured `time.monotonic()` span against a bound. Same class as the two tests [#3134](https://github.com/tim1016/learn-ai/pull/3134) fixed. Not seen to fail here; CI shards run on two vCPU. Each already proves the real point by event order: the stuck thread is still blocked when `drive()` returns | **fix** for the handoff: assert the pass returned while `stuck` is still unset, and drop the elapsed-time bound | nothing |
| F5 | `held_option_quotes.py:374,392,470,483,634`; `sqlite/entry_buying_power.py:262`; `option_model_inputs.py:481`; `option_search.py:554` | 3 | hygiene | `ruff BLE001` on eight `except Exception`. Each logs with a structured `action` (and `exc_info` where it is a fault), records it on the fault log, returns the exception to a caller that records it, or sets it on the waiting future. None is silent | **leave-with-reason** | nothing |
| F6 | `ALPACA_LIVE_OPTION_BOTS_ENABLED`, `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED`, `ALPACA_OPTION_BUYING_POWER_JUDGED` (`app/config.py:273,298,307`) | 1 | owner-ruling | all three default to `False` and no checked-in file turns one on (opt-in, so not dead). **Per the #3103 step log, `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=true` is on in `deploy/fleet/env/paper.env`, marked TEMPORARY, and the Paper lane was recreated with it at 16:50 CDT on 2026-10-13; JUDGED and LIVE_OPTION_BOTS were absent.** Not verified on the machine and not changed here | **owner-ruling**, already tracked by the #3103 run: promote it or remove the line and recreate the lane (runbook section 9) | — |
| F7 | 5 `# type: ignore` in 3 app files; 217 `# noqa: F811`, 20 `# noqa: F401` and 38 `# type: ignore[...]` in the test files | 7 | hygiene | the app's five are narrowings the code has already guarded: `simulated_multi_leg_orders.py` asserts `record.option is not None` first; `option_admission.py:328` follows `_leg_refusal`, which refuses a quote without both sides and asserts both; `bot_option_run.py:421` is a `return-value` on a feed adapter. The tests' `F811` is the fixture re-import idiom, as in [#3148](https://github.com/tim1016/learn-ai/issues/3148) F4 | **leave-with-reason** — a refactor with no defect behind it | nothing |

### F1 — what the cut removes

| Symbol | Added | Evidence |
|---|---|---|
| `sqlite/option_entry.py::OptionEntryFacts.close_request` | `93c8e845` 2026-10-06; its last caller went with `75ef6bb7` "complete multi-leg closing" 2026-10-07 | no other mention; a close now builds its order from the structure with `closing_order` where it is made |
| `sqlite/multi_leg_evidence.py::fold_multi_leg_answer` | `6eee8c72` 2026-10-07 "recover multi-leg entry custody and outcomes" | never had a caller; `fold_multi_leg_order_evidence`, which it wraps, is the one `order_evidence.py` calls |
| `sqlite/option_owner_ending.py::NOT_HELD_FOR_AS_UNSETTLED` | `98af73f0` 2026-10-07 (#2913) | a reason code `"OPTION_OWNER_ENDING_NOT_HELD_FOR"` that no refusal raises; the string appears nowhere else (not in the ADR table, the Frontend or the tests) |
| `option_effects.py::OptionExitEvidence.for_other_contracts_than` | `cfc7ecc6` 2026-10-07; last caller went with `75ef6bb7` | no other mention |
| `sqlite/option_events.py::_why_a_real_account_cannot_end` parameter `conn` | #2913 | `ruff ARG001`; one caller, which keeps `conn` for its own reads |

## Pointers, not findings

- The sweep defect found by [#3148](https://github.com/tim1016/learn-ai/issues/3148) F1 (`ReconciliationSweep.run` overflows after 1,024 failed passes) and the unsupervised background taps [#3152](https://github.com/tim1016/learn-ai/issues/3152) is checking are not in this scope. Nothing here is a background loop that can end unnoticed: the held-quote round, the books read and the search worker each record a fault, and `stop()` cancels them (`held_option_quotes.py:529-537`, `option_search.py:446-503`).
- `live_shadow` in `clerk/sqlite/runtime.py` (F2) belongs to [#3149](https://github.com/tim1016/learn-ai/issues/3149).
- [PRD #3095](https://github.com/tim1016/learn-ai/issues/3095) is open and [#3103](https://github.com/tim1016/learn-ai/issues/3103) awaits the owner: the judged comparison and the real-account several-leg order are partial by design, not defects.
- `docs/options-companion-format.md` L90, L98, L200 name `_select_strikes`, `expiry_mode`, `max_dte` and `_norm_pdf`, which do not resolve. It is the options-labs ticket's document: [#3155](https://github.com/tim1016/learn-ai/issues/3155).
- `docs/architecture/engine-authority-map.md` L53-54, L71 name four stale paths and symbols; [#3148](https://github.com/tim1016/learn-ai/issues/3148) F7 already listed them.

## Cleanups that could not be completed

- **F2** waits on [#3149](https://github.com/tim1016/learn-ai/issues/3149): the parameter's call sites are in `clerk/sqlite/runtime.py`.
- **F3** is not delete-only: the five symbols are the observation points of tests that prove behaviour no other test proves.

## Lenses with nothing to fix

- **Lens 4, time and numbers.** Every temporal hit is prose (`expiry_date.isoformat()` in refusal text) or boundary-converted. `option_model_inputs.py` parses Polygon's `ex_dividend_date` into a `date` and converts it with `session_open_ms_utc` before it reaches the model (`DividendOn(ex_ms=…)`, L332); `kept_option_quotes.py::_session_closes` walks days through `session_close_ms_utc`, the canonical calendar. No `get_calendar`, no `09:30` or `16:00`, no fixed offset. The 12 files that carry a formula have a `Validated against:` line naming a test that exists and ran; none says `NONE`. The files that only carry numbers (`option_order_evidence.py`, `simulated_option_orders.py` fill views) cast to `float` at the stock-shaped order row, which is that row's existing type. Tests that compare floats use `pytest.approx(…, abs=1e-9, rel=0)`.
- **Lens 5, boundaries.** The coordinator client sets a read timeout and maps `httpx.TimeoutException` to a named refusal (`option_model_inputs_client.py:111-126`); the rate and dividend shelf evicts its oldest entry (`option_model_inputs.py:503-510`) and has a bound per lookup; the kept-quote file compacts above `COMPACT_ABOVE_ROWS` and is tested to stop growing (`test_half_an_hour_of_hand_overs_for_fifty_contracts_leaves_a_small_file_that_stops_growing`); the held-quote round, books read and per-account writes are cancelled by `stop()`, and the search lanes have `close()` plus a module-level `close_option_searches()`. Boundary input to the preview is checked by `option_preview_out_of_range.py` with `tests/broker/v2panel/test_option_structure_preview_route.py`.
- **Lens 7, docs and drift.** `docs/known-gaps.md` has no entry that names this scope, so none is closed or missing. The ADR 0076 table's `_WRONG_SIDE`, `_HELD_ON_BOTH_SIDES`, `_MORE_CONTRACTS_THAN_HELD`, `_CASH_NOT_WHOLE_CENTS`, `_UNPAIRED` and `_DISAGREES` are suffix shorthand; the full codes exist in `app/options/event_allocation.py` and `option_events.py`.
- **Lens 6, test health (beyond F4).** All 1,053 test functions are new since 10-01. By AST shape none fails the bar: no `assert True`, no identical bodies of three statements or more, and no tombstone. The one `hasattr` pair (`test_several_legs_books.py`) is a type contract, not a retired-feature check ("code that still assumes one leg fails loudly: the type has neither property"). The 13 tests with no `assert` statement either assert through a helper (`_refused`, `_refused_as_expired`, `_expired_and_unsold`) or are state-branch checks that the call does not raise (`test_a_live_shadow_takes_an_option_plan_whatever_the_setting_says`, `test_a_dry_run_registers_a_spread_bot`). Seven tests compare an exact literal of 70 characters or more: five are served documents in `test_option_event_screens.py`, and two pin what a stock enter and a stock exit wrote on master in `test_option_entry_leaves_stock_unchanged.py`, a compatibility check on the custody chain, which is what they are for. One test is marked `slow` (`test_kept_option_quotes.py:252`); the slowest unmarked in-scope test runs 2.3 s. The whole scope runs in 65 s locally with no skips.

## Limits of this read

- The 1,053 tests were judged by name, by AST shape and by reading the 20 or so that a shape or a finding pointed at. The rest were not read line by line.
- Symbol search matches identifiers by name across the repo, so a common method name can hide a dead method behind an unrelated caller. That biases toward keeping. It also cannot see a call made by `getattr` with a built string; no such dispatch appears in the 40 files.
- No Postgres-gated test is in this scope. The neighbour run is not exhaustive: `tests/services/bot_runner` has other files that touch option bots, and CI runs them.
- F4 is a risk read from the code, not an observed failure; no load repro was run.
- F6 reads the state of `paper.env` from the #3103 session's step log (`~/.fleet-recovery/option-qualification-20261013/step-log.md`), not from the file or the container. The env file was not opened.
- The `live_shadow` chain (F2) was traced by grep; `authority_kind` has other readers and is untouched.
