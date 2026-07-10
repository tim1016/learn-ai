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

The order-idempotency item was already satisfied before this roadmap landed.
Commit `89248b0c6` added a per-`client_order_id` lock around the complete
cache-check → contract-qualification → broker-submit → cache-store window, and
`test_place_paper_order_concurrent_same_id_places_once` forces the former
interleaving and proves one broker placement. The 2026-07-10 Stage 1 audit
removed the stale B-01 entry from `docs/known-gaps.md`; durable cross-restart
idempotency remains the separately documented Phase 3.5 boundary, not this
in-process TOCTOU defect.

For this stage, “Parquet publication” covers operator-path datasets: live-run
artifacts, live bar compaction, and optional broker tick partitions. All three
publish through a same-filesystem temporary file, fsync, atomic replace, and
parent-directory fsync; append/read-modify-write paths are serialized. Offline
reconciliation and research report bundles remain tracked separately because
their failure cannot change bot intent, runtime ownership, or restart safety.

The 2026-07-10 Stage 1 audit also closed the two stale fleet-trust entries.
Account Truth is enforced immediately before durable broker submit through
`LiveEngine`'s cached `account_truth_gate_provider`; non-pass evidence drops the
pending batch and raises `AccountTruthBlockError` before any broker call. The
portfolio and engine-wiring regressions live in
`tests/engine/live/test_live_portfolio.py` and
`tests/engine/live/test_live_engine_intent_wal_wiring_vcr_0002.py`. On
host-daemon boot, prior
`ACTIVE` account bindings not backed by a process owned by the new daemon are
now durably retired as `host_daemon.boot_liveness_unproven` before requests are
served. A daemon-owned process reaper applies the normal crash retirement path
when an owned child exits after boot, without depending on status reads. Both
daemon starts and direct `run.py start` require recovery proof before a new
`ACTIVE` row can supersede either retirement. That removes dead namespaces from
sibling trust and blocks their own submit gate. The boot, reaper, and recovery
regressions live in
`tests/engine/live/test_host_daemon_boot_reconcile.py` and
`tests/engine/live/test_run_cli.py`.

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

**Implemented 2026-07-10.** `app/services/surface_hub.py` owns each bot's
producer lifecycle, latest complete document, opaque stream epoch, semantic
fingerprint, monotonic version, coalesced refresh, periodic assembly, and
bounded shutdown. `app/services/live_instance_surface_assembler.py` owns
source gathering and semantic composition behind an explicit dependency
boundary; `live_instances.py` retains transport and wiring only. The
data-plane lifespan starts/stops all visible hubs, and deploy/start brings
newly visible bots under the same owner. A failed first assembly leaves a
retrying producer running. Normal status reads return only the stored document
or typed `SURFACE_SNAPSHOT_UNAVAILABLE`; `?refresh=true` is the explicit
diagnostic/test refresh path. Stop/restart fences and drains in-flight
assembly, and soft deletion stops/removes the hub before unregistering its
publisher. A short-lived async fleet snapshot cache coalesces the shared
run-ledger scan across all per-bot refreshes; accepted deploy/start mutations
invalidate that cache and refresh an already-running hub before returning.

Broker-activity publisher ownership follows producer-observed `live_binding`
transitions: stopped bots do not bootstrap a publisher, live bots retry a
transient bootstrap failure on later producer cycles, and transition-out or
deletion unregisters it. Full-document SHA-256 characterization fixtures in
`tests/fixtures/surface_hub/status_payload_parity.json` were generated from
pre-extraction commit `340fbb266` and pin nothing-deployed, idle/stopped,
running/live, and daemon-unreachable payloads after documented transport/path
normalization. Dedicated regressions cover freshness thresholds, blockers and
receipts, deletion, version, epoch, coalescing, retry, shutdown fencing, and
default-read side effects in `tests/services/test_surface_hub.py` and the
`tests/routers/test_live_instances*.py` suites.

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

**Implemented 2026-07-10.**
`GET /api/live-instances/{strategy_instance_id}/operator-surface/stream`
subscribes to the existing `SurfaceHub`; it never creates a request-owned
producer. Each client receives the current complete snapshot followed only by
semantic-version changes. Events use
`id: <stream_epoch>:<surface_version>`, and reconnect deliberately ignores an
obsolete `Last-Event-ID` and sends current truth rather than replaying state.
Every watcher has a latest-wins queue of one, is removed when its response
ends, and receives an explicit terminal event when the hub stops.
The browser opens the channel only through the same-origin `/api` proxy. The
shared protected-read manifest marks the live-instance namespace, the proxy
attaches the private control secret only for proven local browser intent, and
Python applies its always-authenticated read guard to that namespace.

The Bot Cockpit bootstrap still uses REST, then switches to the state stream
when `environment.flags.botCockpitStateStream` is enabled. The existing
four-second poll remains the flag-off rollback path. The frontend rejects
duplicate or older versions within an epoch and accepts any replacement
epoch. Focused backend and Angular regressions cover current-snapshot
reconnect, queue-one overwrite, suppression of transport-only refreshes,
epoch replacement, flag-off polling, flag-on poll removal, route changes, and
bounded shutdown.

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

**Implemented 2026-07-10.** Broker activity and Bot events now use separate
`DurableEventChannel` owners with one WAL observer, a bounded sequence-indexed
ring, and bounded per-client queues. SSE rows, `Last-Event-ID`, explicit query
cursors, REST backfill, reset events, and gap markers share the
`<durable_stream_id>:<seq>` cursor. A cursor from a replaced WAL is rejected by
REST and produces a named SSE reset; a cursor older than the ring or a slow
client queue produces a gap carrying the last acknowledged safe cursor. Broker
activity publishes through this channel directly after its durable append; the
legacy publisher subscriber fan-out was removed. Bot-event channels are owned
and stopped by `BotEventStreamService`, and application shutdown drains those
tailers after the per-bot producers stop. Focused regressions prove five
clients share one initial scan, composite-cursor reconnect, deep-replay gap,
WAL replacement through both polling and owner-publish paths, isolated queue
overflow, and bounded shutdown. Sequence-only rollback clients receive a
server-side deep backfill before their bounded live subscription; only live
Bot-event subscriptions are cached, and the last disconnect stops and evicts
their channel.

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
