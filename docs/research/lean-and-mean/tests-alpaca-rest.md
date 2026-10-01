# Kill list: Alpaca adapter, profile and clerk (non-SQLite) tests

Ticket #2720, map #2700. This is a plan; nothing is deleted here.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` on 2026-09-30). The map was charted at `87b8e261`.
- **Area:** `PythonDataService/tests/broker/alpaca/*.py`, `profile/`, and `clerk/*.py`. That is 60 test files plus fixture modules and conftests, about 21K lines and 829 test functions. `clerk/sqlite/` is out of scope (#2717–#2719).
- **Skipped as already owned.** Some tests are already listed by #2701 (`dead-alpaca-clerk`) and #2702 (`dead-broker-rest`). They do not appear below. The ones in this area are:
  - the `AccountPin` / `pin_observed_account` cases in `profile/test_account_verification.py`;
  - the `credential_slot_available` cases in `profile/test_credentials.py`;
  - the `not_observed` / `on_selection` parametrize entries in `profile/test_secret_containment.py`;
  - the `RECOVERY_BAND_ALLOWANCE_MULTIPLE` assert in `test_marketable_limit.py`.

Paths below are relative to `PythonDataService/tests/broker/alpaca/`.

## Method

1. An AST pass pulled every test's name, length, docstring and assert lines. I read all 829 against the bar.
2. I opened the body of each candidate. I also opened the double it runs against, to tell a real outcome from a fake's own answer.
3. For each duplicate I name the stronger test that survives. Before a test counts as a duplicate, that survivor's assertions must cover the killed test's assertions.
4. Every killed name was searched across `app/`, `docs/`, `scripts/` and the fixtures. This catches provenance blocks and notes that cite a test by name. Hits are listed under Hazards.

Kinds: (1) trivial, (2) copy or doc pinning, (3) duplicate, (4) mock theater, (5) retired.

## Kill list

### Adapter and client

| Item | Kind | Evidence |
|---|---|---|
| `test_adapter_helpers.py::test_now_ms_is_epoch_millis` | 1 | Asserts only `isinstance(value, int)` and `value > 1_600_000_000_000`. Any clock passes. |
| `test_adapter_helpers.py::test_to_float_parses_decimal_string` | 3 | `to_float("1000.50") == 1000.50`. The golden mapping tests already parse every decimal-string field through it: `test_adapter_account.py::test_from_alpaca_account_maps_every_field` and `test_adapter_positions.py::test_from_alpaca_position_maps_long`. |
| `test_adapter_helpers.py::test_to_str_returns_the_vendor_text_unchanged` | 1 | `to_str("SPY", field="symbol") == "SPY"`, an identity check. The refusal cases next to it (`test_to_str_refuses_a_value_that_is_not_text`) stay. |
| `test_adapter_helpers.py::test_optional_helpers_pass_through_none` | 3 | Asserts `opt_*(None) is None`. `test_adapter_positions.py::test_missing_optional_fields_become_none` proves the same pass-through on a real payload, and `test_adapter_orders.py::test_open_order_has_no_events_and_nullable_prices` does too. |
| `test_adapter_account.py::test_observed_at_defaults_to_now` | 1 | The only assert is `observed_at_ms > 1_600_000_000_000`. |
| `test_adapter_trade_updates.py::test_event_names_match_documented_set` | 2 | Pins a literal subset of the `ALPACA_TRADE_UPDATE_EVENTS` vocabulary. The mapping outcome for each kind is proved by the per-event tests (`test_new_event_maps_with_no_fill_price` … `test_rejected_maps`), by `test_all_fixture_frames_map`, and by `test_trade_updates.py::test_a_gap_reconciled_replacement_keeps_its_replacement_links` (`replaced`). |
| `test_client.py::test_get_account_returns_raw_payload` | 4 | `_FakeAlpaca.get_account` returns a dict and the test asserts the same dict comes back. It is a pass-through over the fake. |
| `test_client.py::test_list_positions_returns_raw_list` | 4 | The same shape: the fake's list is asserted back. |
| `test_client.py::test_get_clock_returns_raw` | 4 | The same shape: `{"is_open": True}` in, `{"is_open": True}` out. |
| `test_schema_drift.py::test_asset_class_alias_is_recognized` | 3 | It tests the test file's own `_known_names` helper. `test_captured_payload_has_no_schema_drift[assets]` reads `assets.json`, which carries the raw `class` key, so it fails if the alias is not recognized. |

### Trade-updates consumer: attribution through the test's own sink

`_EvidenceSink` (`test_trade_updates.py:274`) is a test double. It decides ownership itself (`owned = client_order_id in self.owned_refs`, `:299`). It also raises the hold and refuses `submit` on its own (`_on_hold`, `:320`). The three tests below assert those answers back. Real attribution, the unexplained-order hold and the refused entry live in the SQLite clerk.

| Item | Kind | Evidence |
|---|---|---|
| `test_trade_updates.py::test_owned_client_order_id_journals_order_event` | 4 | `event.owned is True` and `event.order_ref == owned_ref` are the fake's allowlist answer. The real path is proved by `clerk/test_trade_evidence.py::test_sqlite_websocket_fill_records_exact_execution_and_separate_ack`. |
| `test_trade_updates.py::test_foreign_client_order_id_journals_unexplained_and_counts` | 4 | The hold (`clerk.is_on_hold()`) and the refused submit (`BrokerSubmissionHeld`) are both raised by `_EvidenceSink`. The counter assert only echoes what the fake returned. The real outcome is proved by `clerk/test_trade_evidence.py::test_sqlite_unexplained_trade_update_records_external_order_without_a_bot_fill` and `::test_unexplained_hold_refresh_retains_its_broker_event_and_names_its_episode`. |
| `test_trade_updates.py::test_absent_client_order_id_journals_unexplained` | 4 | `""` is not in the fake's `owned_refs`, so the fake answers "unexplained", and that answer is asserted back. The SQLite tests above prove the same thing with a real repository. |

### Profile

| Item | Kind | Evidence |
|---|---|---|
| `profile/test_credentials.py::test_the_allowlist_is_exactly_the_two_owner_decided_slots` | 1 | Two literal equalities: `CREDENTIAL_SLOTS == (DEFAULT, LIVE)` and `(DEFAULT, LIVE) == ("default", "live")`. That the allowlist is closed is proved by `test_a_slot_off_the_allowlist_never_reaches_an_environment_lookup`. |
| `profile/test_credentials.py::test_the_availability_shape_carries_a_label_and_a_boolean_and_nothing_else` | 3 | Pins the dataclass field set. The outcome it guards, no secret in the availability payload, is proved by `profile/test_secret_containment.py::test_the_availability_payload_carries_no_secret_or_variable_name`. |
| `profile/test_runtime_context.py::test_the_resolved_context_is_immutable` | 1 | Assigning to a frozen dataclass raises `AttributeError`. That is the language working, not the resolver. |
| `profile/test_settings_injection.py::test_a_broker_without_injected_settings_still_defers_to_the_singleton` | 3 | It is the paper case of `test_capabilities.py::test_capabilities_select_by_settings_mode`, which runs the same singleton monkeypatch for both paper and live. |

### Config, fault injection and clerk

| Item | Kind | Evidence |
|---|---|---|
| `test_config.py::test_live_configuration_holds_no_retired_session_count` | 1 | Asserts two retired names are absent from `AlpacaSettings.model_fields`, which is true by construction. The sealed-record side that still matters, `test_a_sealed_records_retired_session_counts_keep_their_domain`, stays. |
| `test_fault_injection_live.py` (whole file, 1 test) | 3 | `test_fault_injection.py::test_flag_on_but_not_paper_fails_closed` covers the same case: flag on, settings not paper. It asserts `injection_permitted() is False` and also that `arm()` raises `FaultInjectionRefused`, so it is the stronger test. |
| `clerk/test_ceremony.py::test_the_bounds_are_the_ones_cutover_shipped` | 1 | `(DEFAULT_CONFIRMATION_TTL_MS, MAX_CONFIRMATION_TTL_MS) == (120_000, 300_000)`, a literal pin. The refusal at the bound is proved by `test_the_ttl_must_be_a_whole_millisecond_count_inside_the_bound` and `test_expiry_is_inclusive_of_the_last_admissible_millisecond`. |
| `clerk/test_live_arming.py::test_the_mode_disagreement_code_is_the_adapters_own` | 1 | One assert that a constant equals a constant. |
| `clerk/test_live_envelope.py::test_the_admission_reason_codes_are_the_envelope_refusals` | 2 | Pins the literal set of five reason codes and that it is a `frozenset`. Each refusal outcome is proved at the ENTER seam: `clerk/sqlite/test_envelope_admission.py` (#2717) and `clerk/test_shadow_envelope_runtime.py`. |
| `clerk/test_fifo_pnl.py::test_reversal` | 3 | The same scenario and numbers as `::test_golden_fixture_reversal` (BUY 100@10, SELL 150@12, realized 200, 50 short open). The golden test asserts the same fields against the fixture with `atol=1e-9`. |
| `clerk/test_fifo_pnl.py::test_multi_day` | 3 | The same scenario as `::test_golden_fixture_overnight_position` (BUY 100@10, BUY 50@11, SELL 80@13 the next day, realized 240). The golden test asserts more fields. |
| `clerk/test_fifo_pnl.py::test_marks_complete_partial_coverage_returns_none` | 3 | The same scenario as `::test_golden_fixture_incomplete_marks` (SPY 100@10 marked 11, AAPL 50@200 unmarked, so `open_pnl` is None). |
| `clerk/test_fifo_pnl.py::test_no_normalize_money_call_names_a_fifo_float_view_attribute` | 3 | Its own docstring calls it a "tripwire, not the guarantee". It also names the guarantee: `money.display_cents` refuses a float (`clerk/test_money.py::test_display_cents_refuses_anything_but_an_exact_decimal`), plus the rendered-string regressions in `test_panel_projection.py`, `test_fee_attribution_view.py` and `test_simulated_account.py` (#2723 / #2725). See hazard 2. |

## What the cuts orphan

- **`test_trade_updates.py`.** With the three attribution tests gone, check whether anything still reads `_EvidenceSink.is_on_hold`, `.unexplained_order_count` or the `_on_hold` branch of its `submit`. Drop whatever no longer has a reader. `BrokerOrderRequest` and `_warm` stay, because 17 surviving consumer tests call `_warm`.
- **`test_adapter_helpers.py`.** The `now_ms` import, and any other import left unused.
- **`clerk/test_fifo_pnl.py`.** `_FIFO_FLOAT_VIEWS` and the `ast` / `Path` imports, if only the tripwire used them. Nothing under `tests/fixtures/golden/broker-v2-fifo-pnl/` changes, and no golden test is touched.
- **`test_fault_injection_live.py`.** Its import of `clerk/live_arming_fixtures.py::live_settings` goes. The helper stays, because `clerk/sqlite/test_cutover_cli.py` also uses it.
- **`PythonDataService/scripts/pr_shard_durations.json`.** Stale ids for every deleted test, including the whole `test_fault_injection_live.py` file. Regenerate it, or confirm the sharder ignores unknown ids (as #2701 and #2702 also note).
- No conftest fixture loses its last user. Project-scope ruff finds the leftover imports.

## Hazards the cutting PR must carry

1. **Re-check at your own SHA.** The list ages, so re-run each row's evidence before deleting. That means the body, the survivor's assertions and the citation search.
2. **The FIFO tripwire is cited by name in two docs.**
   - `docs/math-sources-of-truth.md:206` (the FIFO row's "Validated against" cell)
   - `docs/references/broker-v2-fifo-pnl.md:82`

   Edit both in the same commit, and run the docs link-contract tests (`pytest tests/contracts`). The rendered-string regressions those docs name as the real guarantee must survive their own tickets (#2723, #2725). If either ticket cuts them, keep the tripwire.
3. **The FIFO golden tests are sacred.** Only the three hand-written duplicates go. Do not touch the fixture, its `attribution.md` or the tolerance. `test_duplicate_event_key_is_idempotent` is **not** a duplicate of `test_golden_fixture_duplicate_delivery` and stays: the golden one de-duplicates upstream, while the hand-written one proves that `compute_fifo_pnl` itself de-duplicates.
4. **The trade-updates attribution cuts rest on SQLite tests.** Before deleting the three tests, confirm that the named survivors still exist at the cutting SHA and still prove the real outcome: an unexplained order is recorded, holds the account, and the next entry is refused. Check them in `clerk/test_trade_evidence.py` and the `clerk/sqlite/` hold tests (#2717–#2719). If the clerk SQLite tickets cut those, keep these.
5. **Money-path run.** Run `tests/broker/alpaca/`, `tests/broker/v2panel/` and `tests/contracts/` after the cut.

## Considered and kept

- **Call-order tests where the order is the behavior.** These stay because the order they check is a money-path outcome:
  - `clerk/test_active_runtime_taps.py`: the sweep is *not* started eagerly.
  - `clerk/test_authority_reconnect.py`, every `root.names()` sequence: the authority is retired before a refusal is installed, so lease and fencing hold.
  - `test_trade_updates.py::test_reconnect_opens_the_next_source_before_gap_reconcile`: subscribe first, so no fill is missed.
  - `::test_each_frame_captured_verbatim_before_handler_runs`: evidence is durable before it is derived.
- **`clerk/test_shadow_broker.py::test_the_trade_port_holds_no_vendor_client`.** It is a source scan, but it is the structural guard that a shadow world riding a live account holds no client able to send a real order. On the money path, keep when unsure.
- **Drift guards between declarations that must stay equal:**
  - `test_live_envelope.py::test_the_envelope_reads_exactly_the_settings_live_mode_requires`
  - `profile/test_runtime_context.py::test_the_envelope_field_names_are_the_dataclass_field_names`
  - `::test_the_paper_pair_is_the_configuration_modules_pair`
  - `::test_the_whole_number_fields_are_the_dataclasses_integer_fields`
  - `::test_the_integer_predicate_agrees_with_the_sealed_record_validator` (the CLAUDE.md #5 parity test)
  - `test_config.py::test_the_envelope_domains_agree_with_the_settings_that_declare_them`

  Each one fails when one copy changes without the other, and that would make a valid live boot fail to start or a seal refuse.
- **`clerk/test_live_arming.py::test_the_recorded_refusal_codes_are_their_own_names_and_the_set_is_closed`** and **`clerk/test_live_arming_ledger.py::test_production_ledger_has_no_mutating_arming_interface`.** Both pin retired arming codes and the read-only ledger. #2701 keeps those codes so old receipts still replay. Keep when unsure.
- **`clerk/test_ceremony.py::test_the_digest_of_a_fixed_payload_is_pinned_to_a_literal`.** The hashed bytes are a contract with tokens operators already hold.
- **`#2702`'s two open questions, answered.** Both symbols are test-only seams that surviving tests read, so by ☆ they stay:
  - `CaptureJournal.records_written`, read in `test_capture_hook.py` and `test_portfolio_history_endpoint.py`, which prove capture routing.
  - `BrokerOrderRequest`, used through `_warm` by 17 surviving consumer tests.

## Pointers outside this area

- **#2711 (one-off docs).** These plans quote tests by name, including the file this list cuts: `docs/superpowers/plans/2026-09-09-live-slice-7-gate-remeaning.md:3639-3708` (`test_fault_injection_live.py`), `2026-09-08-live-slice-5-risk-envelope.md:198` and `2026-09-09-live-slice-6-arming-ceremony.md:133`. They are sediment if #2711 cuts them. If not, they go stale.
- **#2714 / #2741 (reference notes, kept-doc pass).** `docs/references/broker-v2-fifo-pnl.md:82` and `docs/math-sources-of-truth.md:206` name the tripwire (hazard 2).
- **#2723 / #2725 (v2 panel and service tests).** `test_panel_projection.py`, `test_fee_attribution_view.py` and `test_simulated_account.py` hold the rendered-string regressions this list relies on. They must not be cut as duplicates of the FIFO tripwire.
- **#2717–#2719 (clerk SQLite tests).** The unexplained-order hold and refusal tests this list names as survivors (hazard 4).

## Not reviewed

- **Long scenario tests read by name and first assert only.** In `clerk/test_trade_evidence.py`, `clerk/test_authority_reconnect.py`, `clerk/test_manual_order_routes.py`, `test_market_liveness.py` and the consumer half of `test_trade_updates.py` (tests over 40 lines), I judged each test from its name, docstring and first asserts, not line by line. A duplicate *between* two of those long scenarios would not have been seen.
- **Cross-area duplicates.** Overlap was checked only one layer up and down where a candidate pointed there: adapter against clerk, and FIFO against the v2 panel. Adapter tests were not checked against `tests/broker/fleet/`, `tests/services/` or `tests/routers/`. Those tickets (#2721, #2724, #2725, #2730) should check whether their tests duplicate this area's.
- **Fixture modules.** `clerk/activation_fixtures.py`, `live_authority_fixtures.py`, `live_envelope_fixtures.py`, `live_arming_fixtures.py` and both `conftest.py` files were not audited for helpers that nothing uses. They were checked only for orphans these cuts create.
- **Parametrize cases.** Whether individual cases inside a parametrized test repeat one another was not checked.
