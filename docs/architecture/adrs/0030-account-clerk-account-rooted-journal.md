# ADR 0030: Account Clerk Authority Is Account-Rooted and Journal-Canonical

- **Date:** 2026-07-14
- **Status:** Accepted
- **Context:** PRD #1015, issue #1016

This is PRD #1015's documentation authority for decisions 2 (account-rooted
authority), 7 (journal-canonical ledger), 8 (identity-scoped fencing and
retirement), and 15 (the ADR/deletion ledger).

## Decision

Each broker account has one Account Clerk authority. Its durable intake inbox,
serialized journal, future lease, and future clerk generation live below that
account's artifact root, rather than in a daemon or a runner artifact tree.
The daemon supervises a clerk but does not become a broker-write authority.

The Clerk lifecycle follows the approved connected broker account, not bot
process presence. Broker connect ensures the account service before Account
Truth is accepted; stopping the last bot leaves it in healthy standby so
observation and automatic flat-account reconciliation continue. Only an
explicit broker-account detach releases the Clerk. A Clerk exit while the
account remains attached is replaced even when the bot fleet is empty. This
deliberately rejects the earlier bot-scoped reap policy, which made every
normal idle account lose observation proof and appear fenced after five
minutes.

The supervising daemon also owns the container-to-host connection boundary.
When the data plane is configured with `host.containers.internal` or
`host.docker.internal`, a host-native Clerk or bot child receives
`127.0.0.1`. Container aliases are valid coordinates from inside the data
plane but are not assumed to resolve in the host process namespace.

The existing `AccountOwnerSubmitIntent` remains the intake identity on the
wire: trace id, account id, strategy instance id, run id, namespace, intent
id, and namespace-qualified order ref are not translated or re-minted. Clerk
intake validates the identity against the latest **ACTIVE** account-instance
binding. A stale run, retired binding, or namespace mismatch is rejected only
for that identity; it cannot fence a sibling identity.

The durable inbox is replayed into one append-only account journal. A
`recorded` receipt is returned only after its journal row is fsynced, and
always before any broker contact. The journal is the authoritative managed
ledger for the subsequent submit, acknowledgement, fill, reconciliation, and
per-namespace exposure slices.

Account-level contamination, account-truth managed claims, launch gates, and
monitor meters will consume the journal ledger after the required shadow and
drift-parity period. Per-run live-state sidecars remain bot-local working
state; they are not a second managed-exposure authority.

## Rationale

The 2026-07-14 paper validation showed that advancing a shared generation for
every runner start makes the newest bot invalidate all existing siblings. An
account is the shared resource; a bot process is not. Serializing durable
account intake while keeping identity validation local to each submitting bot
preserves concurrent bots without allowing a stale run to place an order.

## Deletion ledger

The following machinery is retired by the clerk cutover slices, not left as a
dormant fallback:

- Bot-start advancement of `accounts/<account>/owner_generation.json`.
- Per-runner global owner-generation checks that reject healthy siblings.
- Runner-held write-capable IBKR submit lanes and broker callbacks.
- Per-run sidecar sums as the managed-exposure source for account verdicts.
- The cohort launcher: its HTTP routes, scheduler/resumption loop, receipt-authorized
  admission bypass, evidence sampler, cockpit dialog, monitor, and generated client
  contract. Historical account-event fields remain read-only evidence only; every new
  start takes the complete interactive gate chain and proceeds one bot at a time.

Until the cutover slices land, these mechanisms remain documented legacy
behavior. There is no fallback from a disabled clerk to multi-writer submit.

## Consequences

Issue #1016 establishes only receipt #1: durable inbox, serial journal,
identity-scoped rejection, and replay. Clerk process lifecycle and lease
fencing follow in #1018; the broker drain and receipt #2 follow in #1020; the
journal ledger verdict cutover follows the reconciler and shadow-parity work.

## Account Observation Lease migration boundary (2026-07-15)

The Account Observation Lease is now fenced by the accepting Account Clerk
generation plus its matching, unexpired `RUNNING` Clerk lease, not the retired
per-runner owner generation. A generation file alone is insufficient because
it survives a crashed or reaped Clerk. Lease schema v2 and shadow-comparison
schema v2 make that boundary explicit. Owner-keyed v1 lease artifacts fail
closed, and owner-keyed comparison rows are classified as legacy evidence that
cannot authorize promotion.

One process-wide setting, `IBKR_ACCOUNT_GATE_AUTHORITY`, selects the account
proof consumed by both Start and submit. Its default remains `account_truth`.
The `observation_lease` branch is deliberately dormant until three distinct
canonical NYSE paper sessions produce valid v2 comparisons with no
lease-weaker result and the Clerk-restart HITL smoke passes. Per-bot selection
is forbidden because it would create split account authority.

The obsolete, zero-production-caller owner-generation advance writer and
startup recording method are removed. Remaining owner-generation reads are
still active compatibility/safety consumers and are not deleted under a false
"dead code" claim; each must move to a characterized Clerk-backed replacement
before its artifact contract can be retired.

## Track B deletion ledger and authority truth-up (2026-07-15)

Issue #1058 was closed on 2026-07-15 before this ledger and the operator
surface cutover were complete. This section records the honest post-closure
state. It is the deletion ledger for every `owner_generation`,
`AccountOwnerGeneration`, `AccountOwner`, and per-runner AccountOwner
reference under `PythonDataService/app` as of this change; a single source
reference may serve more than one category only when its data model is used by
both the Clerk and a historic replay.

| Exact source coverage | Classification | Track B disposition |
| --- | --- | --- |
| Bot-start `accounts/<account>/owner_generation.json` advancement; `advance_account_owner_generation`; `AccountOwner.record_accepting_generation` | **DEAD** | Already deleted before this change. The source tree has zero production references, so there is no further deletion to claim. |
| `engine/live/account_owner.py::AccountOwnerSubmitIntent`; `engine/live/account_clerk*.py`; `schemas/journal_cures.py`; Clerk journal/reconciler/operation models | **CLERK-BACKED-LEGACY-NAME** | The name is the compatibility wire identity for Clerk intake and journal rows. It does not select a per-runner broker writer. Defer the coordinated wire/model rename; do not churn it in Track B. |
| `engine/live/run.py` Clerk-generation providers, `owner_generation` callback fields, and `AccountOwnerSubmitResult` adaptation; `engine/live/live_engine.py`; `engine/live/live_portfolio.py`; `engine/live/reconciliation_orchestrator.py` | **CLERK-BACKED-LEGACY-NAME** | Normal strategy submit and namespace cancellation use Clerk RPC. These parameters retain the old vocabulary while carrying the active Clerk generation. Rename only with the compatibility model above. |
| `broker/ibkr/orders.py`; `routers/broker.py`; `engine/live/account_owner_fence.py` | **CLERK-BACKED-LEGACY-NAME** | The grant/fence spelling is compatibility debt at the broker boundary. It remains fail-closed and is not evidence of a runner-owned normal submit path. A separate rename must preserve the existing fail-closed fence. |
| `engine/live/account_artifacts.py` legacy `AccountOwnerGeneration` read/write model and `owner_generation.json`; `services/bot_lifecycle_projection.py`; `services/lifecycle_projection_{store,replay,schema}.py`; `schemas/lifecycle_projection.py`; `schemas/bot_events.py` | **LEGACY-READER** | These consume or preserve historic owner-keyed artifacts/events and PostgreSQL projection rows. They are not current write authority. Keep dual-read/history until a versioned artifact and database migration retires the historic record. |
| `services/bot_lifecycle_receipt_copy.py` historical receipt labels and `services/account_reconciliation.py`'s generic account-condition helper | **LEGACY-READER** / **not a runner owner** | Historic raw receipt tokens remain exact audit values. The account-condition helper's `owner` is ordinary domain ownership, not `AccountOwner` broker authority. Neither is renamed in this track. |
| `routers/live_instances.py::_resolve_account_owner_surface`; `services/live_instance_surface_assembler.py`; `services/operator_surface.py`; `services/operator_trader_guidance.py`; `services/operator_blockage_ladder.py`; `services/bot_lifecycle_{chart,receipts}.py`; Frontend operator-surface types and fixtures | **LEGACY-READER — MIGRATED** | Done in Track B. The read-only response is now schema v2 `account_clerk`, sourced from the Clerk generation plus matching active lease. It no longer reads or returns the legacy owner artifact. Historic account-event tokens remain opaque audit data. |
| `engine/live/run.py::emergency_flatten` direct broker cancel/place calls, `read_account_owner_generation`, and `account_owner_write_grant` | **SAFETY-LANE** | Do not modify in Track B. This is the separately invoked emergency path, not the normal Clerk RPC path. Its direct IBKR session and lock/fence coordination require a dedicated design and regression suite. |
| `engine/live/run.py` recovery fallback `account_owner.run_broker_write` | **SAFETY-LANE** | Do not delete under a caller-count heuristic. It participates in recovery handling and must be characterized with the emergency/recovery architecture before removal. |

The Track B operator-surface migration is display-only: it feeds
readiness/guidance/chart evidence and does not change Start, normal submit,
`IBKR_ACCOUNT_GATE_AUTHORITY`, or a broker write boundary. A response is
healthy only when the Clerk generation is `accepting` **and** its matching
`RUNNING` lease is unexpired. The regression test writes a conflicting legacy
owner artifact and proves the response returns only the active Clerk evidence.

### Emergency flatten decision proposal — not implemented

**Decision proposed for the #1058 follow-up:** make emergency flatten an
elevated Account Clerk operation. The Clerk retains the OS advisory lock and
the only write-capable IBKR session, validates an explicit whole-account
emergency actor/confirmation, and writes a journal/audit receipt before broker
effects. The operation must depend only on account identity, Clerk state, and
broker evidence; it must not depend on a LiveEngine instance or its run ledger.

Alternatives considered:

1. **Recommended: route through the active Clerk.** Preserves the single
   broker session and the lock invariant. When the Clerk cannot be reached,
   fail closed and require Clerk/daemon recovery before any write.
2. **Independent emergency process that coordinates the existing lock.** Only
   viable with an explicit Clerk-death/reap and fencing-takeover protocol. A
   plain lock acquisition is insufficient because it can race a live Clerk or
   broker session. This is more complex than the Clerk operation and is not
   recommended without a distinct incident-mode ADR.
3. **Allow direct emergency writes only when Clerk appears down.** Rejected.
   “Appears down” is not a fencing proof and would recreate two-write-client
   risk precisely when evidence is least reliable.

This proposal leaves the current safety lane unchanged. Its implementation
needs an ADR-approved takeover/recovery policy, a Clerk-unavailable operator
workflow, journal receipt semantics, and failure-injection tests for lock,
session, and crash races.

## Clerk S2 lifecycle-honesty boundary (2026-07-21)

Issue #1155 establishes the daily-exit floor without creating a second
broker writer or enabling overnight trading.

- **Clock out is a Clerk-lane operation.** `CLOCK_OUT` makes the runner pause
  new strategy work, flatten through its existing Clerk submit/cancel boundary,
  and poll a broker-primary position snapshot. A cached snapshot cannot prove
  flatness. Only an empty fresh snapshot, a durably persisted `STOPPED` latch,
  and the completed command acknowledgement make `clock_out_receipt.json`
  eligible for `CLOCKED_OUT_FLAT` / `OFF_DUTY` projection.
- **Every dead child gets a fact, never an invented clean exit.** The daemon
  maps a complete clock-out receipt to `CLOCKED_OUT_FLAT`; all other reaped,
  crashed, halted, and failed-launch paths record their specific terminal
  duty outcome. The cockpit renders that persisted outcome and reason rather
  than holding a dead child in optimistic `RUNNING` copy.
- **Carryover remains explicitly disabled.** Each bot lifecycle record now
  reserves `carryover_policy=FORBID`. No extended-hours or overnight behavior
  is activated by this field. A later policy must be Clerk-authored and opt in
  deliberately.
- **The STOPPED latch is one-way until Resume.** Start only reads the latch and
  rejects a stopped bot; it cannot clear it as a side effect.
- **Retirement is logically atomic across artifact roots.** A per-bot
  `retirement_transition.json` is fsynced as `PENDING` before retirement
  successor rows are written to every discovered account registry. While
  pending, both Start and Clerk intake reject the identity. The durable
  per-instance operation fence spans deploy/reopen, Start's ACTIVE
  binding/spawn, Clerk's broker-write boundary, and terminal reaping, so a
  retirement cannot interleave after an earlier admissibility check. Reaping
  also refuses to overwrite a different active run. The daemon replays pending
  transitions at boot, verifies every registry fold is `RETIRED`, then persists
  lifecycle `RETIRED` with no roster membership or active duty and records
  `COMMITTED`. This fence makes a crash between individual file writes
  fail closed instead of leaving a usable stale binding or partial roster.

The relevant regression seams are
`test_live_engine_command_channel.py`, `test_host_daemon.py`,
`test_bot_daily_lifecycle.py`, and `test_live_instances.py`.

## Clerk S3 daemon-command idempotency boundary (2026-07-21)

Issue #1156 makes the host daemon's Start, Stop, and emergency-flatten routes
the durable idempotency boundary. Every command carries an opaque
`idempotency_key`; the daemon records its command name, canonical semantic
payload hash, account scope, and exactly one response before it returns. A
matching duplicate replays the recorded outcome without repeating process or
broker work. Reusing a key for a different command or payload is a durable,
operator-visible `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_COMMAND` conflict.

The staged rollout is intentionally account-scoped. The host's
`LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS` setting is empty by
default, which preserves one durable outcome and logs matching duplicates in
shadow mode. Naming a specific account turns replay enforcement on; removing
it reverses that decision without erasing the forensic record. If a daemon
dies after claiming a key but before persisting an outcome, enforcement returns
`IDEMPOTENCY_OUTCOME_UNKNOWN` and never re-executes the potentially effected
command. Operator-facing responses preserve the opaque key and mark a replay
with `idempotency_replayed=true`.

## Issue #1044 callback-stream hardening traceability

| Requirement | Verification |
| --- | --- |
| Start after broker connect; always stop in shutdown | `test_clerk_process_acquires_lock_before_broker_connect_and_releases_after_disconnect` |
| Stream task death closes normal submit intake, writes an alarm, drains health, and exits for supervision | `test_stream_task_death_alarms_rejects_normal_submits_and_exits_unhealthy` |
| Attribution is installed before broker await and rebuilt after restart | `test_market_fill_before_ack_is_attributed_before_broker_await`; `test_restart_rebuilds_durable_callback_attribution_index` |
| Reconciler-adopted and retried intents retain attribution | `test_reconciler_adopt_and_retry_restore_callback_attribution` |
| Callback is journal-fsynced before relay and duplicate callbacks have one semantic journal row | `test_clerk_relays_callbacks_only_to_the_originating_namespace`; `test_journal_exposure_survives_bot_crash_and_deduplicates_execution` |
| Unattributed callbacks persist without a guessed namespace, create a consumed account alarm, and block starts | `test_unattributed_broker_callback_is_persisted_and_blocks_new_account_starts`; `test_account_projection_includes_unattributed_callbacks` |
| Journal fsync does not block the callback event loop | `test_callback_fsync_is_offloaded_without_allowing_relay_before_record` |
