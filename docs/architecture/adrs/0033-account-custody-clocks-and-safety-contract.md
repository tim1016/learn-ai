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

## Strategy A0 cutover amendment (2026-07-27, #1245)

Normal paper-strategy submission now uses `submit_custody_v2`; the runner
returns only after the Clerk has durably recorded A0. It must not translate
that receipt into an `IbkrOrderAck`, a broker order id, or `Submitted` state.
The production Clerk starts this capability with bounded account-wide entry
and risk-reducing capacities (64 and 32 respectively); those are backpressure
limits, not evidence that an individual strategy may have that many entries.

For each `(account_id, strategy_instance_id)`, the Clerk admits at most one
nonterminal normal entry. The journal-derived check survives originator death
and bot restart, and returns `CLERK_ASYNC_ENTRY_PENDING` before creating a
second A0 row. The dedicated risk-reducing lane does not consume that normal
entry slot. An entry slot becomes available only after an economic terminal
callback or an A0-only expiry before broker submission.

The bot keeps a small in-memory projection of its own A0 admissions so it can
wait for Clerk callback facts instead of assuming a broker call succeeded. It
is explicitly not an account ledger. On a same-process callback it restores
the original strategy metadata; after a bot restart it accepts only an exact
namespace-matching fill and derives the minimum metadata required to project
that fill. The canonical Clerk journal and the bot callback WAL remain the
durable records. A duplicate local strategy entry while the earlier one is in
custody is a nonterminal suppression, not a broker-uncertainty halt.

## Shadow account-epoch amendment (2026-07-27, #1246)

The account-rooted Clerk persists `account_epoch = (clerk_boot_id, epoch_seq)`
beside its generation and lease. It is a proof horizon, not a broker-session
boolean: each Clerk journal fact may retain its immutable `origin_epoch`, its
later `observed_epoch`, and the `reconciliation_id` under which it was
recorded. Older journal rows deliberately retain these fields as unknown;
their sequence number or write time cannot be backfilled as epoch proof.

Socket loss, IBKR 1100, 1101, 1102, critical callback-stream silence, a failed
active poll while nonterminal work exists, Clerk replacement, and generation
fencing produce idempotent account-epoch receipts. The first trigger advances
the proof sequence and sets an observable shadow `would_block_reason`; later
distinct triggers attach evidence to that same invalid epoch. 1101 requires a
full later reconciliation while 1102 is an incremental candidate, but both
are new-epoch candidates and neither restores entry authority by itself.

Slice 4 does not block a write. The Clerk health RPC exposes the current state
and proposed block reason so an operator can compare shadow disagreement with
the unchanged production admission path. A stale Clerk generation is forbidden
from advancing or rewriting epoch state; a successor boot records its own
epoch and starts invalid until the later enforcement/reconciliation slice mints
an explicitly clean or adopted successor.
