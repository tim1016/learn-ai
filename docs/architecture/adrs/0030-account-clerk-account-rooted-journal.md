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
