# Broker configuration integration — Package G

Integration baseline: `037ffe12`. Sources: PRs #2018–#2025, with Package F
through `e11b8824`. The configuration stack, sealed-envelope prerequisite and
Clerk-volume ceremony documentation are consolidated into one delivery.
The main checkout's uncommitted Paper/Shadow rehearsal changes are excluded
at the owner's request. No running account, credential file, container, active
Clerk volume, execution lease or production configuration was changed.

## Acceptance evidence

All automated broker observations use fake ports and temporary Clerk/artifact
roots. Paths below are relative to `PythonDataService/tests/` unless stated;
bare `test_*.py` names are in `broker_configuration/`.

| Acceptance scenario | Evidence |
| --- | --- |
| Save, rename, clone, reload, archive, multiple profiles | `broker_configuration/test_profiles_service.py`, `test_routes.py`; two-connection metadata races in `test_store_concurrency.py` |
| Exact selected slot; missing/rotated/wrong credentials; forged owner/actor/slot | `broker/alpaca/profile/`, `broker_configuration/test_alpaca_seams.py`, `test_secret_absence.py`, `test_routes.py` |
| Save/stage/Apply never grants trading authority | `test_selection_service.py`, `test_worker_binding.py`, `test_cutover_rehearsal.py`; authority remains separately activated and armed |
| Rename preserves identity; effective policy change requires re-arm | `test_envelope_type_fidelity.py`; the A→B→A regression keeps invalidation durable while historical arming bytes remain unchanged |
| Existing envelope hashes and stored numeric types | `test_envelope_type_fidelity.py`, `broker/alpaca/profile/test_runtime_context.py` |
| Active obligations block a changed profile/revision, including same-account changes | `test_prior_obligations.py`, `test_worker_binding.py`, `test_binding_decision.py` |
| Refused Apply or staged-only crash recovers last-effective | `test_worker_binding.py`, `test_retired_environment_gate.py`; retired settings refuse the change without removing the prior binding |
| Concurrent selection/startup and interrupted startup | `test_store_concurrency.py`, `test_worker_lifecycle.py`; separate connections/contexts, real competing processes and crash lock release |
| Authoritative account pin before custody; Shadow identity | `broker/alpaca/clerk/test_active_authority.py`, `test_worker_lifecycle.py` |
| Empty/unreadable configuration boots unavailable; no post-cutover env fallback | `test_worker_binding.py`, `test_retired_environment_gate.py`, `contracts/test_alpaca_configuration_source.py` |
| Paper, Shadow, Live, Dry Run isolation and existing recovery | `broker/alpaca/`, `broker/v2panel/`, `services/` targeted suites; the excluded shared-feed rehearsal is outside this integration |
| Loss hold and exit pricing remain sealed until re-arm | `broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py`, `test_runtime_program_leg.py`, `services/test_alpaca_live_envelope.py` |
| CLI effective-only arming, before/after diff, atomic handover | `scripts/test_manage_alpaca_arming_effective_revision.py`, `scripts/test_manage_alpaca_arming.py`, `test_selection_service.py` |
| Import twice, preview, cutover and rollback | `test_legacy_import.py`, `test_cutover_rehearsal.py`, `scripts/test_manage_broker_configuration.py` |
| Profile/selection survival and cold reopen | `test_schema_migration.py`, `test_clerk_dir_parity.py`, `test_envelope_type_fidelity.py`; `compose.yaml` keeps the Clerk volume external |
| UI persistence, staged/effective status, actionable read/write failures | Frontend configuration specs and parent account-card spec; rejected resource reads render Retry, not an Angular resource exception |
| Generated contracts and documentation | OpenAPI `--check`, TypeScript `codegen:check`, ADR-status and documentation-contract checks |

Volume destruction was not exercised against the operator's deployment.
The persistence claim is a combination of the external-volume declaration and
temporary-database reopen/cold-start tests, not a production reset receipt.

## Validation receipt

- Python combined consumer suites: **4,966 passed, 10 skipped**, 168 seconds.
  Command from `PythonDataService/`: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker_configuration tests/broker/alpaca tests/broker/v2panel tests/contracts tests/routers tests/scripts tests/services -q`.
  The integration worktree used the existing host virtual environment while
  importing source and tests from the integration checkout.
- Frontend: **127 tests passed** across the 11 changed specs and two consuming
  account-card/desk specs. Each was selected by exact file path with `ng test --watch=false`.
- Production Angular build and full Frontend ESLint: passed.
- Project-scope Python ruff, OpenAPI `--check`, TypeScript `codegen:check`, ADR
  status, documentation-contract and whitespace checks: passed.
- New failure regressions were demonstrated before their fixes. The combined
  suite also caught a simulated binding refusal leaking between tests; the
  lifecycle fixture now resets that process state before and after each test.

## Independent review outcomes

The standards review found resource-error rendering and non-atomic metadata
updates; both have failing-before regression tests and fixes. The spec review
found rejected-Apply recovery, same-account preflight, startup/account-pin
fencing and reversible arming disagreement gaps; each is covered by the tests
above. Follow-up review checked the CLI handover race, unreadable arming cleanup
and refusal precedence. No historical arming/custody/activation schema changed.

## GitHub review follow-up

The review of commit `476bc9d8` raised six findings. The follow-up corrects:

- Missing-versus-unavailable CLI configuration detection, including broken
  database and parent-directory links and permission/I/O failures.
- Terminal bot lifecycle handling during prior-account obligation checks;
  outstanding custody and unreadable lifecycle evidence still refuse switching.
- Atomic import publication, including owner creation, adoption, storage-fidelity
  verification, events and optional staging, with rollback on any refusal.
- Exact historical revision staging from the UI without an implicit Apply.
- Distinct restoration events and the location of live credential placeholders.

Regression cases reproduced the behavioral failures before correction. Targeted
follow-up validation: **548 Python tests and 40 frontend tests passed**; production
build, full frontend lint, project-scope Python lint and whitespace checks passed.
Independent checks found no remaining blocker within these review findings.
Commit `500f488a` passed all GitHub checks. The subsequent Paper-reset work records
the owner's final scope resolution and its additional evidence below.

## Release and cutover checklist

1. ADR 0060 is Accepted (2026-09-11); all five owner choices are resolved.
2. Use the [credential-slot and cutover runbook](../references/alpaca-credential-slots.md).
   Inject the permitted secret pairs and retain the external Clerk volume.
3. Schedule the installation cutover with its worker stopped, no open account
   obligations and no armed instance. Back up configuration securely outside Git.
4. Run the import preview in the data-plane image against the Clerk volume;
   inspect the exact values and confirmation token, then apply the import.
5. Verify/approve the observed account in the configuration page, stage and
   press Apply, remove the seven retired user-setting lines, then restart.
6. Confirm effective profile, exact account and authority world. A profile
   grants no activation or arming; perform those separate ceremonies only when
   intentionally authorized. A refused Apply requires a fresh Apply request.
7. For rollback, stop the worker and reconcile obligations first. Restore the
   reviewed prior code/deployment settings; retain profile records and never
   roll back custody databases, activation generations or arming ledgers.

The owner accepted installation-local nicknames, immediate UI renames without
Apply, and preserving unrelated Live profiles during a Paper reset. ADR 0060
records all three. No operational reset or production cutover has been performed.

## Paper reset completion — 2026-09-11

`broker_configuration/test_developer_reset.py` covers target-only configuration
cleanup, retained Live/other-account revisions and immutable events, mixed profile
history, worker/handover exclusion, separate-process and connection exclusion,
transaction rollback, inaccessible databases, idempotent retry and never-reused
revision numbers. The integrated commit-failure test publishes custody reset,
refuses the stale authority while configuration rolls back, and completes cleanup
on retry without publishing another reset.

`broker/alpaca/clerk/sqlite/test_dev_reset.py` and the canonical bot repository
suite cover current and retired Paper bots, retained Live/Shadow/other-account
bots, preserved legacy IBKR history, and refusal on malformed canonical evidence.
Target-account activation evidence and historical Live configuration pins prevent
a Paper selection from authorizing reset of a stopped Live account. Empty saved
configuration after reset cannot reactivate environment bootstrap.

The original missing cleanup/locking and revision-number reuse were reproduced
before correction. All new tests use temporary artifacts and fake broker evidence.

Final reset validation: **4,316 Python consumer tests passed** across
`tests/broker_configuration`, `tests/broker/alpaca`, `tests/services`,
`tests/scripts` and `tests/contracts` (144 seconds). The two affected Frontend
specs passed **30 tests**. Production build, full frontend lint, project-scope
Python lint, OpenAPI contract, ADR/documentation guards and whitespace checks
passed. The build reports the existing unrelated `NG8102` template diagnostic
in `strategy-lab-run-stats.component.html`.

The final activation-mode compatibility cases exercise a real test cutover,
two ordinary authority resets and then developer reset: Paper succeeds, Live
refuses, and a corrupted predecessor refuses. Each inherited mode is derived
from verified same-account activation ancestry; no sealed record is changed.
The final focused activation/reset/cutover run passed **113 tests**, including
missing and duplicate predecessor refusals.
