# Alpaca SQLite Clerk invariant-to-test traceability

**Scope:** Issue #1395 implementation and ADR 0035 qualification evidence.

Paths below are relative to `PythonDataService/`. The committed smoke and full reports
record the exact selected campaigns, host context, PRAGMAs, fixture transaction mix,
latencies, file sizes, and query plans.

| Invariant / acceptance claim | Implementation authority | Focused proof |
|---|---|---|
| One authority per account; DB existence is not activation. | `app/broker/alpaca/clerk/active_authority.py`, `sqlite/activation.py` | `tests/broker/alpaca/clerk/test_active_authority.py`, `tests/contracts/test_alpaca_active_authority_wiring.py` |
| A valid activation is bound to account, generation, DB identity, broker-proof hash, and quarantine-manifest hash. Invalid/failed activated startup does not fall back to JSONL. | `sqlite/activation.py`, `active_authority.py` | `test_recovery_tooling.py::test_activation_requires_matching_database_and_referenced_artifacts`; active-authority failure tests |
| Strategy registration is durable before order capability; live ENTER/EXIT dispatches through SQLite only after activation. | `sqlite/runtime.py`, `services/bot_runner.py`, `sqlite/enter.py`, `sqlite/exit.py` | `test_runtime.py`, `test_bot_runner.py`, `test_active_authority.py` |
| Intent and mirror finalization precede broker contact. A disk-full prepare failure produces no transition or broker-eligible work. | `sqlite/repository.py`, `sqlite/mirror.py`, `sqlite/enter.py`, `sqlite/exit.py` | `test_enter.py::test_intent_commits_before_the_broker_is_ever_called`; `test_repository.py::test_disk_full_before_mirror_prepare_fails_closed_without_transition` |
| Duplicate, double-click, retry, lost response, and restart produce one economic intent and exact durable identity. | `sqlite/commands.py`, `sqlite/enter.py`, `sqlite/uncertainty.py` | duplicate/lost-response/restart tests in `test_enter.py` and `test_commands.py` |
| EXIT is cancel-first, folds intervening partial fills, proves siblings terminal, and reduces only proven remaining attributed quantity. | `sqlite/exit.py`, `sqlite/exit_resolution.py` | partial-fill/cancel race and lost-cancel-response tests in `test_exit.py` |
| Trade-update redelivery/out-of-order evidence is monotonic; a reconnect gap reconciles before admission reopens. | `clerk/trade_evidence.py`, `alpaca/trade_updates.py`, `sqlite/reconcile.py` | `test_trade_evidence.py`, out-of-order test in `test_enter.py`, `test_trade_updates.py::test_reconnect_gap_reconcile_pulls_missed_orders` |
| Current-state reads use materialized indexed folds; bounded timeline pages are operation-first and include source, Clerk-observation, and durable-record clocks. | `sqlite/projections.py`, `sqlite/projection_models.py`, schema indexes | `test_projections.py`, qualification `EXPLAIN QUERY PLAN` evidence |
| Recovery capability and execution share one policy; action tokens include only relevant durable facts. No generic clear/retry/blind flatten. | `sqlite/recovery_policy.py`, `sqlite/recovery_execution.py` | `test_recovery_policy.py`, router action tests, frontend action rendering tests |
| BOT uncertainty remains bot-scoped; unknown causes fail closed account-wide. Rebuild/reset appear only for authority failure. | `sqlite/recovery_policy.py` | scope, healthy/unhealthy capability, stale-token, and action-availability cases in `test_recovery_policy.py` |
| Online backup is WAL-safe, identity-bound, fully verified, fsync-published, and interruption cannot replace the last verified publication. | `sqlite/recovery.py::create_verified_backup` | `test_recovery_tooling.py::test_interrupted_backup_keeps_previous_verified_publication` |
| Restore accepts only a non-symlink direct bundle under this account's backup root, same current generation/DB identity, verified SHA/receipt, and current finalized mirror head; old DB files are preserved. | `sqlite/recovery.py::{verify_backup_bundle,restore_verified_backup}` | outside/symlink/tamper/older-snapshot restore tests in `test_recovery_tooling.py` |
| Mirror rebuild accepts only a contiguous finalized, hash-verified, account/generation/database-bound mirror and preserves the old DB. | `sqlite/mirror.py`, `sqlite/rebuild.py`, `sqlite/recovery.py::preserve_and_rebuild_from_mirror` | mirror tamper/gap/substitution tests in `test_repository.py` and `test_recovery_tooling.py` |
| Corrupt DB and corrupt uncheckpointed WAL fail closed on startup. | `sqlite/repository_lifecycle.py` plus bound mirror reconciliation | corrupt-page test in `test_repository.py`; `test_recovery_tooling.py::test_corrupt_wal_fails_closed_on_startup` |
| Reset requires fresh matching broker identity, finite flat positions, no open orders, and every governed bot stopped; it rotates generation and preserves old authority. | `sqlite/recovery.py::reset_authority` | exposure/open-order/nonfinite/preflight/success reset tests in `test_recovery_tooling.py` |
| Cutover plan is read-only; both plan/apply require a cleanly stopped checkpointed DB. Apply rechecks exact broker/roster/DB/legacy evidence, has no force option, quarantines legacy, and fsyncs activation. | `sqlite/cutover.py`, `scripts/manage_alpaca_sqlite_clerk.py` | all cases in `test_cutover.py`, including unchanged-file plan, live-WAL refusal, changed evidence, token expiry, activation/quarantine, and CLI restart resolution |
| REST bootstrap and versioned SSE project the active SQLite authority without frontend safety derivation; raw receipt codes use the shared label boundary. | SQLite router/schema, Broker V2 evidence service, `SurfaceHub`, Angular Broker V2 panel | `tests/routers/test_alpaca_clerk_sqlite.py`, `tests/broker/v2panel`, Angular timeline/service tests, generated-contract checks |
| Qualification covers 1/10/100 bots and 10k/100k/1M transitions with deterministic broker-free fixtures. | `sqlite/qualification.py`, `scripts/run_alpaca_sqlite_qualification.py` | `test_qualification.py`; `docs/audits/alpaca-sqlite-clerk-qualification-full.{json,md}` |

The scale loader is an offline batched hash-chained fixture builder, not a production
write-throughput claim. Capture latency is measured separately through real repository
registration commits. Human paper-account cutover and soak are intentionally excluded
and remain in #1383.
