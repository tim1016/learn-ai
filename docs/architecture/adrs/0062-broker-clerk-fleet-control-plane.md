# ADR 0062 — A broker-neutral fleet control plane addresses isolated broker clerk agents

**Status:** Accepted 2026-09-12
**Provenance:** The multi-broker clerk PRD [`docs/prds/2026-09-12-multi-broker-clerk-control-plane.md`](../../prds/2026-09-12-multi-broker-clerk-control-plane.md), selected by the two-contestant plan tournament recorded in [`docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json`](../../audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json) (Sol plan B winning 89–69 over the GLM plan A, reviewed with no material blockers). The owner resolved the delivery-shaping decisions on 2026-09-12: the first slice is the foundation spine (this ADR plus `app/broker/fleet/` with fake-provider conformance); the fleet runs as Docker Compose named volumes on one private host network; the first real configuration is Alpaca Paper plus Alpaca Live with the provider extension boundary left open for Tradier, Webull and later brokers; and browser-driven enrollment is deferred for the reasons recorded in Decision 8, not banned forever.
**Decision drivers:** The Alpaca runtime is intentionally single-clerk — one process-global `ActiveClerkRuntime`, one effective broker configuration, one `ALPACA_CLERK_DIR`, and the `installation_worker()` lock enforces one worker process per installation. Concurrent Paper and Live operation therefore requires separate installations with no shared control plane, no shared routing contract, and no stable clerk identity a browser can address. Separate Clerk volumes alone do not prevent two volumes being configured with credentials for the same external account; only a fleet-level, broker-qualified assignment fence does. And a generic trading clerk would erase provider safety boundaries — Alpaca's account verification, custody, leases, arming and recovery semantics are Alpaca's, and a future Tradier or Webull clerk must own its own rather than inherit Alpaca behaviour by registering a string.
**Related:** ADR 0035 (SQLite Clerk is the sole custody authority — **unchanged; the coordinator stores no custody**), ADR 0037 (SQLite sole Alpaca custody — unchanged, per clerk), ADR 0042 (one semantic seam, exact-identity selection — the fleet route contract extends this with explicit broker/clerk identity), ADR 0047 (recovery is an offline ceremony — retained per clerk), ADR 0059 (arming and envelope — retained per clerk; its one-live-account-per-installation scope clause extended by this ADR's clerk-per-lane topology), ADR 0060 (configuration profiles on the Clerk volume — Decision 5's no-worker-identity clause superseded here, everything else stands per clerk), ADR 0022 (`int64 ms UTC` — all fleet wire and storage timestamps).
**Vocabulary:** `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)".
**Supersedes:** [ADR 0060](0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md) Decision 5's clause "**one worker per installation … no `worker_id` column and no workers table**" **and only that clause**, in exactly the way ADR 0060 itself anticipated: it deferred cross-installation multi-worker to "a shared Clerk volume, a worker key, one-worker-per-account enforced in the profiles database" and named it so it would be *designed rather than discovered*. This ADR is that design, generalized: the worker identity lives in the fleet registry as a durable `worker_key` bound to one clerk, and one-writer-per-account is enforced broker-qualified across volumes by the fleet assignment fence. ADR 0060's remaining content — profiles DB on the Clerk volume, staged/effective/sealed, `selection_generation` fencing, the envelope type fidelity, Paper developer reset — stands unchanged **per clerk**: each clerk volume keeps its own profiles database and its own installation selection row, and nothing in this ADR moves configuration off the Clerk volume. This ADR also **extends** (does not weaken) ADR 0059's "Not done by this ADR: more than one live account per installation": a fleet installation may now operate multiple clerks, but every `real_live` clerk retains the full three-way mode agreement, per-instance arming, sealed envelope and cash-bound ENTER semantics independently, and a second live clerk is enrolled only after the PRD's Phase 5 gates pass.

## Context

One installation can address exactly one effective clerk. The frontend can render configuration and the Trader/Operator lenses, but it has no durable clerk identity with which to address multiple isolated execution lanes, and the routing contract carries no lane identity at all — a command's destination is whatever the single active runtime happens to be.

Two failure modes make "just add another account in the same process" the wrong answer. First, the process-global singletons (`ActiveClerkRuntime`, the active binding, the trade-updates consumer, the market-liveness consumer) are structural: a second clerk in one process shares every one of them, so a Paper fault can disable a Live lane. The known gap F13 (panel reads serializing globally behind one GIL) is further evidence that lanes need processes, not threads. Second, the existing execution lease excludes writers only within one shared custody volume; two different Clerk volumes configured with credentials for the same external account would both believe they are the valid writer.

The PRD's platform direction is therefore a narrow broker-neutral fleet layer plus distinct broker-owned clerk implementations: Alpaca Paper and Alpaca Live run as different processes against different physical volumes, profile databases, custody databases, credentials, leases, streams and recovery units; a future Tradier integration supplies a `TradierClerkAgent` and a future Webull integration a `WebullClerkAgent`, each owning its SDK client, credential convention, account verification, commands, capabilities, custody semantics, receipts, risk or arming policy, recovery and tests. No broker inherits Alpaca behaviour merely by registering a string.

## Decision

### 1. A small Python fleet coordinator owns identity, discovery, assignment fencing and routing correlation — never execution

The coordinator is a control plane: clerk identity, discovery, exact routing, broker-qualified account-assignment fencing, health projection, and routing receipts. It is not an execution authority. Its registry is a dedicated schema-versioned SQLite database on the coordinator's own control volume (following the repository's WAL, migration-lock and local-filesystem patterns) and stores **no lane configuration, custody, order, fill, position, activation, arming, callback or operational execution data**. The generic directory it serves is broker-neutral and computes no balances, positions, P&L, exposure or risk — financial truth stays in each provider clerk's custody, and any cross-lane comparison remains a read-only, provenance-preserving aggregation that never combines values.

### 2. One clerk, one process, one physical volume — and two fences

Every production clerk is a separately deployed process against a distinct named physical volume holding only that clerk's profile database, selection row, custody databases, ledgers, receipts, streams, operational files, backups and recovery state. Alpaca Paper and Alpaca Live may not use writable subdirectories of the same mounted production volume. Isolation is structural, not a naming convention: a volume identity marker (backend-issued `volume_id` plus an external nonsecret mount attestation, written at provisioning time) must verify — canonical root, mount, marker, deployment expectation and registry identity — before any database writer or broker client opens. Missing, duplicated, copied, symlinked, noncanonical or mis-mounted identity fails before authority opens.

Two fences are required because they fence different things: the existing local custody lease protects one volume, and the broker-qualified fleet assignment protects one `(broker, canonical external account)` across volumes.

### 3. Identities are durable, opaque and broker-qualified

- A stable, opaque, non-semantic `clerk_id` is backend-issued; callers never mint or parse it. Retirement is terminal; IDs are never recycled.
- Each clerk carries an immutable `broker`, a durable `worker_key`, a durable `volume_id`, an ephemeral `agent_instance_id`, a monotonic `routing_epoch` incremented per agent registration, and a clerk-local `effective_binding_generation` that changes only when the effective profile, revision or account changes.
- `worker_key`, internal endpoints and internal service credentials never cross the public API.
- The account assignment key is `(broker, provider_canonical_external_account_id)`. The provider adapter alone canonicalizes and verifies an external account ID; the registry treats it as opaque. Identical raw account IDs may therefore exist safely under different providers and must conflict within one.

### 4. Assignment ownership never expires; reassignment is a proof-driven host ceremony

Only one reserved or effective assignment may exist per key. Assignment generations are monotonic and updated transactionally. A heartbeat timeout or unreachable clerk marks the lane unreachable but **never releases its assignment** — liveness never transfers ownership, so a network partition cannot manufacture a second broker writer. Reassignment is an offline host ceremony proving the old agent and volume are offline, credentials are isolated, and provider-specific obligations are clear.

### 5. Explicit routing and response verification

Every public provider route carries both `{broker}` and `{clerk_id}` as path segments; the coordinator resolves `clerk_id` and then verifies the path broker equals the clerk's immutable broker. The broker string never selects an arbitrary network endpoint — endpoints come only from deployment-owned registry configuration. Commands carry broker, clerk, exact account when applicable, entity identity, typed capability, durable idempotency identity and expected effective-binding generation; the coordinator uses immutable clients bound to one broker/clerk/endpoint/credential tuple, and the agent validates provider, clerk, volume, internal credential, routing epoch, effective account, binding generation, capability and provider safety gates before any mutation. Browser selection is never command authority: an action freezes its target identity from the server resource it was opened against. The PRD's stable refusal families (`clerk_not_found`, `clerk_broker_mismatch`, `clerk_volume_clone_detected`, `clerk_assignment_conflict`, and the rest) are pinned with HTTP status and retry semantics in the public contract. A routing receipt correlates an attempt but never replaces the provider clerk's execution or custody receipt.

### 6. Capabilities are closed, typed, provider-declared evidence

A capability is a closed typed operation declared by a concrete provider adapter. Unsupported actions are typed refusals, never translation, emulation or downgrade, and the generic fleet layer may not infer parity between providers. The production adapter registry is code-owned; test-only fake providers are injectable without entering it, and an unknown production provider or undeclared capability fails closed. Generic fleet modules must not import Alpaca risk, custody, execution, arming or recovery implementations.

### 7. Startup, Apply and recovery semantics are preserved per clerk

Each clerk keeps immutable profile revisions, Stage → Apply (`202`, intent only) → controlled restart, worker-only effective acknowledgement, one-shot Apply, last-effective crash recovery and stale selection-generation fencing exactly as ADR 0060 defines them — once per clerk volume. The provider adapter performs read-only account, mode and pin verification before fleet assignment reservation; the clerk reserves its account before opening custody and its execution lease; the coordinator makes a session command-routable only after the worker has acknowledged the local binding and confirmed the fleet assignment with its binding generation. During coordinator unavailability, a verified original clerk may recover its already-confirmed last-effective binding, while new assignments, different-account Apply, enrollment, reassignment and browser commands fail closed. Provider custody recovery remains a provider-owned offline ceremony (ADR 0047); volume restore/rebind and fleet registry recovery are separate host-only ceremonies.

### 8. Enrollment, retirement and reassignment are host ceremonies now; a browser path is deferred, not forbidden

Clerk enrollment, retirement, remount, restore, reassignment, process restart and Live arming are **not browser actions in this delivery**. The reasons are structural, not stylistic:

1. **Enrollment requires host powers a browser-gated process must not hold.** Creating a named volume, mounting it, injecting environment-only credentials and starting a process are orchestrator operations. Making them browser-reachable means either accepting secret material over the API (forbidden — credential resolution is a code-owned mapping from `(broker, clerk_id, credential_slot)` to environment-only material) or giving the data-plane service docker-level authority, which turns every future compromise of that service into a host compromise.
2. **The trust model is one shared installation secret.** With no operator identity, RBAC, attribution or revocation, a browser-reachable enrollment would let any session holding the secret restructure the fleet — including minting new real-money lanes.
3. **Every authority-changing ceremony in this repository is already proof-based and host-driven** (Apply is 202 plus a controlled restart; arming is a CLI; recovery is offline). Enrollment creates a new authority and is the most consequential of them.

This is recorded as a **deferred owner decision, not a permanent product boundary**: once operator authentication exists as its own accepted project and a scoped host-provisioning agent can perform the host half under attributable identity, a UI slice may wrap the same ceremony the CLI performs. The provisioning CLI is therefore designed as the ceremony surface such a slice would call — receipts in, opaque IDs out — so the future UI is additive.

### 9. Frontend context is three orthogonal dimensions

Provider, clerk and the Trader/Operator lens are orthogonal: `?lens=trader|operator` remains the lens representation; the app creates no `?clerk=` parameter and no clerk preference key; canonical lane routes include broker and clerk identity; state is keyed by `(broker, clerk_id)` with independent resources, loading, errors, retries, streams and circuit state. Deep links to unknown, retired, inaccessible or wrong-provider clerks fail in place and never redirect to another lane. One lane's failure is explicit partial failure, never omission, substitution, retargeting or global failure of healthy lanes. There is no fleet-wide mutation endpoint.

### 10. Delivery is phased with the Alpaca adapter as the first and only production provider in this program

The fleet spine and fake-provider conformance land first (this ADR plus `app/broker/fleet/`); the Alpaca adapter and the coordinator/agent role split, the explicit route cutover, the frontend migration, the two-clerk rollout and the provider-scalability conformance follow as separately gated phases per the PRD's delivery plan. Only Alpaca is enabled in production by this program. Every later real broker — Tradier, Webull or otherwise — requires its own accepted authority and product slice with its own credentials, custody, safety, evidence, recovery and tests. Deprecated IBKR bot-control and navigation surfaces remain retired and are not used as an implementation model.

## Consequences

- Alpaca Paper and Live become concurrently operable through one observable control plane while their processes and storage stay isolated; a Paper process, volume, database, credential or stream failure cannot disable, restart, retarget or corrupt the Live lane.
- The coordinator's registry becomes a new protected artifact: registry recovery must not reconstruct custody from aggregate data or silently mint replacement clerk identities, identity conflict keeps routing disabled, and rollback never merges volumes, moves custody, deletes registry history, releases assignments or copies arming ledgers.
- Migration of the existing single Alpaca clerk issues broker, clerk and volume identities, verifies the existing volume, and imports the effective account assignment without rewriting historical sealed records; `combined` mode preserves exact historical single-installation behavior during migration, and independent single-clerk routing remains a supported rollback posture.
- The owner accepts the operational cost of host ceremonies for enrollment, retirement, reassignment and registry recovery, and the delivery cost of the phase gates (duplicate volume and account-assignment failures under concurrency, fake-provider boundary, secret-absence, fault-isolation and frontend explicit-identity evidence) before the second lane goes live.
- Frontend state, services and guards gain a `(broker, clerk_id)` keying dimension; the shared lens kernel is untouched by it.

## Anti-patterns rejected

- A generic trading clerk that normalizes provider differences into fabricated feature parity.
- Releasing an account assignment on heartbeat loss, lease expiry or coordinator restart — any path where a partition manufactures a second writer.
- Resolving a command's destination from browser selection, card state, stored preference, "first healthy clerk", or any mutable context.
- The coordinator computing or combining balances, positions, P&L, exposure or risk across lanes.
- Storing lane custody, configuration or credential material — values, fragments, lengths, environment names, secret-derived hashes — in the fleet registry, or letting any of them cross the API or logs.
- A fleet-wide Start, Stop, Flatten, Deploy, Archive, Restart, Arm or Reassign endpoint.
- Inferring a capability for one provider because another provider declares it.
- Retiring, restoring or rebinding a clerk from the browser.
- Building, restoring or consulting deprecated IBKR bot-control surfaces as the model for any of this.

## References

- [`docs/prds/2026-09-12-multi-broker-clerk-control-plane.md`](../../prds/2026-09-12-multi-broker-clerk-control-plane.md) — the PRD this ADR accepts; §14 phases, §19 owner decisions, §20 acceptance criteria.
- [`docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json`](../../audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json) — plan tournament receipt.
- ADR 0060 — superseded in Decision 5 only, as it itself anticipated.
- ADR 0059 — arming and envelope semantics, retained per clerk.
- ADR 0035 / ADR 0037 — custody authority, unchanged.
- `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)" — domain terminology.
