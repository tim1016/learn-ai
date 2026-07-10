# Bot Cockpit target architecture roadmap

**Status:** Proposed delivery sequence 2026-07-10. This document may change
without amending ADR-0028.
**Architecture:**
`docs/architecture/adrs/0028-bot-cockpit-operator-plane-authority-and-channel-contracts.md`.

## Purpose

ADR-0028 fixes the durable decisions: Python owns the Bot Cockpit operator
plane, one producer owns a versioned snapshot, state and event channels have
different delivery contracts, freshness is source-specific, and remediation
ends as a surface-neutral condition plus a host-scoped blocker. This roadmap
orders the work into incrementally shippable stages. A stage may depend on an
earlier stage; “shippable” means the repository remains coherent and the exit
criteria are observable at each boundary.

## Stage 0 — Finish the in-flight OperatorBlocker slice `[UX]`

Finish operator-blocker slice 3 on its existing branch and contract. Do not
introduce the future two-layer condition/blocker model during the slice.

**Exit criteria:**

- blockers render on Bot Cockpit and deploy preflight;
- the ADR-0027 disposition/move invariant tests are green; and
- no slice-3 work is duplicated on the ADR/roadmap branch.

## Stage 1 — Close known safety gaps `[SAFETY]`

Before streaming work churns the same live-control files, close or explicitly
defer each known safety defect:

- `_flatten` handles net-short exposure correctly;
- durable-state write failures fail loudly and become operator blockers;
- order idempotency claims are atomic before broker submit, closing the TOCTOU
  window;
- Parquet publication uses temporary-write plus atomic rename; and
- `verdict-gates-nothing` and crashed-sibling-`ACTIVE` are fixed here or carry
  a written deferral with owner and safety rationale.

**Exit criteria:**

- every fixed defect has a regression test that fails before the fix;
- every deferral has a concrete tracking reference and reason; and
- paper-account net-short flatten is verified end to end.

## Stage 2 — Extract the snapshot producer `[PREREQUISITE]`

Introduce the per-bot `SurfaceHub` with REST/snapshot responsibilities only.
Extract assembly from `live_instances.py` into that producer boundary. Add
`{stream_epoch, surface_version}`, version-on-semantic-change, and one stored
latest snapshot. Move `_resolve_activity_publisher_for_status` bootstrap from
status-time read behavior into producer lifecycle. This is the final producer
ownership boundary; later stages add delivery mechanisms without replacing it.

**Exit criteria:**

- the status endpoint is served from the producer-owned snapshot;
- old-versus-new golden fixtures prove payload parity for supported states;
- identical semantic documents do not increment `surface_version`;
- a producer restart changes `stream_epoch` and clients accept the new epoch;
- freshness-threshold transitions increment the semantic version; and
- default status GET is proven free of canonical writes and publisher
  bootstrap side effects.

## Stage 3 — Add delivery protocols and shared daemon observation `[SCALE]`

Keep one feature flag until the Cockpit consumer is ready, but deliver Stage 3
as three independently shippable slices with separate rollback boundaries.

### Stage 3A — Full-snapshot state SSE

Expose the `SurfaceHub` snapshot through the latest-wins state channel with
epoch/version IDs and bounded queue-one fan-out.

**Exit criteria:**

- Bot Cockpit can run with its four-second status poll disabled behind the
  flag;
- ordinary reconnect and producer-epoch replacement are tested; and
- shutdown closes state watchers and client queues within the graceful-
  shutdown budget.

### Stage 3B — Per-channel event tailers and rings

Give each bot event channel its own WAL tailer, sequence-indexed ring, and
bounded client fan-out. Use `<durable_stream_id>:<seq>` for SSE IDs,
`Last-Event-ID`, REST backfill, gap markers, and explicit query cursors.

**Exit criteria:**

- five stream clients cause no repeated WAL scans;
- ordinary reconnect, cursor-before-ring deep replay, WAL replacement, and
  queue-gap recovery are tested; and
- shutdown closes tailers and event client queues within the graceful-
  shutdown budget.

### Stage 3C — Fleet daemon cache and circuit breaker

Add fleet-batched daemon polling with stale-while-revalidate plus bounded
circuit-breaker backoff. Every bot hub consumes the shared stamped observation.

**Exit criteria:**

- instrumentation shows one daemon `/instances` network call per interval,
  independent of bots and connected clients;
- breaker-open serves the last stamped observation with current
  `UNREACHABLE` meaning; and
- cache and breaker tasks stop within the graceful-shutdown budget.

## Stage 4 — Move Bot Cockpit to the reactive store `[UX+SCALE]`

Add the route-provided `BotSurfaceStore`, the small authenticated SSE connection
primitive, typed snapshot and event-feed adapters, soft-fail resolver/guard,
route providers, request resources, and linked selections.
Delete the page polling engine, ActivityTab manual cache, post-mutation sync
refetches, duplicated remediation derivations, and dispatch-object threading.

Computed signals remain presentation selectors only; backend-authored
verdicts, headline arbitration, action eligibility, and remediation are never
re-derived in Angular.

**Exit criteria:**

- `bot-control-page.component.ts` has no status-polling `setTimeout` loop;
- stream loss retains the same-session snapshot read-only and shows each
  source's age/freshness;
- page refresh during Python outage renders “control plane unreachable” rather
  than persisted stale state;
- a proven missing/deleted bot is rejected, while an unreachable control plane
  does not block route activation;
- the authenticated mutation response establishes pending with an attempt ID,
  and the matching latest or terminal receipt arrives through the surface
  stream without a synchronization fetch; and
- one tested selector/presenter site replaces the duplicated frontend
  remediation rendering.

## Stage 5 — Deliver PRD #951 on the new store `[UX]`

Deliver the stream-primary Bot Cockpit from PRD #951 on the Stage-4 store. Its
closed frontend condition-to-affordance map ships as designed and remains the
documented interim mechanism until Stage 8.

**Exit criteria:**

- PRD #951 acceptance criteria pass;
- the node inspector and redundant trader-guidance timeline are deleted;
- the Bot event stream is the persistent run-history surface;
- stream actions join against the backend-authored current action block; and
- mutation-to-receipt rendering requires no post-action status refetch.

## Stage 6 — Enforce lifecycle and AccountOwner single writers `[SAFETY+SCALE]`

Complete the ADR-0026/PRD #974 duty-roster path and make AccountOwner the sole
broker writer. The canonical owner generation fences intent acceptance and
every broker-write boundary; Postgres owner snapshots remain projections only.

**Exit criteria:**

- every lifecycle state and condition obeys the Button Rule;
- every shared-account broker write is reachable only through AccountOwner;
- `SIGSTOP old owner → lease expiry → takeover → SIGCONT old owner` refuses all
  stale-generation writes;
- `kill -9` takeover completes within the declared lease TTL; and
- neither scenario permits a double submit or two accepted writer generations.

## Stage 7 — Add fleet delivery and prove bulkheads `[SCALE]`

Add the fleet SSE channel and roster blocker chips, bound daemon exited-record
retention by count plus TTL, and formalize per-bot queue/task resource limits.

**Exit criteria:**

- the roster is served from the shared fleet poll and channel;
- a 1,000-events/second spike on bot A does not push bot B beyond the
  baseline-derived p95 latency budget recorded by the stage test;
- client and ring memory remain within declared per-bot bounds under overflow;
- a one-week daemon soak reaches a bounded RSS plateau after exited-record
  retention stabilizes, with no continuing upward trend; and
- the soak result and retention settings are recorded with the stage evidence.

## Stage 8 — Land the host-scoped blocker end state `[UX]`

Amend ADR-0027 and introduce the surface-neutral condition plus host-scoped
`OperatorBlocker` projection. Replace PRD #951's frontend affordance-map
codomain with backend-authored moves. Add backend-authored confirmation prose,
exact deep links, and inline panels across Bot Cockpit, deploy preflight, fleet
roster, and Account Monitor.

**Exit criteria:**

- the same condition can truthfully be `fix_elsewhere` in Bot Cockpit and
  `fix_here` in Account Monitor without duplicating condition identity;
- every remediation is reachable from the surface displaying its blocker;
- all four hosts render the shared blocker component;
- ADR-0025 arbitration still occurs exactly once in Python;
- zero confirmation title/body/consequence constants remain in Frontend; and
- ADR-0027, its schema tests, and the operator-surface contract are amended in
  the same implementation change.

## Ordering rationale

Stage 0 protects the in-flight contract. Stage 1 front-loads safety before the
streaming refactor touches the same paths. Stages 2–4 are the producer-before-
consumer chain. Stage 5 folds the existing stream-primary PRD onto that
consumer instead of creating a parallel store. Stage 6 closes lifecycle and
broker-writer authority before fleet scale. Stages 7–8 depend on the channel
and store foundations from Stages 3–4.

## Deliberate exclusions

This roadmap does not introduce a message broker, Redis, a PythonDataService
microservice split, a CQRS framework, GraphQL subscriptions, WebSockets, JSON
Patch, Postgres in the Bot Cockpit read path, multi-user RBAC, Kubernetes,
service mesh, distributed tracing, or a blocker-authoring DSL. ADR-0028 records
the architectural reasons; a future need must demonstrate a changed topology
or authority boundary before revisiting them.
