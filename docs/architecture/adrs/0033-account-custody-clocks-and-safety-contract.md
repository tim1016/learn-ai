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
The production Clerk starts this capability with eight bounded account-wide
entry slots and **zero** asynchronous risk-reducing order slots. The broker
does not yet expose an externally reachable atomic reduce-only primitive, so
inventing a reserved lane would overstate the safety contract. Risk-reducing
Pause and Stop intents use their separately durable lifecycle path; async
broker order placement fails closed until that primitive exists.

For each `(account_id, strategy_instance_id)`, the Clerk admits at most one
nonterminal normal entry. The journal-derived check survives originator death
and bot restart, and returns `CLERK_ASYNC_ENTRY_PENDING` before creating a
second A0 row. An entry slot becomes available only after an economic terminal
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

## Suspended-account effect amendment (2026-07-27, #1249)

`risk_reducing` is a server-derived effect class, never a client flag. A
caller may name only an exact order identity for cancellation or an exact stock
position identity plus its observed signed quantity for closing. The Python
classifier derives `ENTRY`, `EXACT_CANCEL`, `EXACT_CLOSE`, `AMBIGUOUS`, or
`ACCOUNT_EMERGENCY` from the latest durable reconciliation projection and the
current Clerk epoch. Missing, stale, pre-epoch, symbol-only, reused, or changed
evidence is `AMBIGUOUS` and cannot reach a broker write.

When account safety is `SUSPENDED`, only server-proved exact effects can cross
the final Clerk fence. The public asynchronous order lane admits neither
`EXACT_CLOSE` nor `EXACT_CANCEL`: the latter remains internal until Slice 13
binds it to a snapshot/action envelope, and the former is explicitly blocked
as `BROKER_ATOMIC_REDUCE_ONLY_UNAVAILABLE`. Native IBKR stock orders do not
have an atomic reduce-only guard, so even a final position read could race a
late fill and flip through zero. The classifier continues to derive an exact
stock close for an operator-visible, machine-readable blocker, but it cannot
create A0 or reach IBKR until a broker-side guarded-close protocol exists.

For any future guarded close, the contract is already constrained to the
opposite side, no larger than the current signed position, and matching the
IBKR `con_id` in the execution spec; it cannot resolve a same-symbol contract.
A reconciliation receipt is eligible only when it is strictly later than the
current epoch observation: equal millisecond timestamps do not prove callback
ordering and fail closed. Every durable order spec must carry the immutable
intent's exact `order_ref`, so the receipt's attribution token is also the one
IBKR receives.

The internal exact-cancel primitive accepts both broker `order_id` and echoed
`order_ref`, and the adapter re-resolves that exact pair immediately before it
calls IBKR; a reused reference cannot cancel a sibling order. It is not yet an
operator-UI action: Slice 13 must bind it to the later snapshot/action envelope.
The legacy internal risk queue remains available while an account is clean, but
does not gain a suspension exception and still uses the ordinary epoch gate.

## Producer-log split and deletion ledger (2026-07-28, #1256)

`account_events.jsonl` is now read-only historical evidence.  It is no longer
a forward writer, shared sequence allocator, or cross-producer mutex.  Forward
operational observations are appended under
`accounts/<account>/producer_operational_logs/<producer>/<boot>.jsonl`; each
row has a schema version, producer boot ID, producer-local sequence,
idempotency identity, and distinct event/arrival/record clocks.  A repeated
receipt replay is idempotent; a receipt-less observation is deliberately a new
fact even when its payload and local test clock happen to match another one.

The Account Desk merges legacy and producer rows with a deterministic display
sort.  This establishes repeatable pagination only.  It does **not** claim a
global sequence, callback causality, broker-time ordering, or permission to
derive an order/exposure verdict.  A corrupt producer stream fails its selected
display scope honestly; it does not modify or invalidate the Clerk journal.

The one exception to a producer-only display merge is a deliberately direct,
read-only projection of a manual-order acknowledgement from
`clerk_journal.jsonl`.  The former `account_clerk_manual_order_acked` sidecar
write and the unattributed-callback sidecar mirror were removed.  Consequently,
the Account Desk can show an operator order receipt only when it is backed by
a canonical Clerk receipt; a producer record with identical-looking order
fields is intentionally non-authoritative and cannot produce a trader outcome
or operator receipt.

### Removed legacy writers

All former forward writers now enter the one compatibility shim in
`account_artifacts.py`, which routes to the indicated producer log.  No caller
appends to `account_events.jsonl` or allocates `account_events.seq`.

| Former writer locations | Event family | Producer log | Authority after split |
| --- | --- | --- | --- |
| `account_registry.py`, `host_daemon.py` | binding, retirement, daemon lifecycle | `daemon` | Binding ledger / host daemon artifacts; history is display-only |
| `account_clerk.py`, `account_clerk_reconciler.py`, `account_epoch.py`, `account_epoch_observer.py`, `account_owner.py` | supervisor, reconnect, stream, reconciliation, epoch observations | `clerk_supervisor` | Clerk journal and fenced epoch/lease artifacts; history is display-only |
| `account_artifacts.py` | freezes, recovery proof, audited override, generation, restart intensity | `data_plane` or `clerk_supervisor` by event family | Their typed account artifacts; history is display-only |
| `account_reconciliation.py`, `account_journal_authority.py`, `account_crash_recovery.py`, `account_gate_promotion.py`, `journal_recovery.py`, `legacy_stale_claim_retirement.py` | reconciliation, policy, recovery and compatibility evidence | `data_plane` | Typed reconciliation/recovery artifacts and Clerk journal where applicable |
| `routers/account_reconciliation.py` | Clerk restore presentation evidence | `data_plane` | Host/Clerk restore receipt, not the display row |
| `live_engine.py` | observation-lease shadow comparisons | `bot` | Account Truth and observation-lease artifacts |

The legacy `AccountOwner` rows remain compatibility diagnostics only.  They
cannot become Clerk order/exposure authority, and the current production Clerk
continues to be the only actor allowed to submit through the canonical journal.

### Compatibility-reader ledger

| Reader or endpoint | Retained purpose | Boundary |
| --- | --- | --- |
| `read_legacy_account_events` and tolerant legacy reader | forensic access to pre-split rows | historical evidence only; no rewrite |
| `repair_account_event_sequence` and its recovery endpoint | explicit repair of a pre-split malformed sequence | legacy-only operator ceremony; never a forward writer |
| `read_account_events` / `read_account_events_with_snapshot` | legacy-call-site display compatibility | merged display rows and virtual bytes; never a causal or authority source |
| `AccountEventJournalService` | Account Desk cursor projection | explicit provenance and clocks; manual receipts read directly from Clerk journal |
| `AccountEventRecord` / `TolerantAccountEventRecord` | parse and repair historical envelopes | no forward use |

The deletion tripwires cover concurrent Clerk-supervisor and daemon appends,
same-boot replay, restart duplicates, a corrupt/truncated producer stream,
legacy dual-read, and a forged operational manual-order row.  The latter may
remain inspectable as an operational row, but cannot become an order receipt;
only the canonical Clerk projection can do so.

### Narrow control artifacts, not a replacement journal

Some safety transitions need a current, typed decision that cannot be rebuilt
from a display sort. They use account-local artifacts with one named owner;
they are not a second event ledger and never own orders, fills, positions, or
custody. The operator history mirrors their transitions only after the typed
write succeeds.

| Artifact | Sole decision it owns | Display history may not decide it |
| --- | --- | --- |
| `account_recovery_clearance.json` | a typed recovery proof or audited override can release a crash-retired restart block | a recovery-looking desk event cannot restart a bot |
| `account_reconciliation_invalidation.json` | an execution invalidates a prior reconciliation receipt | a late display row cannot make an old receipt valid or invalid |
| `account_owner_inflight.json` | an A0-prepared owner submission still needs recovery | an owner-history projection cannot suppress a recovery action |
| `account_journal_authority.json` | account-local Clerk-journal parity qualification and stream revocation | producer events cannot qualify or requalify the Clerk journal |
| `account_clerk_restart_smoke.json` | the active Clerk generation has an operator-recorded restart smoke | a historical smoke display row cannot authorize a replacement generation |
| `legacy_stale_claim_retirements.json` | a legacy sidecar claim has been retired | an operational mirror cannot hide a claim from fleet safety |
| `account_observation_lease_shadow_history.json` | submit-boundary no-weaker shadow parity for gate promotion | a delayed producer callback cannot promote the lease path |

Each artifact is atomically replaced under its own account-local lock and is
validated on read. A corrupt artifact fails the corresponding safety decision
closed; it does not fall back to producer history. The sole migration exception
is an absent artifact seeding once from immutable pre-split
`account_events.jsonl`; forward producer rows are excluded from that seed.

## Deterministic eight-bot qualification amendment (2026-07-28, #1257)

The custody program has one broker-free acceptance campaign in
`app.services.account_custody_qualification`. It executes seventeen backend
boundaries in the approved PRD with deterministic queue, fsync, qualification,
broker, callback, epoch, and action timing controls. Its report is an
operator-readable JSON artifact, not a replacement Clerk journal: every drill
contains the initial state, injected fault, expected invariant, observed
receipts, final account verdict, and a deterministic evidence reference.

The campaign exercises the deployed eight-entry / zero-risk-reducing capacity
configuration through a real ephemeral Clerk journal, epoch authority, safety
authority, durable desired-state writer, and producer-local log. It directly
proves eight retained entry slots and the ninth entry's typed refusal. A
risk-reducing asynchronous order lane is deliberately absent: no externally
reachable atomic reduction primitive currently exists. Its
deterministic controls are injected at the actual final A0-admission,
post-inbox-durable-append, and dequeued-pre-A1 boundaries; they are not clock
advances staged before the Clerk call. It reports only non-overlapping
receipt intervals (request→intake, intake→inbox fsync, A0→A1,
A1→callback, callback→ack, and epoch notice→recovery), never synthetic SLO
durations or an accepted-to-effect latency when the outage drill correctly
leaves actuation pending. The report is SHA-256 content-addressed over the
complete semantic payload, and the verifier recomputes that digest before a
report is trusted.

The browser portion is qualified separately at its own real transport boundary: the Bot
Surface test drives the production EventSource retry path from an error through
a fresh subscription and a new server snapshot. During that interval the
control panel retains the same-session snapshot read-only and preserves any
pending mutation until the new snapshot carries its receipt. This cross-stack
test is a CI/publish gate, not a drill in the backend qualification report;
the backend runner does not pretend to execute a browser reconnect.

No deterministic result claims broker or paper execution. A deterministic pass
has the explicit certificate state `DETERMINISTIC_PASSED_AWAITING_PAPER`, and
the report always records paper status as `NOT_RUN`. The runner has no IBKR
client and accepts no self-authored paper receipt. A future promotion path must
be a trusted IBKR adapter that validates immutable broker-originated evidence;
until then, the distinction between repeatable fault proof and an
environment-specific broker observation remains explicit.
