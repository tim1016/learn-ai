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

## Asynchronous custody shadow amendment (2026-07-27, #1244)

The Clerk may expose `submit_custody_v2` only when constructed with explicit,
validated entry and risk-reducing capacities. It returns the durable A0 receipt
and a per-intent custody fold; broker work is performed by a Clerk-owned
background worker. Existing `submit` callers retain their synchronous receipt
#2 behavior until the separately proven strategy cutover.

Queue saturation is a typed refusal before A0, not a caller timeout. The
versioned read API exposes lane depth and capacity, and replaying the same
identity returns its one durable lifecycle rather than placing again. On Clerk
restart, A0-only entry work is durably expired before submit; risk-reducing
work is retained as action-required until an explicit policy resumes it. Work
at A1 or later keeps ADR 0008's uncertainty and reconciliation rules.

The ten-second `submit_custody_v2` value is a **caller response deadline**, not
a claim that a server can preempt an in-progress filesystem fsync. A response
deadline may therefore end with the identity-scoped
`ACCOUNT_CLERK_UNAVAILABLE:TIMEOUT` outcome while the durable A0 result is
still unknown to the caller. That is explicitly neither acceptance nor
refusal: the originator must read `read_custody_v2` (or replay the exact same
identity) before deciding its next action. This avoids the false safety claim
that a cancelled async task can stop a thread already writing a journal row.

An async lane is carried by the same durable `recorded` A0 row (and its inbox
crash-replay row), never by a follow-up queue receipt. The bounded queue is
only a process-local scheduler. Until durable A1, the generic reconciler must
not probe or retry async work; it also treats expiry, policy-hold, and
recovery-action-required rows as non-retryable. A worker blocked by a dynamic
post-A0 intake fence writes a durable `submission_hold` status rather than
leaving a request indefinitely queued. The optional originator notification is
bounded, coalesced, and never authoritative over the durable read API.
