# PRD — Account-safe bot operations: asynchronous Clerk custody and one operator truth spine

- **Status:** Approved 2026-07-27 — implementation slicing authority
- **Owner:** Inkant
- **Created:** 2026-07-27
- **Baseline:** `origin/master@b2a192d25c` (`fix: preserve protected account reads and drawdown provenance (#1240)`)
- **Incident dependency:** PR #1241 is open and not merged at this baseline. Every
  implementation slice below must start after #1241 merges, or explicitly rebase
  and re-prove the incident regressions it changes.
- **Implementation issues:** #1243 through #1257.
- **Builds on:** ADR 0004 (durable desired state), ADR 0008 (durable submit and
  uncertain acknowledgement), ADR 0010 (operator action semantics), ADR 0018
  (broker recovery), ADR 0025 (single dominant headline), ADR 0026 (daily bot
  lifecycle), ADR 0027 (operator blockers), ADR 0028 (operator-plane channels
  and per-source freshness), ADR 0030 (account-rooted Clerk authority), issues
  #1114 and #1150, and incident PR #1241.
- **Design source:** the 2026-07-27 AMD connectivity incident and the subsequent
  expert review. The review's useful correction is adopted here: this is a
  multi-slice custody and operator-control program, not a timeout tweak.
- **Runtime posture:** paper only. This PRD does not authorize live-money
  trading.

---

## 1. Executive summary

The AMD incident did not reveal one broken timeout. It revealed an ownership
boundary that is synchronous in the wrong place.

Today `AccountClerk.submit_intent()` durably records an intent and then waits
for the broker acknowledgement before its RPC returns. That makes a bot wait
across four different domains: local queueing, journal durability, broker
qualification/submission, and broker callback delivery. A caller deadline can
therefore expire after the Clerk has safely accepted custody but before the
caller sees the broker result. During a real connection loss this ambiguity
propagates into terminal bot outcomes, stale UI claims, and operator actions
whose effect cannot be proved.

The product change is:

1. The Clerk returns a bounded **A0 custody receipt** as soon as the intent is
   durably accepted, before broker contact.
2. Broker work continues under Clerk custody and advances asynchronously
   through A1–A3 receipts.
3. Account safety is governed by an account epoch and an effect-aware verdict.
   Unknown or unmanaged exposure suspends new risk but does not erase
   attribution or retire healthy siblings.
4. Deploy, Bot Control, and Account Desk render the same versioned
   `AccountSafetySnapshot`; they do not assemble competing safety stories.
5. Operator actions carry one typed, expiring, idempotent envelope. Durable
   risk-reducing intent remains available during outages, while actions that
   need fresh evidence fail closed.
6. The safety contract is proven with deterministic delay, disconnection,
   callback-order, Clerk-death, and bot-death drills.

The target feeling is deliberately simple: a bot may lose its process, its
socket, or its browser observer, but the account never loses the answer to
“who owns this intent, who is managing the resulting exposure, and what may
happen next?”

## 2. Ground truth on current master

This PRD starts from current authority rather than the historical target shape:

- ADR 0030 and the shipped runtime make the **Account Clerk the normal paper
  order submitter and callback owner**. The registrar-only topology in issue
  #1150 (“bots register, then submit on their own broker connections”) is
  superseded by that cutover. The useful requirements in #1150—durable
  pre-submit registration, one journal, Clerk classification, and centralized
  halt—remain binding.
- `AccountClerk.submit_intent()` currently returns
  `(AccountClerkRecordedReceipt, AccountClerkBrokerAckReceipt)`. Receipt #1 is
  fsynced before broker contact, but the RPC waits for receipt #2.
- The account-rooted `clerk_journal.jsonl` is canonical for managed order and
  exposure history. Relational transaction history is a rebuildable
  projection, never write authority.
- `account_events.jsonl` is still used by several producers for operational and
  lifecycle evidence. It must not become a second order/exposure ledger.
- Account Truth already detects `retired_owner_live_exposure`: the exposure is
  attributable, but its owner is no longer managing it. This PRD turns that
  fact into an explicit suspended-account behavior instead of a fleet-wide
  terminal reaction.
- Freshness, stream epochs, desired state, mutation attempts, operator notices,
  and blocker moves already exist, but are projected through several schemas
  and surfaces. The product gap is one account-level safety snapshot, not a
  second set of competing facts.
- Issue #1114 remains the authority for policy-governed self-healing account
  hygiene. This PRD does not duplicate its repair ladder or automation policy;
  it supplies the custody, epoch, and UI truth needed for those repairs to be
  operated safely.

## 3. Problem statement

### 3.1 One synchronous call spans unrelated clocks

The current submit RPC asks one deadline to cover:

1. bot-to-Clerk transport;
2. Clerk intake queue wait;
3. inbox and journal fsync;
4. broker request dispatch;
5. broker acknowledgement or callback delivery.

Those stages have different owners and failure modes. A timeout means only
“the caller stopped waiting.” It does not mean the Clerk rejected the intent,
the broker did not receive it, or exposure did not change.

### 3.2 Process ownership and exposure custody are conflated

The bot that originated an intent is useful provenance, but it is not the only
process capable of safely managing the order after durable admission. Once the
Clerk accepts custody, killing the bot must not erase attribution or leave the
account without a custodian. Conversely, a surviving bot must not assume that
its own health proves the account safe.

### 3.3 Reconnect facts can be individually true and jointly unsafe

IBKR callbacks, status snapshots, executions, positions, journal appends, and
browser events can arrive or be observed in a different order from the causal
broker sequence. Journal `seq` is durable serialization order; it is not a
claim that broker event A caused broker event B. Without a shared account
epoch, facts from before and after a disconnect can be accidentally composed
into a “current” state.

### 3.4 The operator sees several partial truths

Deploy preflight, Bot Control, Account Desk, broker connectivity, Account
Truth, Clerk health, and browser SSE state each explain part of the system.
The UI can therefore show a clean headline beside a stale detail, offer a
button based on a different snapshot, or confuse local browser reachability
with broker safety.

### 3.5 Outage actions lack one explicit contract

Some actions are durable desired-state changes; some are process mutations;
some are broker writes; some require fresh position evidence. The UI needs to
say which intent it accepted, whether actuation is pending, when the evidence
expired, and whether clicking again is a replay or a new request.

## 4. Product principles and invariants

### 4.1 Three roles, always distinguishable

For every intent and any exposure it creates, the system must be able to name:

- **Originator:** the immutable strategy instance/run/namespace that authored
  the intent.
- **Custodian:** exactly one durable account authority responsible for
  resolving the intent lifecycle. For normal paper operation this is the
  accepting Clerk generation.
- **Manager:** at most one currently fenced actor permitted to issue a
  follow-up broker write for that lifecycle.

An originator may die. A custodian may be replaced after fencing and recovery.
Attribution never changes. No lifecycle may have two managers.

### 4.2 Receipt #1 transfers custody, not economic outcome

An A0 receipt proves:

- the identity passed admission;
- the intent is fsynced in the canonical Clerk journal;
- the Clerk accepted responsibility to reach a terminal resolution;
- replaying the same idempotency identity cannot create a second broker order.

It does **not** prove:

- the broker received, accepted, or filled the order;
- exposure changed;
- the intent can safely be retried under a new identity;
- the account remains eligible for new entry risk.

### 4.3 Uncertainty reduces permission, not history

Unknown broker state, stale critical facts, epoch invalidation, or unmanaged
attributable exposure changes the account verdict to `SUSPENDED` or
`RECONCILING`. It never rewrites a known owner as foreign and never retires
healthy sibling bots.

### 4.4 Risk reduction is server-derived

Clients do not label their own action “risk reducing.” The Python authority
derives the effect class from:

- intent purpose;
- target order or position;
- the fresh account projection;
- worst-case fill and cancellation outcomes.

The account may permit a cancel or exact close while blocking a new entry.
Unknown effect fails closed.

### 4.5 Evidence freshness is compositional

`generated_at_ms` says when a response was assembled. It does not freshen its
dependencies. Every independently ageing source retains its own `as_of_ms`,
freshness status, and account-epoch relationship. The UI advances displayed
age; it does not author freshness.

### 4.6 Safety authority stays in Python

Python owns account verdicts, effect classification, custody folds,
reconciliation, and action capabilities. .NET may project or transport
already-authored values without recomputation. Angular renders and routes the
closed contracts; it never computes account safety from raw fields.

## 5. User stories

1. As an operator, when a bot submits an order I see “Safely handed to the
   Clerk” as soon as A0 is durable, without pretending the broker accepted it.
2. As an operator, I can follow one intent through queued, submitting,
   broker-known, working/partially-filled, and terminal outcomes from one
   custody timeline.
3. As an operator, if the originating bot dies after A0, I see the Clerk
   continue custody and I do not lose attribution.
4. As an operator, if a fill arrives after its bot is retired, the account
   becomes suspended for new entries, the exact exposure remains attributed,
   and I am offered a bounded cure.
5. As a healthy sibling bot, I am not retired or fatal-halted because another
   bot lost its socket or died.
6. As a strategy bot, I may not have more than the configured number of
   nonterminal entry intents, so a fast loop cannot flood the Clerk while
   acknowledgements are delayed.
7. As an operator, I can distinguish “the Clerk has it,” “the broker has it,”
   and “the broker changed exposure.”
8. As an operator, after broker loss I see the account epoch invalidate
   immediately and new entry risk stop before any old observation is reused.
9. As an operator, reconnect shows a finite reconciliation workflow and mints
   a new epoch only after required order, execution, and position evidence has
   been reconciled.
10. As an operator, I receive an outage diff that names intents, orders,
    executions, and positions discovered or changed while the link was down.
11. As an operator, callback arrival order cannot make a filled order look
    newly open or duplicate a fill.
12. As an operator, Deploy, Bot Control, and Account Desk show the same account
    safety verdict, epoch, freshness, and primary move for the same snapshot
    version.
13. As an operator, browser connection is visibly local evidence and never
    displayed as proof that the broker or Clerk is healthy.
14. As an operator, a durable Pause or Stop request is accepted even when the
    target process is unreachable, and the UI says that actuation is pending.
15. As an operator, Start, Resume, or Deploy is never queued while safety proof
    is unavailable; it is rejected with the current blocker and required next
    proof.
16. As an operator, Cancel is available only for an exact, current order and
    can be durably queued before broker actuation.
17. As an operator, Flatten is never implied by a stale position. It requires a
    fresh reconciled target, confirmation, and post-action proof.
18. As an operator, every action has an idempotency identity, evidence version,
    expiry, preconditions, confirmation contract, and durable receipt.
19. As an auditor, I can reconstruct a lifecycle using event time, arrival
    time, durable record time, account epoch, and immutable identifiers without
    treating file sequence as causality.
20. As a validation operator, I can deliberately inject queue delay,
    qualification delay, callback delay/reordering, socket loss, bot death, and
    Clerk death and receive a machine-checkable drill report.

## 6. Domain contract

### 6.1 Custody stages

The public contract uses four stages:

| Stage | Meaning | Minimum durable evidence |
|---|---|---|
| `A0_CUSTODY_ACCEPTED` | Clerk owns resolution; no broker claim | fsynced recorded-intent row |
| `A1_BROKER_WRITE_STARTED` | fenced manager crossed the broker-write boundary | fsynced submitting row |
| `A2_BROKER_KNOWN` | broker identity/ack is correlated to the intent | ack/order identity row |
| `A3_ECONOMIC_TERMINAL` | lifecycle can no longer create new economic change | terminal order state plus deduplicated fills |

The detailed internal lifecycle remains explicit:

`recorded → queued → submitting → broker_known → working |
partially_filled → filled | cancelled | rejected | expired_before_submit |
uncertain_requires_reconciliation`.

Status is folded as a monotone state machine with explicit exceptional
transitions. Fill facts form an idempotent set keyed by broker execution
identity. A late status callback may add evidence, but may not move the
economic lifecycle backwards.

### 6.2 Bounded A0 admission

The first production cutover is intentionally bounded:

- queue capacity is configured from supported paper-fleet capacity, exported
  as health evidence, and never unbounded;
- one nonterminal **entry** intent per strategy instance is the initial
  policy; risk-reducing intents use a separate bounded lane;
- admission refuses when the durable queue is full rather than waiting until
  the client times out;
- entry intents may expire only before `A1_BROKER_WRITE_STARTED`;
- exit, cancel, and flatten intents never silently auto-void because a timeout
  elapsed;
- cancel of an A0-only intent closes it locally; cancel after A1 becomes a
  broker cancellation lifecycle;
- on restart, queued entries are expired with a receipt, queued
  risk-reducing intents are surfaced as action-required or resumed under an
  explicit recovery rule, and any intent at/after A1 is reconciled before new
  entry admission.

The launch SLO is:

- A0 returns or explicitly refuses within **10 seconds** in the supported
  eight-bot paper configuration, including queue admission and fsync;
- the instrumentation slice must measure p50/p95/p99 per phase before a lower
  production timeout is selected;
- no code may infer broker absence merely because any client deadline elapsed.

### 6.3 Retry identity

ADR 0008 currently permits one retry **with the same
`intent_id`/`order_ref`** only after the broker is provably absent. That stays
binding.

The proposed alternative—mint a new intent with `supersedes`—requires an
explicit ADR 0008 amendment and a migration/dual-read plan. No implementation
slice may quietly change retry identity while building asynchronous custody.

### 6.4 Account epoch

The Clerk owns a persisted account epoch:

```text
account_epoch = (clerk_boot_id, epoch_seq)
```

Each journal observation carries:

- `origin_epoch`: the epoch under which the intent was admitted, when
  applicable;
- `observed_epoch`: the current Clerk epoch when the observation became
  durable;
- `reconciliation_id`: required instead of an observed epoch while the Clerk
  is rebuilding proof;
- `event_at_ms`: broker/source event time when supplied;
- `arrived_at_ms`: local receipt time;
- `recorded_at_ms`: durable journal time.

All timestamps are `int64 ms UTC`. Raw broker timestamp text may be retained as
opaque forensic evidence but never crosses a typed boundary as time authority.

The current epoch is killed by:

- broker socket loss;
- IBKR connectivity-loss signal such as 1100;
- critical callback/heartbeat silence past its backend-authored threshold;
- failure of an active status probe while nonterminal intents exist;
- accepting Clerk death or generation fencing.

Both IBKR 1101 and 1102 create a new epoch. They may select different
reconciliation depth, but neither preserves write permission merely because
the broker says data was maintained.

During invalidation:

- new entries are refused;
- already admitted broker writes do not blind-retry;
- exact cancels and exits may proceed only through the current
  effect-classified recovery lane;
- the Clerk reconciles required open orders, completed orders/executions, and
  positions;
- a new epoch becomes current only after a durable `CLEAN` or explicitly
  `ADOPTED` reconciliation receipt.

### 6.5 Account safety verdict

`AccountSafetyVerdict` is closed:

| Verdict | Meaning | Entry risk | Exact cancel/exit |
|---|---|---|---|
| `CLEAN` | current epoch and critical facts prove managed state | eligible subject to normal gates | eligible |
| `RECONCILING` | link restored; current proof is being rebuilt | blocked | only through recovery policy |
| `SUSPENDED` | attribution exists, but management or current proof is missing | blocked | permitted when effect is proved risk reducing |
| `CONTAMINATED` | exposure/order is foreign or cannot be safely attributed | blocked | only account-emergency policy |

`retired_owner_live_exposure` maps to `SUSPENDED`, not `CLEAN` and not
automatically `CONTAMINATED`. The suspension lifts only when all of the
following are true under the current epoch:

- no nonterminal intent remains under the retired owner;
- no unmanaged or unknown exposure remains;
- a fresh reconciliation receipt proves the resulting account state;
- any successor/adoption binding is durable before it is treated as manager.

### 6.6 Sole writer and audit logs

The split is:

- `clerk_journal.jsonl`: sole authoritative writer is the current fenced
  Clerk. It owns intent, broker lifecycle, execution, exposure attribution,
  reconciliation, and account-epoch receipts.
- Producer operational logs: daemon, Clerk supervisor, data plane, and bot may
  each append to their own log with producer-local sequence.
- Operator history projection: merges producer logs deterministically by
  timestamp and stable tiebreaker for display. It makes no causal or global
  total-order claim.

The existing multi-producer `account_events.jsonl` is migrated or narrowed;
adding a global file mutex is not the target architecture.

## 7. `AccountSafetySnapshot`

One Python-authored snapshot is the truth spine for Deploy, Bot Control, and
Account Desk:

```text
AccountSafetySnapshot
  schema_version
  snapshot_id
  snapshot_version
  generated_at_ms
  account_id
  posture
  verdict
  verdict_reason
  account_epoch
  reconciliation
  critical_sources[]
  custody_summary
  exposure_summary
  blockers[]
  actions[]
  outage_diff
  evidence_refs[]
```

### 7.1 Composition rules

- It composes existing Account Truth, observation lease, Clerk health,
  reconciliation, transaction projection, lifecycle, and blocker authorities.
  It does not fork them.
- Every critical source includes its own `as_of_ms`, freshness, epoch or
  reconciliation relationship, and evidence reference.
- Snapshot version changes when a semantic dependency changes, including a
  backend-authored freshness threshold transition.
- The snapshot never calls the broker from a fleet/list read. Broker work
  remains in the background observation/reconciliation authority.
- A stale snapshot is still renderable as stale evidence. It cannot authorize
  a fresh action.

### 7.2 Surface behavior

All three surfaces render the same compact **truth spine**:

- account verdict and dominant reason;
- current epoch and “verified at” time;
- Clerk custody count by stage;
- unmanaged/unknown exposure count;
- one primary operator move;
- snapshot version used by the move.

Each surface may host different detail, but may not change the verdict,
headline, or action eligibility:

- **Deploy:** account admission and why a new bot may or may not be created or
  started.
- **Bot Control:** instance provenance plus a custody ribbon for this
  namespace; deeper custody evidence opens in the shared evidence drawer.
- **Account Desk:** full account reconciliation, exposure, outage diff,
  custodian policy, and account-scoped recovery.

The browser transport chip is explicitly local:
“This browser is receiving updates” / “This browser is offline.” It is not
inside `AccountSafetySnapshot` and is never styled as broker proof.

The slice that introduces the truth spine retires the older contradictory
connectivity/safety strips on those hosts in the same change. It does not add
another permanent banner.

## 8. Operator action envelope

The program extends the existing `ActionCapability`, `OperatorMove`, durable
desired-state, and mutation-attempt contracts into one presented envelope; it
does not create arbitrary executable actions authored by the backend.

```text
PresentedOperatorAction
  action_id                  # closed enum
  target                     # typed account/instance/order/intent identity
  snapshot_id
  snapshot_version
  evidence_refs[]
  effect_class               # server-derived
  idempotency_key
  issued_at_ms
  expires_at_ms
  preconditions[]
  confirmation
  availability
  unavailable_reason
  disposition
```

Angular routes only closed action IDs to typed local dispatchers. Backend
prose is rendered verbatim. Raw codes use the shared `receiptLabel` pipe;
opaque IDs, refs, hashes, and paths remain exact.

### 8.1 Outage semantics

| Action | During missing current proof |
|---|---|
| Deploy / Start / Resume | reject; never queue permission to create risk |
| Pause | durably accept desired state; show actuation pending |
| Stop / End day | durably accept desired state; show actuation pending |
| Retire | accept only if the lifecycle evaluator can preserve/account for every nonterminal intent and exposure; otherwise route to custody resolution |
| Cancel exact order | durably queue only with exact current order identity; execute when the fenced manager can prove the target |
| Flatten | record operator intention, but do not synthesize quantities from stale evidence; require fresh reconciliation, confirmation, and post-action proof before broker writes |
| Reconcile | accept as an idempotent recovery request |

An action response reports durable acceptance separately from observed effect.
Timeout is never translated to success or failure without the matching
mutation/custody receipt.

## 9. Recovery and callback-order contract

IBKR order callbacks do not have an independent health transport. Health is
proved by:

- the Clerk session heartbeat/connection epoch; and
- active, bounded re-poll while nonterminal intents exist.

Callback folds obey:

- execution dedupe by `exec_id`;
- order identity by namespaced `order_ref`, then durable broker identities
  according to ADR 0008;
- terminal economic state cannot regress because an older status arrived
  later;
- a fill may arrive before an acknowledgement and still attach to the durable
  intent;
- journal sequence orders durable writes only;
- every reconnect emits an outage diff, including an empty “no changes
  discovered” receipt.

## 10. Observability and SLOs

Each intent exposes phase timestamps and durations for:

- bot request sent;
- Clerk request received;
- queue admitted/refused;
- inbox fsynced;
- journal A0 fsynced;
- broker write started;
- broker call returned/raised;
- first broker identity observed;
- first fill observed;
- economic terminal recorded;
- bot/originator notified.

Required operational metrics:

- A0 latency p50/p95/p99 and refusal count;
- queue depth/capacity by entry and risk-reducing lane;
- age and count of intents in every custody stage;
- uncertain/reconciliation count;
- callback heartbeat and active-probe age;
- epoch invalidation and reconciliation duration;
- stale/unknown critical source count;
- orphan/unmanaged exposure count;
- action accepted-to-effect latency;
- producer-log merge gaps or corrupt rows.

Initial safety SLOs:

- no broker write occurs without a prior durable A0 receipt;
- no duplicate broker write for one intent identity;
- no new entry crosses A1 after account-epoch invalidation;
- A0 returns or refuses within 10 seconds in the supported eight-bot paper
  configuration;
- capacity exhaustion is explicit, bounded, and visible;
- every reconnect reaches a durable clean/adopted/suspended/contaminated
  verdict or remains visibly reconciling; it never times out into “clean”;
- no transient Clerk/broker delay terminally retires an otherwise healthy
  sibling bot.

Latency targets tighter than the 10-second admission bound are selected only
after the instrumentation slice supplies receipts.

## 11. Deterministic validation strategy

The feature is not accepted by a happy-path browser demo. The test harness must
control each boundary independently and produce a content-hashed drill report.

### 11.1 Required drills

1. **Queue delay before A0:** one bot is delayed; siblings receive bounded
   admission or explicit capacity refusal.
2. **Fsync delay:** A0 does not return before durability, and no broker write
   starts.
3. **Qualification delay after A0:** bot stays alive or may die safely; Clerk
   retains custody.
4. **Socket loss after A0 and before A1:** entry remains resolvable; epoch
   invalidates; no duplicate.
5. **Socket loss after A1 before A2:** intent becomes uncertain and reconciles
   under ADR 0008.
6. **Fill before ack callback:** fill attaches once; lifecycle does not
   regress.
7. **Duplicate and reordered callbacks:** execution set and terminal state are
   stable.
8. **Originator death after A0:** Clerk continues; attribution remains;
   sibling bots do not terminally fail.
9. **Originator retirement followed by late fill:** verdict becomes
   `SUSPENDED`; entries block; exact exit/cancel remains available.
10. **Clerk death with nonterminal intents:** generation fences, replacement
    reconciles before accepting entries, and no two managers exist.
11. **IBKR 1101 and 1102:** both mint a new epoch; configured reconciliation
    depths run; no stale write permission survives.
12. **Callback silence with socket connected:** active poll detects loss of
    proof and invalidates permission.
13. **Browser offline:** server custody continues; UI reconnect renders the
    authoritative history without inventing missed transitions.
14. **Pause/Stop during daemon outage:** durable intent is accepted and later
    actuation is idempotent.
15. **Start/Resume/Deploy during outage:** no action is queued and the exact
    blocker is returned.
16. **Flatten with stale positions:** no broker write; reconciliation and
    confirmation are required.
17. **Operational-log concurrency:** Clerk and daemon lifecycle events remain
    durable without contending for a global order/exposure writer.
18. **Outage diff:** changed and unchanged reconnects both emit explicit
    receipts.

### 11.2 Test layers

- Pure fold/property tests for lifecycle monotonicity, execution dedupe,
  attribution, effect classification, and epoch compatibility.
- Journal durability/crash-boundary tests around A0, A1, and callbacks.
- Clerk RPC integration tests with a deterministic fake broker.
- Host daemon/process tests for bot and Clerk death.
- Router contract tests for the snapshot and action envelope.
- Angular tests that prove the same snapshot renders consistently on all three
  hosts and that stale actions cannot dispatch.
- One supported eight-bot paper drill after the deterministic suite passes.

IB Gateway availability is not required for deterministic acceptance. A real
paper session is a final environment receipt, not the only way to prove the
state machine.

## 12. Tracer-bullet implementation slices

These are the product slices to turn into GitHub issues after operator
approval. Each slice must be independently mergeable and demonstrate a
visible behavior or safety receipt.

1. **#1243 — Instrument Clerk custody and ratify the A0/epoch contracts.** Add phase
   stamps/metrics to the existing synchronous path, render a custody timeline
   in existing evidence, and record the required ADR amendments. No behavior
   cutover yet.
2. **#1244 — Introduce bounded asynchronous Clerk submit behind a disabled
   capability.** Add entry and risk-reducing queues plus an RPC that returns
   A0, advances A1–A3 in the background, exposes per-intent reads, and proves
   delayed/reordered broker behavior with a fake broker. Normal strategy submit
   remains on the old call.
3. **#1245 — Cut normal paper strategy submission over to A0 custody.** Move the
   strategy path to the asynchronous API, add the one-nonterminal-entry gate
   and originator notification/fold, and prove bot death after A0.
4. **#1246 — Shadow account epochs and prove every invalidation trigger.** Persist the
   epoch, add dual epoch stamps and reconciliation IDs, and publish a
   would-block shadow verdict for socket loss, broker signals, silence, and
   Clerk death without changing admission yet.
5. **#1247 — Enforce the account-epoch write fence and reconcile before reopening.**
   Block new entry writes after invalidation, reconcile required broker facts,
   mint a new epoch only from durable clean/adopted proof, and emit an outage
   diff for 1100/1101/1102, silence, and Clerk death.
6. **#1248 — Suspend late exposure from a retired bot without killing siblings.**
   Project `retired_owner_live_exposure` into `SUSPENDED`, make retirement
   custody-aware, and prove the dead-bot late-fill lifecycle and lift predicate
   end to end.
7. **#1249 — Permit only server-proved risk reduction while suspended.** Derive effect
   class from intent purpose plus current account projection, keep entries
   blocked, and permit only exact cancels/closes whose worst-case effect does
   not add risk.
8. **#1250 — Expose the versioned `AccountSafetySnapshot` composition API.** Compose
   existing account authorities into one read contract with per-source
   freshness, epoch relationships, custody/exposure summaries, blockers, and
   evidence references.
9. **#1251 — Render one account truth spine on Deploy, Bot Control, and Account
   Desk.** Consume the shared snapshot, show one verdict/epoch/primary move,
   label browser connectivity as local evidence, and remove the superseded
   contradictory safety strips.
10. **#1252 — Add the per-bot custody ribbon and evidence-drawer timeline.** Show the
    selected namespace's A0–A3 counts and intent timeline without duplicating
    account-level verdict logic.
11. **#1253 — Prove the presented action envelope with Reconcile Now.** Extend existing
    capability/move/mutation contracts with snapshot version, expiry,
    idempotency, evidence references, and server revalidation; route one
    recovery action end to end before migrating the dangerous actions.
12. **#1254 — Enforce the outage matrix for ordinary lifecycle actions.** Durably
    accept Pause/Stop while actuation is unavailable, reject rather than queue
    Deploy/Start/Resume, and report intent separately from observed effect.
13. **#1255 — Make exact Cancel and fresh-evidence Flatten snapshot-bound.** Queue only
    an exact current order cancel; require reconciliation, confirmation, and
    post-action proof before a flatten broker write.
14. **#1256 — Separate operational producer logs from the Clerk ledger.** Migrate
    multi-producer operational events away from shared order/exposure
    authority, keep the Clerk sole writer, and expose a deterministic
    non-causal history merge.
15. **#1257 — Qualify the supported eight-bot fleet under deterministic faults.**
    Package the full fault suite, capacity/latency report, and one paper
    acceptance run into a reproducible content-hashed artifact. Tune production
    deadlines only from the measured results.

## 13. Dependencies and sequencing

```text
PR #1241 merged/rebased
        |
        v
  1 Telemetry + ADRs
        |
        +------> 2 Async API shadow ------> 3 Strategy A0 cutover
        |
        +------> 4 Epoch shadow ----------> 5 Epoch enforcement
                                              |
  3 + 5 -------------------------------> 6 Suspended late exposure
                                              |
                                              v
                                        7 Risk-reduction policy

  5 + 6 -------------------------------> 8 Safety snapshot API
                                              |
                                +-------------+-------------+
                                v                           v
                         9 Shared truth spine       10 Custody ribbon
                                |
                                v
                         11 Action envelope
                                |
                          +-----+-----+
                          v           v
                   12 Outage matrix  13 Cancel/Flatten

  1 -------------------------------> 14 Producer-log split

  3,5,6,7,9,10,11,12,13,14 -------> 15 Qualification
```

Slices 2–3, 4–5, 8–10, and 11–13 must remain separate. Their shadow,
backend-contract, and one-action tracer bullets prove the safety boundary
before a behavior or UI-wide cutover.

## 14. Definition of done

- A bot can receive A0 and die without losing attribution, duplicating an
  order, or forcing healthy siblings into terminal lifecycle states.
- Every nonterminal intent has one durable custodian and at most one fenced
  manager.
- Account epoch invalidation blocks new entry writes before reconnect facts are
  reused.
- Late and duplicate callbacks fold idempotently without lifecycle regression.
- Retired-owner live exposure is suspended and curable, never mislabeled
  clean.
- Deploy, Bot Control, and Account Desk render one snapshot identity and
  verdict for the same account state.
- Browser connectivity is never presented as broker safety.
- Every presented action is typed, idempotent, expiring, snapshot-bound, and
  server-revalidated.
- Pause/Stop remain durably expressible during outages; Start/Resume/Deploy
  cannot be queued without current proof; Flatten cannot compute from stale
  positions.
- The Clerk journal remains the sole order/exposure authority; operational
  history no longer pressures it into a global multi-producer log.
- All 18 deterministic drills pass, followed by the supported eight-bot paper
  qualification.
- Every bug fix has a regression test. Every timestamp crossing a boundary is
  `int64 ms UTC`. No new dependency is introduced without a documented
  rejected alternative.

## 15. Non-goals

- Enabling live-money trading.
- Replacing files-canonical authority with Postgres or another relational
  writer.
- Letting the browser participate in the safety loop.
- Automatically flattening merely because a network or daemon connection was
  lost.
- Treating journal sequence as causal event time.
- Implementing issue #1114's entire self-healing custodian policy or repair
  ladder; this PRD integrates with it.
- Rewriting the Bot Control or Account Desk information architecture beyond
  the shared truth spine and evidence drawer.
- Allowing backend-authored arbitrary URLs, commands, or executable action
  payloads.
- Changing ADR 0008 retry identity without an accepted amendment.
- Solving high-frequency order throughput. Capacity targets the supported
  eight-bot paper validation fleet.

## 16. Explicit ADR work

Slice 1 must either amend or add decision records for:

1. ADR 0030: A0 transfers custody and broker work continues asynchronously.
2. ADR 0008: current same-identity retry remains, or an explicit supersession
   amendment is accepted before code changes.
3. Account epoch ownership, invalidation triggers, and dual epoch stamps.
4. `CLEAN | RECONCILING | SUSPENDED | CONTAMINATED` and effect-aware
   permissions.
5. The Clerk journal versus producer operational-log writer split.
6. The shared `AccountSafetySnapshot` as composition authority, not a new raw
   fact source.

Any conflict discovered between a new decision and a currently accepted ADR
must be surfaced in the issue and resolved in the same PR; implementation may
not silently pick a side.

## 17. References

- `docs/architecture/adrs/0004-instance-addressed-operator-control-plane.md`
- `docs/architecture/adrs/0008-durable-submit-protocol-order-identity-recovery.md`
- `docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md`
- `docs/architecture/adrs/0026-daily-bot-lifecycle-three-state-button-rule-single-writer.md`
- `docs/architecture/adrs/0027-operator-blocker-disposition-taxonomy.md`
- `docs/architecture/adrs/0028-bot-cockpit-operator-plane-authority-and-channel-contracts.md`
- `docs/architecture/adrs/0030-account-clerk-account-rooted-journal.md`
- `PythonDataService/app/engine/live/account_clerk.py`
- `PythonDataService/app/engine/live/account_clerk_rpc.py`
- `PythonDataService/app/engine/live/account_clerk_journal.py`
- `PythonDataService/app/engine/live/account_clerk_journal_models.py`
- `PythonDataService/app/services/account_truth_snapshot.py`
- `PythonDataService/app/schemas/account_truth.py`
- GitHub issues #1114 and #1150
- GitHub PR #1241
