# ADR 0033: Account custody clocks and the safety composition contract

- **Date:** 2026-07-27
- **Status:** Accepted
- **Context:** Account-safe operations PRD; issues #1243–#1257; the AMD connectivity incident
- **Amends:** ADR 0008 and ADR 0030

## Decision

The Account Clerk is the sole durable custodian of an admitted account intent.
The originator is immutable provenance; the manager is the one fenced actor
allowed to make the next broker write. Custody is not an economic claim: A0
means a Clerk journal receipt is fsynced, while A1, A2, and A3 mean,
respectively, broker-write start, broker identity correlation, and terminal
economic resolution.

This first slice instruments the existing synchronous RPC only. It does not
return early at A0 or change any caller deadline. The later asynchronous path
is disabled by default and may be enabled only after its separate admission,
restart, and idempotency proof is accepted.

Every custody timeline retains three non-interchangeable clocks:

- broker/source event time when IBKR supplied it;
- local arrival time when the Clerk observed a callback; and
- durable record time when the Clerk journal wrote the receipt.

Journal `seq` is a serialization cursor, never event time or causal proof.
Absent clocks remain unknown. A late callback can add an older source fact but
cannot rewrite an earlier local-arrival, durable-phase, or terminal fact.

The current retry rule in ADR 0008 remains unchanged: a retry uses the same
`intent_id` and `order_ref` only after broker absence is proven. No
supersession identity is introduced by this program.

The account epoch is owned by the accepting Clerk generation. On its planned
activation, loss of broker proof or Clerk fencing invalidates entry authority;
a fresh CLEAN or ADOPTED reconciliation is required before a new epoch allows
entry writes. The closed safety verdict vocabulary is `CLEAN`, `RECONCILING`,
`SUSPENDED`, and `CONTAMINATED`; current behavior remains unchanged until its
individual shadow and enforcement slices land.

The Clerk journal remains the sole order, exposure, custody, reconciliation,
and epoch authority. Daemon, supervisor, data plane, and bots write only
producer-local operational logs. The future `AccountSafetySnapshot` composes
those existing authorities for operator read surfaces; it does not become a
new raw-fact store or allow .NET or Angular to calculate safety.

## Considered options

- **One timeout and one timestamp:** rejected because it hides queue, durable
  acceptance, broker, and callback failure domains.
- **Treat journal sequence as causal time:** rejected because file serialization
  cannot establish broker causality or callback arrival order.
- **A new account-safety ledger:** rejected because it would compete with the
  Clerk journal just as custody needs one authority.
- **New retry identities:** rejected until an explicit ADR 0008 migration and
  dual-read plan are accepted.

## Consequences

Transaction detail is the existing read surface for custody timing. It must
show unknown phase facts honestly and derive durations only from compatible
same-clock facts. Future queue, epoch, UI, action, and producer-log slices use
these terms and must not silently alter the synchronous baseline.
