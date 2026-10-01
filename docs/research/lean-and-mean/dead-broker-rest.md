# Kill list: dead code in the fleet, IBKR, broker configuration and migration layers

Ticket #2702, map #2700. Plan only; nothing is deleted here.

- **Read at:** `87b8e261021ec673c2e7c80448c0973bccd45378` (`origin/master`, the same SHA the map was charted at).
- **Area:** `PythonDataService/app/broker/{fleet,ibkr,contract,capture}/`, `broker/alpaca/*.py`, `broker/alpaca/profile/`, `broker/fleet_composition.py`, `app/broker_configuration/`, `app/installation_migration/`, `app/operator/`. That is 113 files, about 39K lines. `alpaca/clerk/` (#2701) and `broker/v2panel` are excluded.

## Method

1. **Module reachability.** I followed the import closure out from the real entry points: `app/main.py`, every script under `PythonDataService/scripts/` and `scripts/`, `lean_sidecar`, and module paths named in compose files, Dockerfiles, shell scripts or CI. A module that no live module imports is dead.
2. **Symbol reachability.** I ran a fixpoint over every prod Python file in the repo, with each top-level function, class, constant and method treated as a unit. A unit counts as live only if something reaches it:
   - a route or other non-wrapper decorator,
   - module-level code,
   - another live unit that names it, resolved through the real `import` and re-export chain,
   - an identifier in a non-Python prod file (compose, CI, TS, C#, JSON).

   Tests, docs, `pr_shard_durations.json` and the generated OpenAPI snapshot never count as callers. Methods resolve by name only, which keeps some dead methods alive (see Not reviewed).
3. **Second opinion.** A throwaway `vulture` run (scratch venv, not added to the repo) produced candidates.
4. **Caller search.** Every row below has a `git grep -w` over the whole repo, excluding docs, at the stated SHA. The evidence column quotes that search.

**Test impact** says what happens to tests when the row goes:
- *delete*: the only tests are tests of this dead symbol (kind 5), so they go with it.
- *repoint*: a live-behaviour test only uses this symbol to look at state, and its call site moves to the live equivalent named in the row.
- *none*: no test touches it.

## Kill list

### A. Whole package: `app/operator/incidents/` (poisoned-safety-halt incident writer)

| Path | Kind | Evidence | Test impact |
|---|---|---|---|
| `PythonDataService/app/operator/incidents/__init__.py` | package | No prod module imports `app.operator.incidents`. The only importers are `tests/operator/test_incident_store.py:10` and `tests/operator/test_safety_halt_notices.py:6`. | delete |
| `PythonDataService/app/operator/incidents/store.py` (`IncidentStore`, `INCIDENTS_DIR`, `list_unresolved`) | module | Imported only by `safety_halt_notices.py:17` (dead) and `tests/operator/test_incident_store.py`. | delete `tests/operator/test_incident_store.py` |
| `PythonDataService/app/operator/incidents/safety_halt_notices.py` (`safety_halt_incident_id`, `build_safety_halt_incident`, `PoisonedIncidentRecordResult`, `poison_and_record_incident`) | module | No prod importer. `poison_and_record_incident` (`:114`) has no caller anywhere, tests included. It imports `app.engine.live.halt`, which is itself unreachable (pointer to #2704). | delete `tests/operator/test_safety_halt_notices.py` |
| `PythonDataService/app/operator/notices/schema.py:233` `OperatorIncident` | class | Its only users are the two dead modules above. `OperatorNotice` and `OperatorNoticeAction` stay, because `app/schemas/live_runs.py` uses them. | the `OperatorIncident` cases in `tests/operator/test_notice_schema.py`, if any |
| `PythonDataService/app/operator/notices/schema.py:120` `RuntimeFreshnessReasonCode` (+ re-export `operator/notices/__init__.py:9,18`) | type alias | No prod reader. The only use is `tests/operator/test_notice_schema.py:142` (`get_literal_args`). | delete that test case |

### B. IBKR wire models nothing reads (the bot-control era, retired by ADR 0038 / #1583)

None of these is on the live bar feed or the Gateway plumbing. `bars.py`, `bar_models.py`, `minute_assembler.py`, `client.py`, `auto_reconnect_monitor.py`, `health.py`, `keepalive.py`, `connect_log_budget.py`, `market_liveness.py` and `market_subscription.py` are all untouched. The rows remove unused symbols from `models.py`, `contracts.py` and `capability.py` only.

| Path | Kind | Evidence | Test impact |
|---|---|---|---|
| `PythonDataService/app/broker/ibkr/models.py:240` `IbkrPosition` | class | Referenced only inside `models.py`. No importer anywhere, tests included. | none |
| `PythonDataService/app/broker/ibkr/models.py:300` `IbkrPositionsSnapshot` | class | Same: only `models.py`. | none |
| `PythonDataService/app/broker/ibkr/models.py:103` `SecType` | type alias | Only `models.py`; the other mention is a retirement audit doc. | none |
| `PythonDataService/app/broker/ibkr/models.py:431` `OrderTimeInForce` | type alias | Only `models.py`. | none |
| `PythonDataService/app/broker/ibkr/models.py:446` `OrderEventType` | type alias | Only `models.py`, as a field type of `IbkrOrderEvent`. | none |
| `PythonDataService/app/broker/ibkr/contracts.py:179` `build_option_contract` | function | Its only prod mention is a comment at `routers/broker.py:415`. The only call is `tests/broker/ibkr/test_option_search.py`. The live option chain qualifies contracts through other helpers in `contracts.py`. | delete its cases in `test_option_search.py` |
| `PythonDataService/app/broker/ibkr/contracts.py:39` `_NY_OFFSET` | constant | No reference anywhere. | none |
| `PythonDataService/app/broker/ibkr/capability.py:36` `_ALL_SESSIONS` | constant | No reference anywhere. | none |

**Cascade, gated on #2704.** `engine/live/account_clerk_journal.py` and `account_clerk_journal_models.py` (the IBKR-era account-clerk journal) are imported by no prod module. Only `tests/_helpers/legacy_ibkr_artifacts.py` and two tests touch them. When #2704 cuts them, these become dead too:
- `models.py:210` `IbkrTradeSnapshot`
- `:224` `IbkrTradeEvidence`
- `:429` `OrderAction`
- `:430` `OrderType`
- `:432` `OrderStatus`
- `:449` `IbkrOrderEvent`
- `:530` `IbkrOrderAck`

Their only non-`models.py` user is `account_clerk_journal_models.py:18,222,415,534`. Cut them in the same PR as #2704's journal cut, or right after it.

### C. Alpaca profile: the account-pin helper nothing uses

| Path | Kind | Evidence | Test impact |
|---|---|---|---|
| `PythonDataService/app/broker/alpaca/profile/account_verification.py:116` `AccountPin` | class | Prod mentions are only its own module and the `profile/__init__.py:38,81` re-export. The live pin path is `BrokerConfigurationService.pin_account` → `ProfilesStore.write_account_pin`, which never builds an `AccountPin`. | delete `AccountPin` cases in `tests/broker/alpaca/profile/test_account_verification.py` (~`:243–322`) |
| `PythonDataService/app/broker/alpaca/profile/account_verification.py:194` `pin_observed_account` | function | Same: only its own module, the `__init__` re-export, and the test above. | delete (same test file) |
| `PythonDataService/app/broker/alpaca/profile/errors.py:150` `AccountVerificationFailed.not_observed` | classmethod | Called only by `pin_observed_account` (`account_verification.py:217`) and `tests/.../test_secret_containment.py:50`. The class itself stays. | drop that parametrize entry |
| `PythonDataService/app/broker/alpaca/profile/errors.py:256` `AccountPinMismatch.on_selection` | classmethod | Called only by `pin_observed_account` (`:220`) and `test_secret_containment.py:57`. Check at cut time whether anything else still uses `AccountPinMismatch`. | drop that parametrize entry |
| `PythonDataService/app/broker/alpaca/profile/credentials.py:170` `credential_slot_available` | function | Prod mentions are only its own module and `profile/__init__.py:52,94`. `legacy_import.py:181,395` has a *field* with the same name, not a call. The only calls are `tests/broker/alpaca/profile/test_credentials.py:125,163`. | delete those cases |
| `PythonDataService/app/broker_configuration/errors.py:96` `CredentialSlotUnavailable` | class | Never raised or imported. Every raise and test import uses the *profile* class of the same name (`profile/errors.py:80`; `test_alpaca_seams.py:16` imports from `app.broker.alpaca.profile`). | none |

### D. Fleet: identity predicates and a store reader nothing calls

| Path | Kind | Evidence | Test impact |
|---|---|---|---|
| `PythonDataService/app/broker/fleet/identity.py:71,76,81,86,91` `is_volume_id`, `is_worker_key`, `is_agent_instance_id`, `is_correlation_id`, `is_service_token` | functions | The only callers are `tests/broker/fleet/test_identity_and_errors.py:76–83`. `is_clerk_id` is live and stays. | delete those assertions |
| `PythonDataService/app/broker/fleet/identity.py:17–21` `_VOLUME_ID`, `_WORKER_KEY`, `_AGENT_INSTANCE_ID`, `_CORRELATION_ID`, `_SERVICE_TOKEN` | regex constants | Used only by the five predicates above. | none |
| `PythonDataService/app/broker/fleet/store.py:530` `FleetRegistryStore.list_sessions` | method | No caller anywhere, tests included. | none |
| `PythonDataService/app/broker/fleet/service.py:994` `FleetControlService.read_lane_quiet_confirmation` and `store.py:1112` `read_latest_lane_quiet_confirmation` | methods | The service method is the only prod caller of the store method, and the service method itself is called only from tests (`test_lane_quiet_transport.py` ×5, `test_lane_quiet_confirmation.py:395,410,589`, `test_schema_migration.py:697`). The live reader is `store.read_lane_quiet_confirmation_on` (`service.py:1057`). | repoint about 9 test sites to `read_lane_quiet_confirmation_on`; this is money-path fencing, so keep the outcome the tests assert |

### E. Broker configuration and migration: test-only observers

| Path | Kind | Evidence | Test impact |
|---|---|---|---|
| `PythonDataService/app/broker_configuration/legacy_import.py:198` `ImportPlan.writes_anything` | property | The only read is `tests/broker_configuration/test_legacy_import.py:142`. | delete that assertion |
| `PythonDataService/app/broker/capture/journal.py:167` `CaptureJournal.records_written` | property | The only reads are tests: `test_capture_hook.py:116,128`, `test_portfolio_history_endpoint.py:106`, `test_journal.py:54,156,175`. `failure_count` (`:160`) is read in prod and stays. | repoint to counting journal lines, or delete the assertions (#2720 judges) |

## What the cuts orphan

- **Re-export lists.** Remove the names from:
  - `app/broker/alpaca/profile/__init__.py` (`AccountPin`, `pin_observed_account`, `credential_slot_available`; imports `:38,41,52` and `__all__` `:81,94,97`)
  - `account_verification.py:269,272` (`__all__`)
  - `credentials.py:238`
  - `broker_configuration/errors.py:161`
  - `operator/notices/__init__.py:9,18`
  - `fleet/identity.py:97–102`
- **Test helpers.** `tests/operator/_helpers.py`: check whether anything is left once the two incident tests go. `tests/_helpers/legacy_ibkr_artifacts.py` goes with #2704's journal cut, not here.
- **Engine import.** `app.engine.live.halt` loses its last importer when `operator/incidents/` goes. It is already unreachable (#2704).
- **Docs** (pointers, not rows):
  - `docs/references/alpaca-credential-slots.md` names `AccountPin` and `pin_observed_account` (#2714).
  - `docs/audits/ibkr-control-plane-decommission-*` and `docs/audits/clerk-lineage-reachability-2026-08-17.md` name the IBKR order models (#2711).
- **Shard ledger.** `PythonDataService/scripts/pr_shard_durations.json` keeps node IDs of the deleted tests. Regenerate it or drop the stale keys (#2707/#2716).

## Hazards the cutting PR must carry

1. **Re-check at your own SHA.** This list ages. Before deleting, re-run `git grep -n -w <name> -- ':!docs'` for every row.
2. **IBKR feed.** Do not touch any other symbol in `app/broker/ibkr/`. CLAUDE.md's provider boundary and ADR 0062 protect the feed and its Gateway plumbing, and `IBKR_BROKER_ENABLED` is load-bearing. The section B rows are leaf symbols with no caller on that path. Once the cut is made, run `pytest tests/broker/ibkr tests/marketdata tests/structural`.
3. **Structural tests that name modules by string.**
   - `tests/structural/test_ibkr_feed_boundary.py:297` already lists the non-existent `app.operator.incidents.watchdog_notices`.
   - `tests/contracts/test_ibkr_order_actuation_retirement.py:163` reads `engine/live/account_clerk_journal.py` by path.

   Both break or go stale when the package or journal goes. Fix them in the same PR (#2731 / #2730 own the tests themselves).
4. **Money path.** The section D store and service readers sit in fleet fencing code. The repoint must keep each test's assertion about the *recorded* confirmation; only the reader changes. If a repoint is not a one-line swap, keep the method.
5. **Section B cascade order.** Cut the seven IBKR order/trade models only after #2704 removes `engine/live/account_clerk_journal*.py`. Otherwise the import at `account_clerk_journal_models.py:18` breaks.
6. **OpenAPI.** None of these symbols is in `contracts/openapi/python-data-service.openapi.json` (checked for the IBKR models), so no contract regeneration should be needed. Confirm with the contract CI job.

## Considered and kept

- **Test-reset seams.** These prod helpers are called only by tests:
  - `reset_active_alpaca_binding_for_testing` (`alpaca/active_binding.py:174`)
  - `reset_alpaca_settings_for_testing` (`alpaca/config.py:178`)
  - `reset_fault_injection_for_testing` (`alpaca/fault_injection.py:247`)
  - `reset_market_liveness_consumer_for_testing` (`alpaca/market_liveness.py:504`)
  - `reset_trade_updates_consumer_for_testing` (`alpaca/trade_updates.py:1047`)
  - `reset_broker_configuration_service_for_testing` (`broker_configuration/runtime.py:106`)
  - `ConnectLogBudget.reset_for_testing` (`ibkr/connect_log_budget.py:126`)

  Each one isolates tests of *live* singletons. Cutting them forces rewriting surviving tests, which the map rules out. Rule question below.
- **Test-only data that a guard test checks against:**
  - `fleet/schema_migrations.py:18,198` `SCHEMA_DDL_V1` (the v1 DDL the migration tests upgrade from; the live code is `SCHEMA_MIGRATIONS`)
  - `installation_migration/contents.py:86` `EXCLUDED_BIND_MOUNTS`
  - `broker_configuration/legacy_environment.py:123` `NEVER_RETIRED_SETTINGS`
  - `fleet/refusal_vocabulary.py:51,69` `_subclass_closure` / `_MINTED_OUTSIDE_THE_CLOSURE` (the drift guard for the Frontend refusal-copy snapshot)

  Each lives or dies with its guard test. The test tickets (#2721, #2722) judge the guard. If the guard stays, the constant stays or moves into it.
- **Test-only observers on live objects:**
  - `BoundWorker.from_profile` (`worker_binding.py:132`; 4 test reads)
  - `FleetRegistryStore.list_assignments_for_clerk` (`store.py:649`)
  - `FleetRegistryStore.list_forced_correlations` (`store.py:1128`; drain-ceremony tests read obligations through it)

  These are money-path tests that observe custody and drain state, so I kept them.
- **Reached only through scripts.** These are live operator ceremonies with runbooks or CI steps:
  - `installation_migration/` ← `scripts/migrate_installation.py`. Runbook `docs/runbooks/migrate-installation.md`, shipped in #2270/#2273.
  - `broker_configuration/cli_binding.py`, `paper_reset.py` ← the `manage_alpaca_*` recovery CLIs.
  - `fleet/refusal_vocabulary.py` ← `.github/workflows/ci.yml:317` snapshot gate, which the Frontend copy map consumes.
- **Cutover tools that are not provably finished.**
  - `broker_configuration/legacy_import.py` (645 lines) and `scripts/manage_broker_configuration.py` describe themselves as "the one-time cutover tool for ADR 0060". But `worker_binding._bootstrap_from_environment` (`:321`) still boots a worker from `.env` when no profile exists, so the env path is live code and the import tool is still the documented way off it.
  - `fleet/compatibility_retirement.py` (637 lines) is evidence-gated alias retirement. It is reached from `lane_runtime.py:31,274,588` and the `manage_broker_fleet.py` ceremony.

  Neither is dead by reachability. Both become cuttable once their ceremony has run on every installation, which a static read cannot see.

## Pointers outside this area

- **#2704 (engine).** All seven are imported by no prod module:
  - `engine/live/account_clerk_journal.py`
  - `account_clerk_journal_models.py`
  - `account_effect_models.py`
  - `account_epoch.py`
  - `account_owner.py`
  - `halt.py`
  - `intent_events.py`

  The import-closure run lists about 70 unreachable `app/` modules in total. The engine/live ones unlock section B's cascade.
- **#2703 (services).** Imported by no prod module:
  - `services/mutation_rung_receipts.py` (uses `OperatorNotice`)
  - `services/paper_live_*`
  - `services/clerk_transaction_projection_schema.py`
- **#2701 (Alpaca clerk).** `broker/alpaca/clerk/sqlite/repository_boundary.py` is imported by no prod module.
- **#2706 / #2708 (routes, Frontend).** The IBKR capability probe's whatIf order preview (`ibkr/capability.py:260` `_preview_outside_rth_limit_order`) feeds a `tradeable` field on the live capability route. It describes *IBKR order* tradeability, a retired mode. Check whether any UI renders it; if not, the preview branch and the field are a retired-mode branch.
- **#2731 (structural tests).** `tests/structural/test_ibkr_feed_boundary.py:297` names the non-existent module `app.operator.incidents.watchdog_notices`.

## Not reviewed

- **Method-level dead code behind shared names.** Methods resolve by name across the whole repo, so a dead method that shares a name with any live method anywhere (`close`, `read`, `snapshot`, `record`, …) shows as live. That is most of `fleet/service.py` (3,042 lines), `fleet/store.py` (1,368), `fleet/routing.py` (1,028), `broker_configuration/service.py` (1,136) and `store.py` (726). They need a typed, call-graph pass, which I did not do.
- **Unread config keys.** I did not audit settings fields one by one: `IbkrSettings`, `AlpacaSettings`, `app/config.py` broker keys, `LegacyEnvironmentPresence`. Fields read through `settings.<name>` resolve by name, so an unread key could hide.
- **Retired-mode branches inside live functions.** For example, `if mode == ibkr:` style arms inside `fleet/routing.py` or `lane_runtime.py`. Below-symbol branch analysis was not done. The three unreachable-code warnings vulture raised (`fleet/delivery.py:265`, `fleet/routing.py:129`, `alpaca/market_liveness.py:394`) are probably exhaustive-match defensive returns. I did not read them.
- `broker/contract/models.py:120` `BrokerOrderRequest`. It has no prod caller (the only uses are 4 sites in `tests/broker/alpaca/test_trade_updates.py`), but those tests feed it to a fake `submit`. I did not read them to decide whether they test a live path. Leave it to #2720, which can cut it if the tests go.
