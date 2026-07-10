# ADR-0028: Bot Cockpit operator-plane authority and channel contracts

**Status**: Proposed 2026-07-10.
**Related**: ADR-0001 (files canonical; Postgres rebuildable), ADR-0013
(operator-surface judgment vs evidence), ADR-0018 (broker session mirror),
ADR-0019 (daemon diagnostics), ADR-0022 (timestamp authority), ADR-0024
(Bot event stream), ADR-0025 (single dominant headline), ADR-0026 (daily
lifecycle single writer), ADR-0027 (operator blocker taxonomy), PRD #951
(stream-primary Bot Cockpit), and PRD #974 (daily lifecycle and AccountOwner).
**Roadmap**: `docs/architecture/bot-cockpit-target-architecture-roadmap.md`.

## Context

The Bot Cockpit can launch and control a bot entirely from the UI, but its
operator-plane implementation is still a proof of concept. Its current state
document is rebuilt for each request, the page polls it every four seconds,
some event streams rescan a WAL for every connected client, and remediation is
distributed across several render sites. This amplifies work with the number
of bots and clients and makes a transient dependency failure look like a page
failure instead of a named degraded state.

The accepted decision stack already fixes the meaning of the surface:
Python authors operator judgments, files remain canonical, the daemon owns
process-registry truth, the broker mirror is a stamped observation rather than
authority, lifecycle phase has one evaluator, and `OperatorBlocker` carries an
honest move. This ADR does not replace those decisions. It fixes the target
read path and delivery contracts that carry them to the Bot Cockpit.

The hard constraint is that the Bot Cockpit is a single-operator,
single-host research surface. It needs one efficient in-process publisher, not
a distributed messaging platform. The choices below deliberately preserve
that boundary.

## Decision

### 1. Python is the Bot Cockpit operator-plane authority

Python is the operator-plane composition authority and the shortest path to
canonical evidence. Angular consumes Python REST and SSE through the existing
same-origin proxy; it never calls a Python port directly. The proxy continues
to enforce the data-plane control-intent/secret boundary, including protected
SSE reads.

Postgres and .NET GraphQL are permanently outside the Bot Cockpit read path.
`.NET` keeps its research/backtest transport role. Postgres remains a
rebuildable analytical projection for historical, cross-run, reporting, and
triage queries.

This narrows, rather than reverses, the existing fallback contract in
`docs/bot-lifecycle-account-owner-authority.md` §4.1: that document already
requires the file-backed `compute_operator_surface` and
`compose_bot_lifecycle_chart` path whenever the lifecycle projection is empty,
stale, disabled, or down. ADR-0028 removes the Postgres lifecycle timeline
from the operator UI read path entirely. Projection unavailability remains an
analytical limitation, never “bot state unknown.”

`Frontend/src/app/services/live-runs.service.ts` is therefore the documented
Bot Cockpit transport boundary, not a bypass around GraphQL.

Python does not eliminate source staleness. A composed view can be no fresher
than the evidence it observes. Source-level freshness is part of the contract
in §5.

### 2. State boundaries retain distinct single writers

The operator surface composes these boundaries without merging their
authority:

| Boundary | Canonical form | Single writer |
|---|---|---|
| Operator intent | `desired_state.json`, `mutation_attempts/` | Python control plane |
| Lifecycle phase | `lifecycle_state.json` | Lifecycle evaluator only (ADR-0026 §4) |
| Process registry | Daemon in-memory registry | Host daemon |
| Runtime evidence | `engine_runtime.json`, `readiness_sidecar.json` | Bot child |
| Order intent | `intent_events.jsonl` WAL | Bot child |
| Broker truth | IBKR session; ADR-0018 mirror is a stamped projection | IBKR |
| Market data | Parquet bars | Engine persistence |
| Projections | `OperatorSurface` document, feeds, catalog, Postgres tables | Snapshot producer plus the relevant domain projection service |

The lifecycle phase row is intentionally separate from process truth. Only
the lifecycle evaluator commits phase transitions; start, clean-exit, retire,
and scheduled ticks call through it. The daemon, child, triage services, and
registries write evidence, never phase. Default reads never persist canonical
state.

### 3. One producer owns each versioned surface snapshot

Assembly I/O moves out of
`PythonDataService/app/routers/live_instances.py` into a per-bot
`SurfaceHub`. The hub owns assembly and the latest snapshot and calls the
existing pure projection in
`PythonDataService/app/services/operator_surface.py`. REST and SSE read the
same stored snapshot; neither independently gathers daemon, file, Parquet, or
diagnostic inputs.

A fleet-scoped daemon snapshot provider is shared by every bot hub. It polls
the daemon's batched `/instances` route once per interval and exposes a
stale-while-revalidate cache. A per-bot hub never starts its own daemon poll.

The snapshot identity is:

```text
{ stream_epoch, surface_version }
```

- `stream_epoch` is opaque and changes whenever that producer lifecycle
  restarts. It incorporates the data-plane boot identity so a client never
  compares versions from different producer lifetimes.
- `surface_version` starts within an epoch, increases monotonically, and
  advances only when the semantic document changes.
- A semantic fingerprint excludes transport metadata
  (`stream_epoch`, `surface_version`, `generated_at_ms`, and continuously
  changing display age). It includes source timestamps, freshness
  classifications, judgments, actions, and evidence. A threshold crossing
  such as `fresh → stale` is therefore a semantic change even when the source
  has not emitted new evidence.
- A client adopts a new epoch unconditionally. Within one epoch it replaces
  its document only when `surface_version` is greater.

Every boundary timestamp is `int64` Unix epoch milliseconds UTC per ADR-0022.

The normal status GET is side-effect-free over canonical state. In particular,
the status-time lazy broker-activity publisher bootstrap currently in
`_resolve_activity_publisher_for_status` moves into producer lifecycle. An
explicit authenticated `?refresh=true` request may ask the producer to run one
coalesced assembly cycle before responding, but that refresh writes no
canonical artifact and starts no unrelated publisher.

### 4. State channels and event channels have different contracts

#### State channel

`GET /api/live-instances/{id}/operator-surface/stream` emits complete
versioned snapshots. Each client has a bounded queue of one; a newer snapshot
replaces an unsent older snapshot because the latest document is complete
truth. The REST status endpoint remains bootstrap and fallback and serves the
same producer-owned snapshot. Full documents are preferred to JSON Patch:
the document is small, replacement is deterministic, and patch-order bugs add
no value here.

Every SSE state event carries `id: <stream_epoch>:<surface_version>`. On a
reconnect with an epoch the server no longer owns, the server sends its current
full snapshot; state channels do not replay obsolete intermediate documents.

#### Event channels

Broker activity and Bot events remain sequenced, append-only histories with
backfill. One WAL tailer per bot feeds a sequence-indexed ring buffer and fans
out through bounded client queues. Connected clients never trigger their own
periodic full-WAL scan.

Every event-channel SSE row carries `id: <seq>`. Rotation preserves sequence
continuity. If continuity cannot be preserved, the server emits a named reset
control event with the new durable stream identity before any new row. The
server honors `Last-Event-ID` when the cursor belongs to the current durable
stream. The shared Angular `sseSignal` primitive must implement every recovery
branch explicitly:

1. **Ordinary reconnect** — resume after the last acknowledged sequence or
   state version, using `Last-Event-ID` or an explicit cursor.
2. **Cursor older than the ring** — close the stream, page through REST
   backfill from the cursor, merge by stable sequence, and subscribe from the
   new high-water mark.
3. **WAL rotation or stream-epoch change** — re-bootstrap the current snapshot
   and applicable event backfill, then subscribe to the new stream identity.
4. **Client-queue overflow** — receive a gap marker containing the last safe
   cursor and recover through the same deep-backfill path as branch 2.

The client may close and recreate `EventSource` with a query cursor when
native automatic reconnect cannot express the required cursor. Browser
default reconnect behavior is never the correctness mechanism. Protected
streams retain the same-origin control-intent handshake used today.

The shared daemon poll is one daemon network call per interval. Its payload
size and processing remain linear in the number of bots; the improvement is
removing the `bots × clients × poll rate` call amplification.

Commands retain durable intent plus acknowledgement through
`mutation_attempts/` and reconciliation. A mutation correlation ID is present
in the state document until its pending or terminal receipt is observable, so
the UI can render `pending → receipt` without a post-mutation synchronization
fetch. Long operations return accepted-with-attempt-ID and publish their
outcome on the state channel.

### 5. Freshness belongs to each source

`generated_at_ms` proves only when the surface was assembled. Every dependency
that can age independently carries its own `as_of_ms` and freshness
classification, including:

- daemon registry/process observation;
- broker session or mirror observation;
- child runtime/readiness evidence; and
- reconciliation evidence.

The snapshot may include `age_ms_at_generation` for initial rendering, but
that continuously changing value is not part of the semantic fingerprint.
The UI advances displayed age from the source timestamp using its existing
server-clock offset; a freshness threshold transition is backend-authored and
causes a new semantic version.

Degraded conditions remain distinct: daemon `UNREACHABLE`, Python control
plane unavailable, broker `HARD_DOWN`, and analytical projection stale do not
collapse into one “degraded” bucket. The daemon connectivity monitor,
ADR-0018 broker recovery state machine, and their existing source-specific
authoring remain authoritative.

ADR-0019 is Proposed at the time of this decision. ADR-0028 explicitly adopts
its diagnostic ladder as the binding narrative authority for daemon
conditions. A circuit breaker in `host_daemon_client.py` is transport
mechanism only and never authors operator meaning.

The Python-down continuity guarantee is same-session only. A route-scoped
store retains its last snapshot in memory and renders it read-only with a
staleness banner while SSE reconnects. The resolver and guard soft-fail on an
unreachable control plane, so routing still reaches the degraded Bot Cockpit.
A known `404`/`410` may still reject a nonexistent/deleted bot. No browser
storage persists an operator snapshot: after refresh during an outage, the
honest page says “control plane unreachable” rather than presenting day-old
trading state as current truth.

### 6. Failures are isolated at their natural scope

The daemon client gains a circuit breaker with bounded exponential backoff.
When open, the producer serves the last stamped process observation and marks
the current host-process condition `UNREACHABLE`; it does not relabel the old
observation as current truth.

Each bot has separate watcher, WAL tailer, ring, client queues, and assembly
task. State queues coalesce and event queues gap-mark. Daemon mutations use a
per-bot semaphore independent of the fleet poll. This bounds per-client memory
and prevents one bot's backlog from directly consuming sibling queues.
Because all hubs still share one Python event loop, the roadmap includes an
explicit load/latency test rather than claiming task separation alone proves a
CPU bulkhead.

The host daemon runs under the host supervisor and prunes exited-process
records by a bounded count plus TTL.

AccountOwner takeover is fenced, not merely leased. An authoritative owner
generation is checked at intent acceptance and immediately before every
broker-write boundary. The Postgres `account_owner_status_snapshots` table may
project that generation but is not its authority. A paused old owner that
resumes after lease expiry must be refused. The required failure scenario is:

```text
SIGSTOP old owner → lease expires → new owner takes over →
SIGCONT old owner → every stale-generation write is refused
```

`kill -9` takeover is necessary but insufficient proof because a killed owner
cannot resume.

### 7. A condition is surface-neutral; a blocker is host-scoped

ADR-0027's `fix_here` and `fix_elsewhere` dispositions are relative to the
viewing surface. For example, fleet contamination is `fix_elsewhere` in a Bot
Cockpit but `fix_here` in Account Monitor. A single unchanged blocker cannot
truthfully serve both hosts.

The target contract therefore has two layers:

1. a surface-neutral condition identity authored once from evidence, carrying
   stable identity, severity, scope, and evidence; and
2. a host-scoped `OperatorBlocker` projection for Bot Cockpit, deploy
   preflight, fleet roster row, or Account Monitor, which authors disposition
   and moves relative to that host.

This is a planned amendment to ADR-0027, not an in-place change during its
in-flight slice 3. Until the amendment lands, the current
`applies_to = deploy | run | both` and disposition/move invariant remain
binding.

All four hosts render one `<operator-blocker>` component. Host-scoped moves
follow these rules:

- `fix_here` carries an inline action and backend-authored confirmation title,
  body, and consequence;
- `fix_elsewhere` carries an exact deep link, anchor, or pre-filled flow;
- `wait` carries no button and states its self-resolution condition; and
- `terminal` carries at least one move and renders only the ADR-0026 Button
  Rule exit.

ADR-0025 headline arbitration runs once in the backend. Angular renders the
authored placement and never arbitrates notices or blockers again.

PRD #951's closed frontend affordance map remains the deliberately interim
mechanism for its delivery stage. The final co-location stage replaces the
map's frontend-authored codomain with backend-authored, host-scoped moves. The
stream remains historical evidence; it does not become a second current
verdict.

## Frontend reactivity contract

One route-provided `BotSurfaceStore` exists per strategy instance. It owns the
bootstrap snapshot, SSE-fed snapshot, stream status, and last-event time.
Angular `computed()` values are selectors and presentation view models only:
they may select the backend-authored dominant headline, blockers, guidance,
and lifecycle chart, but never derive a verdict, action eligibility,
remediation, or arbitration result.

Global stores are limited to genuinely fleet/account-scoped state: fleet
roster, connectivity, and Account Monitor. A shared `sseSignal` owns
reconnect, cursor, backfill, gap, and teardown behavior for every channel.
Angular `resource`/`httpResource` owns request-response backfill; stable
sequence deduplication replaces ActivityTab's manual cache. `linkedSignal`
owns selections that reset when their upstream identity changes.

A functional `botSurfaceResolver` bootstraps the snapshot and soft-fails on
control-plane outage. A `botExistsGuard` blocks only when nonexistence or
deletion is proven, never when reachability is unknown. The page component
becomes layout and store wiring; the four-second polling loop, post-mutation
refetches, duplicated remediation derivations, and dispatch-object threading
are deleted.

## Answers to the five architecture challenges

1. **Scope and progressive disclosure** — the immutable ADR fixes authority
   and channel contracts; the separate roadmap sequences migration without
   making stage order an architectural invariant.
2. **State boundaries and async patterns** — §2 preserves single writers; §3
   gives snapshots one producer; §4 distinguishes latest-wins state from
   replayable history and names every resume path.
3. **Blast radius** — §5 renders dependency-specific staleness; §6 bounds
   queues/tasks, isolates mutations, breaks daemon failure amplification, and
   fences AccountOwner takeover.
4. **Signal reactivity** — one route-scoped store consumes a versioned state
   channel, while shared resources and `sseSignal` own request and stream
   mechanics.
5. **Functional decoupling** — the router transports a producer-owned
   snapshot, Angular selectors do no domain reasoning, and one host-scoped
   blocker renderer replaces parallel remediation derivations.

## What this architecture does not add

- **Message broker or Redis** — one process and one host need an in-memory hub,
  not distributed delivery infrastructure.
- **PythonDataService microservices** — splitting deployment units would add
  failure modes without creating a new authority boundary.
- **CQRS/event-sourcing framework** — the existing WALs and projections already
  provide the required command/evidence separation.
- **GraphQL subscriptions or .NET in the operator loop** — Python already owns
  the judgments and canonical-evidence composition.
- **WebSockets** — SSE downstream plus authenticated HTTP commands match the
  one-way state/event flow.
- **JSON Patch** — full versioned snapshots are small and avoid patch-order
  recovery logic.
- **Postgres in the Bot Cockpit read path** — analytical availability must not
  gate operation.
- **Multi-user RBAC, Kubernetes, service mesh, or distributed tracing** — they
  solve a deployment topology this single-operator, single-host platform does
  not have.
- **Blocker-authoring DSL** — the closed typed Python authoring table is easier
  to exhaustively test and keeps operator meaning in code review.

## Consequences

**Positive:**

- REST and SSE cannot present independently assembled versions of the same
  operator surface.
- Daemon and WAL work scales with producers rather than connected browsers.
- Reconnect, overflow, rotation, and restart are explicit protocol states
  instead of best-effort browser behavior.
- Stale evidence remains visible without being mislabeled as fresh truth.
- Backend-authored remediation can be co-located without making a condition's
  location part of its identity.

**Negative / costs:**

- `SurfaceHub` becomes a critical in-process subsystem and needs lifecycle,
  load, shutdown, and recovery tests.
- Epoch/version semantics and four stream recovery branches increase protocol
  test surface.
- Removing the Postgres timeline from the Bot Cockpit gives up a convenient
  cross-run display there; those queries move to analytical/reporting
  surfaces.
- The final blocker model requires a deliberate ADR-0027 amendment after the
  current slice rather than an immediate schema rewrite.

**Non-consequences:**

- Files remain canonical under ADR-0001.
- The broker mirror remains observational under ADR-0018.
- The Bot event stream remains historical evidence under ADR-0024.
- ADR-0026 retains lifecycle-phase ownership and the Button Rule.
- This ADR proposes no implementation change by itself; the separate roadmap
  controls delivery order.

## References

- `docs/bot-lifecycle-account-owner-authority.md` §4.1 — Postgres lifecycle
  projection and canonical file fallback.
- `PythonDataService/app/routers/live_instances.py` — current status assembly
  and status-time publisher bootstrap.
- `PythonDataService/app/services/operator_surface.py` — pure operator-surface
  projection.
- `PythonDataService/app/engine/live/host_daemon_client.py` — daemon transport
  and circuit-breaker home.
- `PythonDataService/app/schemas/operator_blocker.py` — current host-relative
  blocker contract.
- `Frontend/src/app/services/live-runs.service.ts` — Bot Cockpit REST boundary.
- `Frontend/src/app/services/broker-sse.ts` — current native `EventSource`
  wrapper generalized by the target `sseSignal`.
- `docs/architecture/bot-control-stream-primary-prd.md` — PRD #951 interim
  stream affordance map and surface-disposal contract.
