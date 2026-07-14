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
