# PRD — Multi-broker clerk fleet control plane

- **Date:** 2026-09-12
- **Status:** Ready for owner decisions and issue approval
- **Initial product surface:** Canonical Alpaca Broker V2 under `/brokers/alpaca/...`
- **Platform direction:** A narrow broker-neutral fleet control plane with distinct broker-owned clerk implementations
- **Delivery posture:** Alpaca Paper and Live first; real additional brokers land only through separately accepted product and authority slices
- **Source:** Two-player Sol/GLM plan tournament; audit receipt in `docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json`
- **Authority:** Python owns broker execution, numerical decisions, custody, risk, and provider-specific admission. Angular owns presentation and intent capture only. .NET, where present on a route, is transport only.

---

## 1. Executive summary

The current Alpaca runtime is intentionally single-clerk: one primary
process-global `ActiveClerkRuntime`, one effective broker configuration, one
`ALPACA_CLERK_DIR`, and one worker composition per installation. Concurrent
Paper and Live operation therefore requires separate installations. The
frontend can present configuration and the Trader/Operator lenses, but it has
no durable clerk identity with which to address multiple isolated execution
lanes.

This PRD introduces a broker clerk fleet control plane. A small Python
coordinator owns clerk identity, discovery, exact routing, broker-qualified
account-assignment fencing, health, and routing correlation. Execution remains
inside separately deployed, broker-specific clerk agents. Alpaca Paper and
Alpaca Live run as different processes against different physical volumes,
profile databases, custody databases, credentials, leases, streams, and
recovery units.

The shared control plane is broker-neutral; the trading clerks are not. A
future Tradier integration supplies a `TradierClerkAgent`, and a future Webull
integration supplies a `WebullClerkAgent`. Each broker implementation owns its
SDK client, credential convention, account verification, commands,
capabilities, custody semantics, receipts, risk or arming policy, recovery, and
tests. No broker inherits Alpaca behavior merely by registering a string.

All public reads, commands, streams, deep links, and returned navigation
references carry an immutable provider identifier and an opaque backend-issued
`clerk_id`. A browser selection is never command authority. A durable fleet
assignment fence prevents two clerk volumes from acquiring the same external
account for the same broker. Assignment ownership does not expire on heartbeat
loss, so a network partition cannot manufacture a second broker writer.

The initial delivery implements the fleet spine and the canonical Alpaca
Broker V2 adapter only. Test-only fake providers prove the extension boundary.
Real Tradier, Webull, or other broker surfaces require separate accepted
decisions and provider-owned deliveries. Deprecated IBKR bot-control and
navigation surfaces remain retired and are not used as an implementation
model.

## 2. Product problem

### 2.1 One installation can address only one effective clerk

`PythonDataService/app/main.py` composes one Alpaca binding, broker client,
active runtime, and background-consumer set. The current broker-configuration
runtime is rooted in one Clerk directory, while accepted ADR 0060 deliberately
defines one installation selection and no worker identity.

This makes Paper and Live isolation strong but operationally fragmented. One
browser cannot safely observe and address both lanes through a single control
plane because neither the routing contract nor the product URLs carry a stable
clerk identity.

### 2.2 Separate volumes alone do not prevent duplicate authority

The existing execution lease excludes writers sharing one account custody
volume. Two different Clerk volumes can still be configured with credentials
for the same external account. Without a fleet-level assignment fence, both
could believe they are the valid writer.

The platform therefore needs both:

1. a unique physical volume per clerk; and
2. a durable broker-qualified account assignment that spans those volumes.

### 2.3 A generic trading clerk would erase provider safety boundaries

Alpaca-specific authority includes account verification, configuration,
custody, execution leases, activation, Live arming, receipt and recovery
semantics. Tradier and Webull may expose different account identities,
capabilities, order behavior, evidence, and risk controls.

A reusable fleet layer must not normalize those differences into fabricated
feature parity. It may route an operation declared by a provider adapter, but
it may not translate, emulate, or decide that operation.

### 2.4 Implicit frontend context is unsafe for commands

A selected card, query parameter, stored preference, account lookup, or
"first healthy clerk" rule can change between rendering a command and
submitting it. If submission reads that mutable selection, an action prepared
for Paper could be sent to Live.

The command must instead retain the exact broker, clerk, account, entity,
capability, idempotency identity, and binding generation from the server
resource from which it was opened.

## 3. Goals

1. Operate Alpaca Paper and Live concurrently through one observable control
   plane while keeping their execution processes and storage isolated.
2. Give every clerk a durable opaque identity and a unique, verifiable physical
   volume identity.
3. Require explicit broker and clerk identity on every clerk-scoped public
   read, mutation, stream, deep link, and returned reference.
4. Prevent two clerks from reserving or activating the same external account
   for the same broker.
5. Preserve each clerk's configuration, generations, idempotency, custody,
   Live authority, audit, failure, backup, and recovery independently.
6. Contain process, broker, stream, credential, database, and volume failures
   to the affected clerk.
7. Keep the shared fleet layer free of trading math and provider-specific
   safety decisions.
8. Define a conformance boundary for future broker-owned clerk agents without
   implementing a real second broker in this delivery.
9. Migrate the existing single Alpaca clerk without changing historical sealed
   or hash-chained evidence.
10. Preserve safe same-clerk last-effective recovery when the coordinator is
    unavailable while refusing new assignments and browser commands.

## 4. Non-goals

- Implementing real Tradier, Webull, or another new broker integration.
- Restoring, expanding, or using deprecated IBKR bot-control or navigation
  surfaces.
- Multi-user authentication, RBAC, or remote human identity.
- Multi-host clerk agents before transport identity, TLS, and partition
  semantics are separately accepted.
- A generic order, risk, custody, arming, or recovery implementation shared by
  all brokers.
- Cross-clerk or cross-broker financial aggregation in Angular, .NET, or the
  fleet coordinator.
- Fleet-wide Start, Stop, Flatten, Deploy, Archive, Restart, Arm, Reassign, or
  other bulk mutations.
- Browser enrollment, retirement, remount, restore, reassignment, process
  restart, or Live arming.
- Changing existing Alpaca custody, activation, arming, sealed-envelope, or
  historical receipt hashes.
- Cross-provider configuration cloning in the first release.

## 5. Users and jobs

### Trader

- See Paper and Live as distinct lanes with unmistakable provider, mode,
  account, and health identity.
- Open the exact lane needed without changing the Trader/Operator lens.
- Prepare and submit a permitted action knowing its target cannot change while
  the dialog is open.
- Continue using a healthy lane when another lane fails.

### Operator

- Inspect lifecycle, configuration generation, binding generation, custody,
  lease, arming, health, and routing evidence per clerk.
- Diagnose a failed clerk without marking all other clerks unhealthy.
- Provision, restore, retire, or reassign a clerk through explicit host
  ceremonies.
- Prove that every active clerk has its own mounted volume and broker-qualified
  account assignment.

### Broker integration developer

- Implement one concrete broker clerk behind a small conformance protocol.
- Declare only the capabilities the broker implementation actually supports.
- Keep broker-specific clients, credentials, verification, custody, risk,
  evidence, and recovery outside the generic fleet layer.

## 6. Terminology

- **Broker/provider** — immutable code-owned provider identity such as
  `alpaca`. A provider name never becomes a caller-supplied endpoint.
- **Broker clerk agent** — one provider-specific process controlling one clerk
  lane and one physical volume.
- **Clerk** — a durable execution lane identified by an opaque backend-issued
  `clerk_id`.
- **Fleet coordinator** — the broker-neutral Python control plane for identity,
  discovery, assignment, routing, health, and audit correlation. It is not an
  execution authority.
- **Clerk volume** — the distinct physical mounted root containing one clerk's
  configuration, custody, ledgers, operational files, backups, and recovery
  evidence.
- **Volume identity** — a backend-issued `volume_id` plus an external
  nonsecret mount attestation used to detect copied or mis-mounted roots.
- **Routing epoch** — a monotonically increasing identity for an agent
  registration.
- **Effective binding generation** — a clerk-local generation that changes
  only when the effective profile, revision, or account changes.
- **Account assignment** — the fleet reservation for
  `(broker, canonical_external_account_id)`.
- **Capability** — a closed typed operation declared by a concrete provider
  adapter. It is not inferred from another provider.
- **Lens** — the existing Trader/Operator presentation dimension. It remains
  independent of provider and clerk context.

## 7. Product and safety principles

1. **One clerk, one process, one physical volume.** Isolation is structural,
   not a naming convention.
2. **Volume identity before authority.** No database writer or broker client
   opens until mount identity passes.
3. **Explicit target everywhere.** Missing broker or clerk identity fails
   closed.
4. **Two fences are required.** Local custody leases protect one volume;
   broker-qualified fleet assignments protect accounts across volumes.
5. **Liveness never transfers ownership.** Heartbeat loss cannot grant a
   second writer.
6. **Provider implementations own provider semantics.** The generic spine
   routes and verifies; it does not trade.
7. **Capabilities are evidence.** Unsupported actions are unavailable, never
   emulated.
8. **Intent identity is immutable.** Submission uses the resource identity
   captured when the action opened.
9. **Partial reads keep provenance.** One failed lane is reported explicitly
   without omission or substitution.
10. **Historical evidence is preserved.** Fleet correlation is additive and
    external to sealed lane histories.

## 8. Target architecture

```text
Angular broker surfaces
        |
        | explicit broker + clerk_id REST/SSE
        v
Python fleet coordinator
  - broker/clerk/volume registry
  - broker-qualified account assignments
  - immutable agent clients
  - routing and response verification
  - lifecycle and health
  - routing receipts
  - read-only provenance aggregation
        |
        +----------------------------+
        |                            |
        v                            v
Alpaca Paper Clerk              Alpaca Live Clerk
  process A                       process B
  physical volume A              physical volume B
  profile/custody/ledger A        profile/custody/ledger B
  credentials/lease A             credentials/lease B

Future, separately approved:
  TradierClerkAgent
  WebullClerkAgent
  OtherBrokerClerkAgent
```

The initial topology uses one private host/container network. Agent endpoints
come only from deployment-owned configuration. The browser and public API can
never supply an arbitrary agent URL.

## 9. Functional requirements

### 9.1 Broker-specific clerk implementations

- **FR-001:** The fleet package must define a narrow Python provider-adapter
  protocol and a code-owned production adapter registry.
- **FR-002:** The first production adapter must be Alpaca. Test-only fake
  adapters must be injectable without entering the production registry.
- **FR-003:** A provider adapter must declare its immutable provider ID,
  adapter version, canonical account-ID function, health and binding
  observations, typed capabilities, route catalog, and served-context
  validation.
- **FR-004:** Provider-specific clients, credentials, account verification,
  commands, custody, receipts, risk, arming, recovery, and tests must remain in
  the provider implementation.
- **FR-005:** Generic fleet modules must not import Alpaca risk, custody,
  execution, arming, or recovery implementations.
- **FR-006:** An unknown production provider or undeclared capability must
  fail closed.

### 9.2 Clerk and session identity

- **FR-010:** The backend must issue a stable, opaque, non-semantic
  `clerk_id`; callers must not mint or parse it.
- **FR-011:** Each clerk must have an immutable `broker`, durable `worker_key`,
  durable `volume_id`, ephemeral `agent_instance_id`, monotonic
  `routing_epoch`, and clerk-local `effective_binding_generation`.
- **FR-012:** `worker_key`, internal endpoint, and internal credential must
  never cross the public API.
- **FR-013:** Clerk retirement is terminal in the registry; IDs are never
  recycled.
- **FR-014:** Every descriptor, session, assignment, routing receipt, health
  result, and structured route log must carry `broker` and `clerk_id`.

### 9.3 Physical volume isolation

- **FR-020:** Every production clerk must receive a distinct named physical
  volume and canonical mounted root.
- **FR-021:** Alpaca Paper and Live may not use writable subdirectories of the
  same mounted production volume.
- **FR-022:** A clerk volume must contain only that clerk's profile database,
  selection row, custody databases, activation and arming ledgers, receipts,
  mirrors, fences, streams, operational files, backup manifests, and recovery
  state.
- **FR-023:** The fleet coordinator volume must contain no lane configuration,
  custody, order, fill, position, activation, arming, callback, or operational
  execution data.
- **FR-024:** Provisioning must write a versioned identity marker with broker,
  clerk, volume, attestation, and `created_at_ms` fields.
- **FR-025:** Before opening a database writer or broker client, an agent must
  prove canonical root, mount, marker, deployment expectation, and registry
  identity.
- **FR-026:** Missing, duplicated, copied, symlinked, noncanonical, or
  mis-mounted volume identity must fail before authority opens.
- **FR-027:** Each volume must be independently backed up, restored, verified,
  and quarantined.

### 9.4 Registry and discovery

- **FR-030:** The coordinator must use a dedicated schema-versioned SQLite
  registry on its own control volume, following repository WAL, migration-lock,
  integrity, and local-filesystem patterns.
- **FR-031:** Registry storage must include clerks, sessions,
  broker-qualified account assignments, routing receipts, and fleet metadata.
- **FR-032:** Active clerks must have unique worker keys, volume IDs, and volume
  attestations.
- **FR-033:** The registry must expose a broker-neutral read-only directory
  that returns every lane's broker, clerk identity, lifecycle, last observation,
  generations, declared capabilities, volume identity, and provider-authored
  summary.
- **FR-034:** The generic directory must not calculate or combine balances,
  positions, P&L, exposure, or risk.

### 9.5 Process and credential isolation

- **FR-040:** Runtime composition must support explicit `combined`,
  `fleet_coordinator`, and `clerk_agent` roles.
- **FR-041:** The coordinator role must construct no provider broker client,
  active Clerk authority, trade-update consumer, or execution worker.
- **FR-042:** A clerk-agent process must load exactly one provider adapter and
  one clerk volume.
- **FR-043:** Each agent must have a unique internal service credential and
  only the broker credentials allowed for that clerk.
- **FR-044:** Credential resolution must be a code-owned mapping from
  `(broker, clerk_id, credential_slot)` to environment-only material.
- **FR-045:** Requests must not supply environment-variable names, secret
  paths, credential values, or agent endpoints.
- **FR-046:** The coordinator must terminate the browser installation secret;
  it must never forward that secret to an agent.
- **FR-047:** Secret values, fragments, lengths, environment names, and
  secret-derived hashes must not enter registries, APIs, logs, examples, or
  error messages.

### 9.6 Broker-qualified account assignment

- **FR-050:** The fleet assignment key must be
  `(broker, provider_canonical_external_account_id)`.
- **FR-051:** The provider adapter alone canonicalizes and verifies an external
  account ID; the generic registry treats it as opaque.
- **FR-052:** Only one reserved or effective assignment may exist per key.
- **FR-053:** Assignment generations must be monotonic and updated
  transactionally.
- **FR-054:** A heartbeat timeout or unreachable clerk must not release its
  assignment.
- **FR-055:** Reassignment must be an offline host ceremony proving the old
  agent and volume are offline, credentials are isolated, and provider-specific
  obligations are clear.
- **FR-056:** A network partition must never enable two clerks for one
  broker-qualified account.

### 9.7 Startup, Apply, and handover

- **FR-060:** Each clerk must preserve immutable profile revisions, Stage →
  Apply (`202`, intent only) → controlled restart, worker-only effective
  acknowledgement, one-shot Apply, last-effective crash recovery, and stale
  selection-generation fencing.
- **FR-061:** Startup must verify provider, clerk, and volume identity before
  loading a candidate binding.
- **FR-062:** The provider adapter must perform existing read-only account,
  mode, and pin verification before fleet assignment reservation.
- **FR-063:** The clerk must reserve its account before opening custody and its
  execution lease.
- **FR-064:** The coordinator must not make a session command-routable until
  the worker has acknowledged the local binding and confirmed the fleet
  assignment with its binding generation.
- **FR-065:** Crash recovery must be idempotent for the same clerk and must not
  transfer a reservation.
- **FR-066:** During coordinator unavailability, the verified original clerk
  may recover its already-confirmed last-effective binding. New assignment,
  different-account Apply, enrollment, reassignment, and browser commands must
  fail closed.

### 9.8 Explicit routing and response verification

- **FR-070:** Public provider routes must contain both `{broker}` and
  `{clerk_id}` as path segments.
- **FR-071:** The coordinator must resolve `clerk_id` and then verify that the
  path broker equals the immutable clerk broker.
- **FR-072:** The broker string must never select an arbitrary network
  endpoint; endpoints come only from deployment-owned registry configuration.
- **FR-073:** Every command must carry broker, clerk, exact account when
  applicable, entity identity, typed capability, durable idempotency or
  lifecycle identity, and expected effective-binding generation.
- **FR-074:** The coordinator must use an immutable client bound to one
  broker/clerk/endpoint/internal-credential tuple. It must never retarget a
  shared mutable client.
- **FR-075:** The agent must validate provider, clerk, volume, internal
  credential, routing epoch, effective account, binding generation,
  capability, and provider safety gates before a mutation.
- **FR-076:** Every read response and stream event must echo broker, clerk,
  observation timestamp, routing epoch, and binding generation.
- **FR-077:** Backend-authored paths and references must be structured or fully
  broker/clerk scoped; an unscoped product URL must not escape.
- **FR-078:** Mutation retries must retain the same broker, clerk, and
  idempotency identity and must not cross a routing-epoch change automatically.
- **FR-079:** A routing receipt correlates an attempt but never replaces the
  provider clerk's execution or custody receipt.

### 9.9 Lifecycle, health, and partial aggregation

- **FR-080:** Fleet lifecycle must distinguish `provisioned`, `starting`,
  `ready`, `degraded`, `unreachable`, `draining`, and `retired` from provider
  execution readiness.
- **FR-081:** Historical effective acknowledgement must not be presented as
  current liveness.
- **FR-082:** Health, retry, timeout, stream, and circuit state must remain per
  clerk.
- **FR-083:** Fleet aggregation must be read-only and return an ordered
  provenance-preserving result for every requested lane.
- **FR-084:** One lane's failure must be explicit partial failure, never
  omission, substitution, retargeting, or global failure of healthy lanes.
- **FR-085:** There must be no fleet-wide mutation endpoint.

### 9.10 Frontend information architecture

- **FR-090:** Frontend context must model provider, clerk, and Trader/Operator
  lens as three orthogonal dimensions.
- **FR-091:** `?lens=trader|operator` remains the lens representation. The app
  must not create `?clerk=` or a clerk preference key.
- **FR-092:** Canonical lane routes must include broker and clerk identity.
- **FR-093:** State must be keyed by `(broker, clerk_id)` with independent
  resources, loading, errors, retries, streams, and circuit state.
- **FR-094:** A command intent must freeze broker, clerk, account, entity,
  capability, idempotency identity, and binding generation from the rendered
  server resource when the action opens.
- **FR-095:** Submission must not read a global selected broker or clerk.
- **FR-096:** Unknown, retired, inaccessible, or wrong-provider deep links must
  fail in place and never redirect to another lane.
- **FR-097:** Provider capability evidence must drive unavailable actions;
  Angular must not infer capability parity.
- **FR-098:** Code-like reasons use `receiptLabel`, backend-authored prose is
  preserved, tradable symbols use `app-asset-identity`, and timestamps use the
  shared timestamp renderer.
- **FR-099:** Generalization must not create an IBKR navigation section,
  component, or alternative product surface.

### 9.11 Migration and compatibility

- **FR-100:** Existing single-clerk Alpaca configuration and custody stay on
  their current physical volume.
- **FR-101:** Migration must issue broker, clerk, and volume identities, verify
  the existing volume, and import the effective account assignment without
  rewriting historical sealed records.
- **FR-102:** `combined` mode may preserve exact historical single-installation
  behavior during migration, but fleet mode must not expose an implicit command
  default.
- **FR-103:** Angular must move completely to explicit routes before implicit
  mutations are disabled.
- **FR-104:** A second Alpaca clerk may be enrolled only after cross-target,
  assignment, volume, generation, and fault-isolation tests pass.
- **FR-105:** Legacy unscoped reads must be retired after a measured
  compatibility window.
- **FR-106:** Cross-provider profile cloning remains excluded. A future clone
  must be provider-authored, copy only explicitly transferable nonsecret data,
  clear credentials and account pins, and require fresh verification.

### 9.12 Recovery and rollback

- **FR-110:** Provider custody recovery remains a provider-owned offline
  ceremony.
- **FR-111:** Volume restore/rebind and fleet registry recovery are separate
  host-only ceremonies.
- **FR-112:** Registry recovery must not reconstruct custody from aggregate
  data or mint replacement clerk identities silently.
- **FR-113:** Identity conflict must keep routing disabled.
- **FR-114:** Rollback must not merge volumes, move custody, delete registry
  history, release assignments, or copy arming ledgers.
- **FR-115:** Independent single-clerk routing remains a supported rollback
  posture after the second lane is introduced.

## 10. Contract shapes

### 10.1 Broker-neutral directory

`GET /api/broker-clerks`

```json
{
  "observed_at_ms": 1789250000000,
  "clerks": [
    {
      "broker": "alpaca",
      "clerk_id": "clrk_opaque",
      "display_label": "Paper research",
      "lifecycle_state": "ready",
      "volume_id": "vol_opaque",
      "last_seen_at_ms": 1789249999000,
      "routing_epoch": 8,
      "effective_binding_generation": 4,
      "capabilities": ["account_read", "bot_panel", "bot_action"],
      "provider_summary": {
        "endpoint_mode": "paper",
        "authority_state": "real_paper"
      }
    }
  ]
}
```

`provider_summary` is a provider-authored typed union. The fleet directory does
not interpret its financial contents.

### 10.2 Provider routes

```text
GET /api/brokers/{broker}/clerks
GET /api/brokers/{broker}/clerks/{clerk_id}
GET /api/brokers/{broker}/clerks/{clerk_id}/account
GET /api/brokers/{broker}/clerks/{clerk_id}/positions
GET /api/brokers/{broker}/clerks/{clerk_id}/orders
    /api/brokers/{broker}/clerks/{clerk_id}/configuration/...
    /api/brokers/{broker}/clerks/{clerk_id}/accounts/{account_id}/bots/...
    /api/brokers/{broker}/clerks/{clerk_id}/accounts/{account_id}/custody/...
    /api/brokers/{broker}/clerks/{clerk_id}/accounts/{account_id}/gallery/...
```

A route-contract test must prove that existing wildcard routes do not shadow
the new literal clerk surface.

### 10.3 Command context

```json
{
  "capability": "bot_action",
  "idempotency_key": "opaque-durable-key",
  "expected_effective_binding_generation": 4,
  "target": {
    "account_id": "provider-account-id",
    "entity_id": "strategy-or-order-id"
  }
}
```

Broker and clerk remain required path identities. Responses echo all target
identity and return the provider clerk's durable receipt reference.

### 10.4 Stable refusal families

- `broker_not_supported`
- `clerk_not_found`
- `clerk_broker_mismatch`
- `clerk_unreachable`
- `clerk_identity_mismatch`
- `clerk_volume_identity_missing`
- `clerk_volume_identity_mismatch`
- `clerk_volume_already_registered`
- `clerk_volume_mount_unproven`
- `clerk_volume_clone_detected`
- `clerk_binding_generation_conflict`
- `clerk_account_mismatch`
- `clerk_assignment_conflict`
- `broker_clerk_capability_unavailable`
- `broker_and_clerk_required`
- `clerk_routing_outcome_unknown`

HTTP status and retry semantics must be pinned in the public contract. Operator
copy remains backend-authored; Angular renders code-like values through the
shared label path.

## 11. Storage model

Suggested coordinator records:

```text
fleet_meta(
  schema_version,
  registry_id,
  ...
)

clerks(
  clerk_id,
  broker,
  worker_key,
  display_label,
  volume_id,
  volume_attestation_id,
  lifecycle_state,
  created_at_ms,
  retired_at_ms
)

clerk_sessions(
  broker,
  clerk_id,
  agent_instance_id,
  routing_epoch,
  started_at_ms,
  last_seen_at_ms,
  reported_binding_generation,
  reported_account_id,
  reported_state
)

account_assignments(
  broker,
  canonical_external_account_id,
  clerk_id,
  assignment_generation,
  state,
  effective_profile_id,
  effective_revision,
  recorded_at_ms
)

routing_receipts(
  correlation_id,
  broker,
  clerk_id,
  operation_kind,
  nonsecret_target_ref,
  idempotency_key,
  state,
  upstream_receipt_ref,
  created_at_ms,
  updated_at_ms
)
```

Exact DDL belongs to the implementation contract. All timestamp columns and
wire fields use `int64 ms UTC`; textual datetime storage is prohibited.

## 12. Failure model

| Failure | Required result |
|---|---|
| Paper process exits | Only Paper enters restart/recovery; Live remains unchanged. |
| Clerk volume unavailable | That clerk fails before a writer opens; other volumes remain available. |
| Volume copied or mis-mounted | Identity check refuses before database or broker construction. |
| Provider credentials missing | Only that clerk is unavailable; no credential fallback. |
| Broker stream fails | Only the provider clerk's existing stream policy runs. |
| Agent response names wrong broker/clerk/account/epoch | Coordinator isolates the client and refuses the response. |
| Coordinator unavailable | Running clerks continue; new browser commands and assignments refuse. |
| Heartbeat expires | Clerk becomes unreachable; account assignment remains owned. |
| Network partition | No assignment is transferred and no second writer is admitted. |
| One lane times out during fleet read | Response contains an explicit partial result for that lane. |
| Unsupported provider capability | Typed refusal; never translation or downgrade. |
| Registry identity conflict | Routing remains disabled pending host recovery. |

## 13. Frontend information architecture

Initial Alpaca routes:

```text
/brokers/alpaca
/brokers/alpaca/clerks/:clerkId
/brokers/alpaca/clerks/:clerkId/configuration
/brokers/alpaca/clerks/:clerkId/accounts/:accountId/bots
/brokers/alpaca/clerks/:clerkId/accounts/:accountId/bots/:sid
/brokers/alpaca/clerks/:clerkId/accounts/:accountId/gallery
```

State shape:

```text
FleetDirectoryState
  Map<(broker, clerk_id), BrokerClerkLaneState>
    descriptor and capabilities
    desk/account resources
    configuration resources
    panel/gallery resources
    loading/error/retry/stream/circuit state
```

The Alpaca root can display Paper and Live simultaneously. A future
broker-neutral directory may list provider families, but a real provider gets
its own approved product surface rather than a misleading universal trading
desk.

Consequential confirmations identify provider, lane label, actual endpoint or
authority mode, exact account, and clerk identity. Switching the
Trader/Operator lens changes presentation and lazy evidence reads only.

## 14. Delivery plan

### Phase 0 — Authority and owner decisions

- Accept the owner decisions in §19.
- Add a multi-clerk ADR that supersedes only ADR 0060's one-worker and
  no-worker-identity limitation.
- Update the broker-configuration contract, Alpaca desk design, operator
  runbook, documentation authority, engine authority map, and any affected
  math-authority row in the same delivery required by repository governance.
- Freeze sealed-record changes as excluded.

**Gate:** approved identity, volume, routing, assignment, outage, and provider
extension contracts.

### Phase 1 — Generic fleet spine

- Fleet registry schema, store, and service.
- Broker, clerk, session, volume, and generation identities.
- Broker-qualified account assignments.
- Immutable agent-client registry.
- Provider protocol and code-owned provider registry.
- Broker-neutral directory, lifecycle, health, and routing receipts.
- Host provisioning, retirement, volume replacement, verification, and
  recovery commands.

**Gate:** registry contains no custody; duplicate volume and account assignment
fail under concurrency; fake-provider boundary and secret-absence tests pass.

### Phase 2 — Alpaca adapter and agent role

- Implement the Alpaca provider adapter.
- Split startup into coordinator, agent, and transitional combined roles.
- Keep the process-global `ActiveClerkRuntime` only inside one Alpaca agent.
- Verify volume identity before profile or custody access.
- Add internal service authentication and broker/clerk/epoch validation.
- Preserve Alpaca configuration, binding, lease, arming, custody, receipt, and
  recovery semantics.

**Gate:** one Alpaca clerk boots last-effective from its verified volume;
copied and mis-mounted roots fail before authority opens.

### Phase 3 — Explicit Alpaca routes and contracts

- Add broker/clerk-scoped wrappers for account, configuration, bot panel,
  custody, recovery, gallery, streams, Deploy, and manual-order surfaces.
- Remove default-account resolution from canonical fleet commands.
- Inventory and scope all returned navigation and evidence references.
- Export OpenAPI and regenerate TypeScript.

**Gate:** wrong broker, clerk, volume, account, epoch, capability, and binding
generation all fail closed.

### Phase 4 — Frontend cutover

- Add the Alpaca clerk directory and route-scoped context.
- Migrate `BrokersService`, `BrokerV2PanelService`, gallery state,
  `BrokerConfigurationService`, Alpaca desk, bot list and panel, Deploy,
  recovery, and manual-order calls.
- Preserve the shared Trader/Operator lens kernel.
- Disable implicit public Alpaca mutations in fleet mode.

**Gate:** browser network evidence shows explicit broker and clerk identity on
every canonical read, command, and stream.

### Phase 5 — Two isolated Alpaca clerks

- Provision separate Paper and Live external volumes.
- Verify distinct mount identities, roots, profile DBs, custody DBs, leases,
  backups, credentials, and processes.
- Enable read-only aggregation.
- Canary Paper mutations.
- Enroll Live read-only, then enable only existing Live-eligible commands after
  the Live safety matrix passes.
- Keep restart, arming, reassignment, recovery, and provisioning host-only.

**Gate:** Paper process, volume, database, broker, credential, and stream
failures leave Live operational.

### Phase 6 — Provider scalability conformance

- Run N clerks across at least two test-only fake provider adapters.
- Prove provider-qualified account assignment, wrong-provider refusal,
  capability differences, distinct volumes, independent state, and partial
  aggregation.
- Keep fake providers out of the production adapter registry.

**Gate:** generic fleet modules pass conformance without importing Alpaca
execution, risk, custody, or recovery modules.

### Phase 7 — General availability and future providers

- Retire implicit fleet routes after the compatibility window.
- Publish backup, restore, retirement, reassignment, and registry-recovery
  runbooks.
- Preserve independent single-clerk routing as rollback.
- Require a separate accepted authority and product slice for each real
  `TradierClerkAgent`, `WebullClerkAgent`, or later provider.

## 15. Test and qualification matrix

### Volume and storage

- Paper and Live resolve to distinct canonical roots and named volume sources.
- Each root contains only its own configuration and custody data.
- Coordinator volume contains no lane custody data.
- Duplicate volume ID or attestation cannot register.
- Missing marker, wrong provider, wrong clerk, copied marker, symlinked root,
  ordinary image directory, and mis-mounted volume fail before a writer opens.
- Backup and restore of one clerk do not mount or write another clerk.

### Generic fleet contract

- Opaque IDs remain stable and non-semantic.
- Provider association is immutable.
- Unknown production providers refuse.
- Route provider must match clerk provider.
- Account uniqueness is provider-qualified.
- Identical raw account IDs can exist safely under different providers.
- Generations are monotonic.
- Undeclared capabilities refuse.
- All timestamps are bounded `int64 ms UTC` and wire fields are `snake_case`.
- Secret canaries appear nowhere in registry, API, logs, exceptions, or
  contract examples.
- Internal agent routes cannot be reached through public wildcard routing.

### Fake-provider conformance

Using `fake_alpha` and `fake_beta`:

- Run N clerks across both providers with distinct temporary roots.
- Refuse a duplicate canonical account within one provider.
- Permit identical raw account IDs across different providers.
- Refuse a fake-alpha clerk on a fake-beta route.
- Prove provider clients, credentials, health, commands, and state share no
  mutable object.
- Declare different capabilities and refuse unsupported operations.
- Preserve `(broker, clerk_id)` provenance in partial aggregation.
- Kill or corrupt one provider clerk without mutating another.
- Prove the generic spine imports no provider risk or custody implementation.

### Alpaca integration

- Run Paper and Live agents against separate fake Alpaca ports and Clerk roots.
- Prove Paper mutations write only Paper custody and broker counters.
- Refuse Live-to-Paper endpoint-map swaps.
- Keep identical idempotency keys independent across clerks.
- Retry lost responses against the same broker, clerk, and key.
- Reject wrong broker, clerk, volume, account, epoch, or binding generation.
- Race the same Alpaca account and produce one winner plus one durable refusal.
- Preserve independent Stage, Apply, refused Apply, and last-effective recovery.
- Verify historical envelope hashes and arming seals unchanged.

### Fault and concurrency

- Kill, pause, timeout, poison, or corrupt Paper while Live succeeds.
- Restart the coordinator while retaining registry and routing receipts.
- Remove the coordinator while running clerks continue and new assignments
  refuse.
- Refuse duplicated agent sessions and cloned worker keys.
- Prove network partitions do not release ownership.
- Keep circuit breakers and stream reconnects per clerk.
- Preserve existing per-clerk Stage/Apply generation conflicts.
- Refuse retirement or reassignment while process, volume, credentials, or
  obligations are unproven.

### Frontend and end to end

- Alpaca root shows Paper and Live concurrently.
- Provider, clerk, and lens remain orthogonal.
- Deep links retain exact path context without clerk storage.
- Wrong-provider, unknown, and retired links never fall through.
- One lane error does not clear or disable another lane.
- Commands use resource-captured identity, not current selection.
- Unsupported capabilities render from backend evidence.
- Trader lens causes no Operator evidence fetch.
- Accessibility, keyboard, focus, receipt labels, asset identities, and shared
  timestamps remain correct.
- Trace browser request → coordinator receipt → exact provider agent → exact
  lane receipt.
- No deprecated IBKR product route or component is introduced.

## 16. Verification commands

From `PythonDataService`:

```powershell
$env:DATA_PLANE_CONTROL_SECRET=""
..\.venv\Scripts\python.exe -m pytest tests/broker/fleet tests/broker_configuration tests/broker/alpaca/clerk/test_active_authority.py tests/broker/v2panel tests/routers/test_broker_v2_gallery.py tests/routers/test_alpaca_clerk_sqlite.py tests/contracts/test_broker_configuration_route_prefix.py
..\.venv\Scripts\python.exe -m scripts.run_fast_tests
ruff check app/ tests/
python scripts/export_openapi_contract.py --check
```

From `Frontend`:

```powershell
npm run codegen:check
npm run lint
npm run test:guards
npm test
npx playwright test tests/e2e/alpaca-multi-clerk.spec.ts
```

Add a Compose acceptance script that:

1. creates separate coordinator, Paper, and Live named volumes;
2. records and compares their physical mount identities;
3. proves the coordinator volume contains no lane custody;
4. exercises wrong-provider, wrong-clerk, wrong-account, stale-generation,
   cloned-volume, and mis-mounted-volume refusals;
5. kills Paper; and
6. proves a Live read and an eligible Live command still succeed.

## 17. Observability

Structured logs and health projections carry bounded-cardinality:

- `broker`
- `clerk_id`
- `volume_id`
- `agent_instance_id`
- `routing_epoch`
- `effective_binding_generation`
- `correlation_id`
- operation or capability
- outcome
- latency

Track lifecycle, heartbeat age, volume verification, route latency, timeout and
identity mismatches, assignment conflicts, outcome-unknown receipts, declared
capabilities, provider-authored custody and lease state, configuration state,
stream reconnects, and circuit state independently per clerk.

A Paper alert must not mark Live unhealthy. A provider-family alert must not
mark another provider unhealthy. Logs must never contain authorization headers,
broker secrets, environment names, or arbitrary request URLs.

## 18. Rollout and rollback

Roll out the fleet spine around one existing Alpaca clerk first. Verify
single-clerk parity, migration idempotency, and rollback before adding another
lane. Provision Paper and Live on separate volumes, enable multi-clerk reads,
canary Paper commands, and introduce Live read-only before existing Live-eligible
commands.

Rollback before the second clerk returns to transitional `combined` mode.
After two clerks exist, stop only the affected agent and expose each lane
through its independent single-clerk endpoint if the coordinator must roll
back. Keep the fleet registry and audit history intact.

Rollback must never merge Clerk volumes, copy custody, delete registry rows,
release account assignments, move arming ledgers, or silently mint replacement
identities. Older binaries may return only after schema compatibility is
proven.

## 19. Owner decisions required before implementation

1. **Physical volume attestation:** select the supported orchestrator or host
   proof used alongside the backend-issued marker.
2. **Topology:** accept one private host/container network for the first
   version or separately design authenticated multi-host transport.
3. **Configuration placement:** confirm profiles and selections remain inside
   each clerk volume while fleet storage remains registry-only.
4. **Account reassignment:** accept durable no-expiry ownership and the
   host-only proof-driven ceremony.
5. **Provider extension:** require a separate accepted authority and product
   slice for every real broker implementation.
6. **Capabilities:** accept a closed typed vocabulary with no cross-provider
   feature-parity promise.
7. **Compatibility window:** select the duration for unscoped reads; fleet
   mutations never receive an implicit compatibility target.
8. **Provisioning authority:** confirm the browser cannot enroll, retire,
   remount, restart, restore, reassign, or alter agent endpoints.
9. **Authentication:** retain the current single-operator installation-secret
   model or launch a separate RBAC project.
10. **Fleet operations:** accept read-only fleet aggregation and no fleet-wide
    mutations.
11. **Coordinator outage:** accept continued verified same-clerk execution and
    recovery with new assignments and browser commands closed.
12. **Provider directory UX:** permit broker-neutral discovery while requiring
    an approved product surface for each real provider.

## 20. Acceptance criteria

1. Alpaca Paper and Live run concurrently as different processes on different
   physical mounted volumes.
2. Each clerk has its own profile DB, custody set, ledgers, lease, credentials,
   operational files, backup, and recovery unit.
3. The coordinator stores no lane configuration or custody data.
4. Copied, duplicated, symlinked, or mis-mounted volumes fail before authority
   opens.
5. Every canonical route, stream, command, and returned reference contains
   immutable broker and clerk identity.
6. No fleet-mode command resolves an implicit target.
7. Broker-qualified external-account assignments admit only one active or
   reserved clerk and never expire into takeover.
8. Coordinator and agent both verify broker, clerk, volume, routing epoch,
   account, binding generation, and capability before mutation.
9. Paper failure cannot disable, restart, retarget, or corrupt Live.
10. One provider implementation cannot mutate another provider's state.
11. Coordinator failure preserves safe autonomous existing execution while
    new authority changes fail closed.
12. Stage, Apply, last-effective recovery, worker acknowledgement, generations,
    idempotency, Live arming, audit, and failure remain independent per clerk.
13. Existing sealed Alpaca evidence remains byte-verifiable.
14. Fleet reads preserve every lane and every partial failure without financial
    aggregation.
15. Frontend provider and clerk context remain separate from the
    Trader/Operator lens and are never implicit command authority.
16. Typed capabilities prevent assumed provider parity.
17. Fake-provider tests prove N clerks across at least two adapters with
    distinct volumes and no shared provider state.
18. Only Alpaca is enabled in production by this delivery.
19. Every later real broker has its own accepted clerk implementation,
    credentials, custody, safety, evidence, recovery, and test slice.
20. No deprecated IBKR bot-control or navigation surface is built, restored,
    or used.

## 21. Likely implementation touchpoints

- `PythonDataService/app/main.py`
- `PythonDataService/app/config.py`
- `PythonDataService/app/broker_configuration/`
- `PythonDataService/app/broker/alpaca/clerk/`
- New focused package under `PythonDataService/app/broker/fleet/`
- `PythonDataService/app/routers/brokers.py`
- `PythonDataService/app/routers/broker_v2_panel.py`
- `PythonDataService/app/routers/broker_configuration.py`
- `PythonDataService/app/routers/alpaca_clerk_sqlite.py`
- `PythonDataService/app/schemas/`
- `PythonDataService/scripts/manage_broker_configuration.py` or a focused fleet
  management CLI
- `compose.yaml` and `.env.example`
- `Frontend/src/app/services/brokers.service.ts`
- `Frontend/src/app/components/brokers/alpaca-desk/`
- `Frontend/src/app/components/broker/v2-panel/`
- Generated OpenAPI and TypeScript contracts

Exact call sites and route ownership must be re-verified against the current
tree before implementation; this PRD defines behavior, not permission to bypass
the repository's stack and authority rules.

## 22. Source and authority references

- `AGENTS.md`
- `docs/doc-authority.md`
- `docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md`
- `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md`
- `docs/architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md`
- `docs/architecture/broker-configuration-profile-contract.md` §9.4
- `docs/design/alpaca-desk-account-selection-ux-2026-09-11.md`
- `docs/broker-v2-operator-manual.md`
- `docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json`
