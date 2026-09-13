# Multi-broker clerk fleet: adversarial review and proposed delivery revision

Reviewed 2026-09-13 against commit `354ce9ee754d2ea1d37e3e029592c46becd94755` and the supplied Phases 2–7 delivery plan.

This is a proposed delivery revision and point-in-time review. [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md) remains the accepted system decision; the [fleet PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md) supplies its requirements. This document does not silently supersede either. Accepted protocol clarifications belong in their owning contract during implementation, rather than becoming a second system specification here.

## Judgment

Keep separate clerk processes and volumes, durable account ownership without heartbeat expiry, provider-owned execution, explicit clerk routes, and immutable command targets. Those are the right architectural commitments.

Revise the plan before production wiring. Its main blind spot is treating the fleet spine as a finished authority protocol and treating process separation as proof of storage and dependency separation. Neither conclusion follows from the existing tests. The current spine has observable admission gaps, and several proposed transport and recovery choices conflict with the intended behavior.

Single sources of truth are a design constraint. A projection can repeat an authoritative fact, but it must identify its source and version, and must never acquire an independent write path that changes its meaning.

## Authority and module design

| Fact | Single authority | Other representations |
|---|---|---|
| Broker-qualified account ownership | Fleet registry assignment transaction | Clerk recovery evidence records a confirmation; it cannot grant or transfer ownership. |
| Clerk identity and terminal retirement | Fleet registry | Deployment expectations and volume markers must agree with the issued identity. |
| Physical mount and endpoint placement | Deployment-owned host configuration, verified against actual container mounts | Registry retains the approved identity/reference; agents cannot choose their destination or attest a physical mount merely by supplying matching strings. |
| Effective profile, revision, account, and binding generation | Clerk-local profile selection transaction | Coordinator stores the exact confirmed observation and uses it as a routing fence. It does not invent or advance the binding generation. |
| Current registered process | Coordinator session state, fenced by instance and epoch | Heartbeats refresh observations; they cannot confirm assignments. |
| Operation contract | Provider-owned typed operation declarations tied to the actual handlers and schemas | Agent routes, coordinator allowlist, OpenAPI, and generated frontend types derive from those declarations. |
| Execution admission, idempotency, and outcome | Provider clerk's existing command and receipt machinery | Fleet receipts correlate delivery attempts and retain provider receipt references. |
| Custody, arming, risk, and numerical facts | Existing provider authority and canonical Python implementations | Directory, frontend, and coordinator display or carry evidence. |
| Command intent | Immutable target from the rendered server resource | Dialogs retain this target; mutable navigation state cannot replace it. |

The coordinator is a control-plane authority with a narrow forwarding responsibility. Calling it "pure transport" hides its ownership of assignments, session admission, and routing evidence. Keep these responsibilities in a small fleet module. Keep provider admission and execution in the clerk module. Put local and HTTP delivery adapters behind the same provider operation interface; do not build a second trading implementation or a generic trading engine.

## Findings that change the plan

### 1. P1 — Harden the spine before exposing it over HTTP

The following were reproduced through `FleetControlService` using temporary SQLite storage and a minimal fake provider, without modifying production state:

| Probe | Current result | Required result |
|---|---|---|
| Register, send a heartbeat with binding generation/account, never reserve or confirm an assignment, then resolve a route | Route resolves. | Execution routing remains closed. |
| Reserve and confirm an account, then run the proposed reserve step again for the same clerk | Reservation refuses because the assignment is already effective. | Explicit same-owner resume succeeds after its proofs pass. |
| Replace an agent session, then submit a confirmation carrying no caller instance/epoch | Confirmation updates the replacement session. | Superseded-session confirmation refuses atomically. |
| Confirm generation 10, replace the session, then confirm generation 3 | Lower generation is accepted. | Stale confirmed binding evidence refuses; restore requires its own ceremony. |

Evidence: [session observation and assignment methods](../../PythonDataService/app/broker/fleet/service.py), particularly `observe_session`, `reserve_assignment`, `confirm_assignment`, `_record_binding_observation`, and `resolve_route`. `resolve_route` currently checks a non-null reported generation without proving an effective assignment. `confirm_assignment` does not accept caller instance or epoch and reads whichever session is current.

**Revision:** separate observed health from confirmed session binding. Confirmation must atomically compare the authenticated clerk, current instance/epoch, reserved or effective assignment owner/generation, and proposed binding facts. Routing must join those confirmed facts, lifecycle, and liveness. Define same-owner resume explicitly. HTTP authentication alone cannot close these races because the old process may hold the same durable worker identity.

Keep the generation meanings separate: assignment generation changes ownership history; routing epoch changes process registration; binding generation changes the local effective tuple; selection generation fences configuration edits. None substitutes for another.

### 2. P1 — Define crash recovery as a durable protocol

"Local acknowledgement, then confirm remotely" spans two databases. A process can crash after the local write and before the fleet commit; the fleet can commit and lose its reply; the agent can crash before recording that reply. D7 also requires registry verification before profile access, while FR-066 permits recovery with the coordinator unavailable. The plan does not describe how both hold.

**Revision:** specify a recoverable handover with explicit states and reconciliation. The clerk retains additive, nonsecret confirmation evidence identifying the registry, clerk, volume, assignment generation, profile/revision/account, and binding generation it actually confirmed. This is evidence of an existing grant, never an independent grant. Local acknowledgement alone is insufficient for offline recovery.

Pin behavior for failure before reservation, after reservation, after local acknowledgement, after fleet commit, and before local confirmation evidence is durable. A lost confirmation reply remains reconcilable with the same identity; it must not trigger a new assignment. First assignment, different-account Apply, and changed binding must wait for the coordinator. Offline recovery uses only the already-confirmed exact last-effective tuple and verified original deployment/volume.

Registry backup restore is also a safety event: restoring old rows can forget an assignment or roll back epochs. Keep restored-registry routing disabled until a host ceremony reconciles durable lane evidence and deployment membership. Do not silently create an empty replacement registry. A copied local checkpoint cannot prove that the original writer is offline; reassignment still requires host proof and credential isolation.

### 3. P1 — Worker identity must not become a stored bearer credential

D3 authenticates agent-to-coordinator calls with `worker_key`. The existing [schema](../../PythonDataService/app/broker/fleet/schema.py) stores that value in plaintext as durable identity. Using it as the sole network credential changes its meaning: possession of a registry value now authenticates control-plane writes. Constant-time comparison does not resolve the conflict with the no-secret registry rule.

**Revision:** retain `worker_key` as private durable identity and use separate per-clerk, environment-only authentication material for the two transport directions. Define provisioning, loss, rotation, and restart behavior. Map credential slots through deployment-owned code/configuration. Never copy the shared environment containing all broker credentials into every role.

"Printed once, stored nowhere" needs correction: restartable services need operator-managed secret persistence, such as the existing uncommitted environment-file convention. The application must not persist these secrets in registries, receipts, or logs. Provisioning must not depend on recovering secret output from terminal history, and rerunning migration must not silently rotate credentials.

Existing [configuration routes](../../PythonDataService/app/routers/broker_configuration.py) require the installation secret themselves. Therefore "routers unchanged" and "browser secret never reaches the agent" require an explicit authentication adaptation. Reuse the business handlers while composing separate public and internal authentication policies. No unauthenticated fallback.

### 4. P1 — Volume and endpoint evidence needs a host-owned seam

D6 allows an agent-reported endpoint checked against an optional allowlist. ADR 0062 requires deployment-owned destinations. Make the mapping mandatory. Registration can report an approved endpoint reference, but cannot change the endpoint or its credential binding. Disable redirects and environment-proxy inheritance for internal clients; the existing shared-market-status client already uses those settings.

The foundation's [volume verifier](../../PythonDataService/app/broker/fleet/volume.py) explicitly does not inspect physical mount identity. Its marker comparison depends on deployment attestation. A copied marker plus copied expectations cannot prove a distinct physical volume.

There is also a namespace mismatch: provisioning compares absolute root strings globally in `service.py`. Two independent containers may both correctly mount their distinct volumes at `/app/artifacts/alpaca_clerk`. Those equal strings do not identify the same physical root. Conversely, different strings do not prove different volumes.

**Revision:** make a host-owned provisioning/verification ceremony compare actual orchestrator volume sources, container destinations, and issued identities. Qualify paths by their deployment namespace. The agent verifies its own mounted root before opening local stores; the coordinator validates approved identity evidence without mounting every custody volume. Registration/reservation currently make volume checking optional—remote wrappers must neither skip required evidence nor try to inspect an agent-local path on the coordinator filesystem. Give neither runtime a Docker/Podman management socket.

### 5. P1 — Isolate every writable runtime root and preserve feed dependencies

Giving each agent a different `ALPACA_CLERK_DIR` is insufficient. [Current startup](../../PythonDataService/app/main.py) reads bot bindings and arming-related seals through `live_artifacts_root()`, constructs `BotTaskRegistry` from the separate live-runs setting, validates the data lake, and marks old research jobs failed. [Compose](../../compose.yaml) mounts a shared artifacts tree and a broad environment file. Copying the service definition risks sharing bot state and credentials or duplicating unrelated jobs.

The bot runner also resolves a retained read-only market-data feed. This is a dependency to account for; it is not authorization to develop the deprecated IBKR broker-control surfaces. D1's "no broker client in the coordinator" needs a concrete feed ownership/topology decision so agents do not lose a required input when startup is split.

Likewise, returning 503 from `market-status-snapshot` is a functional migration issue. [The existing consumer](../../PythonDataService/app/broker/alpaca/market_liveness.py) can explicitly depend on that upstream for market-status evidence. A runbook sentence does not replace the source or prove that exposure admission still works.

**Revision:** before composition changes, inventory every writer, global, credential set, background task, operational root, and market-data dependency used by an Alpaca lane. Assign each to one role and one owner. Verify operational files, bindings, arming evidence, callbacks, and recovery artifacts are lane-local, not just custody and profiles. Retain shared read-only datasets where appropriate. Provide an explicit supported replacement for each feed dependency before retiring its route.

CPU, memory, disk, and network are still shared on one host. Pin per-clerk queues and concurrency budgets and test contention. Promise application fault containment within the supported host's capacity; separate containers do not make host failure independent.

### 6. P1 — One typed operation catalog, including readiness and command semantics

The current [provider protocol](../../PythonDataService/app/broker/fleet/provider.py) exposes `route_catalog: frozenset[str]`. Strings cannot express HTTP method, required capability, request/response schema, target extraction, stream behavior, idempotency semantics, or whether an operation needs an effective account. A hand-maintained coordinator allowlist plus agent routes plus frontend URL builders would be three competing contracts.

**Revision:** extend provider-owned operation declarations only enough to express these facts, referencing existing handlers and schemas. Derive forwarding, exported contracts, and generated frontend builders from that source. Capabilities follow the declared operation, not a caller-supplied weaker capability. Preserve provider error and receipt semantics rather than translating business payloads generically.

Distinguish configuration access from execution readiness. A provisioned agent with a verified identity but invalid/missing broker configuration must still support authorized configuration inspection and repair. The current all-purpose route resolver refuses any session without a binding generation; using it for every route can lock the operator out of the configuration needed to obtain a binding. Accountless configuration commands retain selection-generation fencing. Trading commands require the confirmed exact account/binding and all existing provider gates.

Include protocol/adapter compatibility in registration. A coordinator must not advertise operations merely because its own newer adapter knows them while an older agent is running. Unsupported version combinations refuse explicitly. Provider summaries remain validated, bounded typed observations; arbitrary JSON is not an acceptable public projection merely because it came from an agent.

### 7. P1 — A routing receipt cannot guarantee a command executed only once

D4 mentions idempotency keys and receipts but does not specify how a retry reaches existing provider deduplication. [Routing receipt storage](../../PythonDataService/app/broker/fleet/records.py) contains no pinned epoch or binding generation; the current update path can replace the outcome for the same lane/key. A timeout is not proof that no side effect occurred. A wrong response identity discovered after submission is also not proof of non-execution.

**Revision:** persist the pinned attempt context before dispatch. The provider clerk remains the sole authority for command deduplication and durable results, using its existing command machinery. Reusing a key with different semantic input must conflict. Keep that comparison at the provider; do not add a competing command database containing business payloads to the coordinator.

Distinguish definitively not dispatched, provider refused, delivered with a provider receipt, and outcome unknown. Preserve attempt history and references without allowing a late failed retry to erase a known successful result. Offer read-only reconciliation of the same command identity after an epoch change; never automatically resubmit to the replacement process. Define behavior for every mutating route, including configuration and commands whose current contract is not naturally idempotent.

Identity response fields must come from the actual serving runtime and resource, not reflected request headers. A mismatch after possible dispatch isolates that client and reports an uncertain outcome requiring provider reconciliation. Refusing the response does not undo the action.

### 8. P1 — Replace the proposed combined-mode streaming transport

The repository pins HTTPX 0.28.1. Its [ASGI transport implementation](https://github.com/encode/httpx/blob/0.28.1/httpx/_transports/asgi.py) collects response chunks and awaits application completion before returning the response. Consequently a long-lived SSE response will not deliver incremental events through this transport. HTTPX also [does not start an ASGI application's lifespan](https://www.python-httpx.org/advanced/transports/#asgi-startup-and-shutdown).

**Revision:** reuse the provider operation implementation behind local and HTTP delivery adapters. Local delivery must preserve an async event stream without buffering it to completion; alternatively, use an actual supported internal HTTP transport. Keep common admission and identity validation in the shared provider operation path. Do not call the two paths identical or rely solely on in-process request tests for network behavior.

For SSE, validate each complete event's provenance, cap event size and pending buffers, propagate cancellation, scope reconnect cursors to the lane/session, and close the old stream when its identity becomes stale. Initial response headers alone do not satisfy FR-076. Reserve request capacity separately from long-lived streams so Paper cannot occupy all Live capacity.

### 9. P2 — Keep D5, but define its exact update rule

Capturing a selection generation can work if it is captured only when the effective `(profile, revision, account)` changes. "At the last acknowledgement" is insufficient: [the current acknowledgement policy](../../PythonDataService/app/broker_configuration/binding_decision.py) also acknowledges a consumed Apply, including one that may leave that tuple unchanged.

**Revision:** one clerk-local transaction decides whether the effective tuple changed and persists its binding generation with that tuple. Stage, refused Apply, ordinary restart, and Apply of the same effective tuple do not change it. A change away and back receives a new generation. Migration establishes a documented initial value without inventing historical transitions. The coordinator only confirms that value.

Frontend resources remain grouped by `(broker, clerk_id)`, but requests, cached values, event cursors, and command dialogs must also retain the resource's account/binding/epoch provenance. Discard late responses after a context change. Use one immutable resource-target type instead of threading several loosely related identifiers through dozens of services.

### 10. P1 — Migration and rollback must work halfway through

"Idempotent rerun is a no-op" handles only completed migration. Provisioning, marker creation, assignment import, local schema upgrade, and secret delivery cannot commit atomically together. The existing provisioning compensation handles Python exceptions; a process kill between durable writes can leave partial state.

**Revision:** make migration an offline, resumable ceremony with durable progress and exact identity checks. Acquire the correct local ownership locks, verify the original effective binding, preserve pending Stage/Apply semantics, import ownership without enabling a new writer, and verify sealed evidence remains byte-identical. An already-complete rerun verifies the result; a partial rerun resumes; inconsistent evidence refuses. Do not remint clerk or volume identities to get past a partial failure.

Prove rollback before changing Compose. Additive DDL is not sufficient: [the existing fleet store](../../PythonDataService/app/broker/fleet/store.py) deliberately rejects a schema newer than its binary. Prefer rollback using a compatible fleet-aware binary/configuration. After two clerks exist, each fallback endpoint still belongs to one original clerk/volume and preserves the fleet assignments.

Retain default `combined` only for the intended legacy/development compatibility posture. Deployment must specify roles explicitly. Once a volume is fleet-enrolled, missing fleet configuration or an identity marker is a refusal, never permission to fall back to unfenced legacy authority. Select and measure the read-compatibility window; zero route hits alone during an unused deployment is not evidence that all consumers migrated.

## Revised delivery sequence

Split the original PR-A. Six reviewable merges are preferable to one oversized PR containing new trust, storage, lifecycle, migration, and networking protocols. Preserve the later product milestones, but move their prerequisite safety evidence forward.

| Delivery | Scope | Exit evidence |
|---|---|---|
| A1 — Fleet protocol hardening | Close the reproduced admission gaps; define confirmed versus observed state, resume, stale-session refusal, durable attempt context, deployment proof, secret identity separation, and the typed operation seam. No production fleet enablement. | Regression probes for all four reproduced cases; fake-provider state-transition and concurrent confirmation tests; a first real-process transport/stream test. |
| A2 — One existing Alpaca lane end to end | Compose role-owned lifespans and roots; authenticated agent registration; actual host mount verification; resumable migration; one scoped read, one representative command, and one stream through the full route. Preserve configuration access when execution is unavailable. | Separate coordinator/agent processes against a fake broker; restart of an effective lane; every handover crash point; lost reply and reconciliation; one-clerk rollback; sealed evidence verification. |
| B — Complete scoped contracts | Cover every Alpaca operation family and backend-authored reference using the typed operation declarations. Adapt authentication and provider command context. Regenerate contracts. | Route inventory accounts for every retained/replaced/retired operation; wrong target and capability gates; per-event identity; no implicit canonical command or internal-route escape; supported mixed-version matrix. |
| C — Frontend cutover | Directory, immutable resource targets, scoped resource/stream state, configuration, bots, custody, gallery, Deploy, manual orders, and deep links. Preserve the lens kernel. | Browser traces include broker/clerk identity throughout; late responses cannot cross account/generation context; open dialogs survive navigation safely; one failed lane leaves the other usable. |
| D — Two-clerk rollout | Explicit role and secret wiring, distinct named volumes and all writable roots, supported market-data sources, bounded per-lane resources, host acceptance script and operational runbooks. | Real Compose isolation tests using fake broker endpoints before any second production lane; Paper canary; Live read-only; then only separately authorized existing eligible Live commands. |
| E — Measured retirement and GA | Retire compatibility reads after the measured window; finalize operator material and production qualification evidence. Future real brokers remain separate accepted work. | No unresolved safety failures in affected surfaces; exercised backup/restore/reassignment and registry recovery; proven compatible rollback; full fake-provider conformance and operator acceptance. |

Each delivery carries its own documentation and tests. Backup/restore semantics, browser-to-provider correlation, and fake-provider conformance are designed and exercised as their seams land, not first invented in E. A mock acceptance script is useful for its own control flow but cannot qualify physical mount separation or process isolation.

## Adversarial acceptance scenarios

1. Two processes race for the same broker-qualified account: only one can open authority; loss of heartbeat never admits the other.
2. A late confirmation from an old instance arrives after replacement registration: it cannot change the new session, generation, or routability.
3. A heartbeat supplies plausible binding facts without confirmation: health updates, execution routing stays closed.
4. Kill either process at every handover persistence boundary; same-owner recovery converges without new identity or ownership transfer.
5. A provider accepts a command but its response is lost or names the wrong identity: the client reports uncertainty, reconciles by the original command identity, and does not duplicate execution.
6. Restart an agent between command preparation and dispatch; retry never crosses the epoch automatically.
7. Stage and re-Apply the same effective tuple, then switch away and back: binding-generation behavior matches the explicit transition rule.
8. Configure an unbound clerk through its authenticated configuration surface while trading commands remain unavailable.
9. Mount two distinct volumes at the same path in separate containers: valid; copy a marker onto the wrong actual volume: refused before writers open.
10. Remove the coordinator: running lanes continue under existing provider rules; only a verified already-confirmed last-effective lane can recover; changed bindings and browser commands refuse.
11. Restore an older registry backup while lane evidence is newer: routing and assignment creation remain closed pending reconciliation.
12. Poison Paper's process, DB, credentials, volume, stream, and request queue separately: Live retains its own state and bounded service capacity within the supported host limits.
13. Reuse an idempotency key independently in two clerks: independent provider outcomes; change the payload under the same clerk/key: conflict.
14. Open a command dialog, change route/lens, receive a late response, and reconnect a stale stream: none changes the frozen target.
15. Interrupt migration after every durable step and rerun: exact identity resumes or a typed refusal explains the conflict; sealed history is unchanged.
16. Roll back after both lanes exist: no shared volume, released assignment, copied arming ledger, deleted registry history, or implicit fallback target appears.

Use targeted consumer tests plus the bounded change gates in [testing rules](../../.claude/rules/testing.md). Run real network/Compose qualification where the seam changes; do not substitute full unrelated local suites for adversarial scenarios. The supplied claim of five pre-existing panel failures was not revalidated in this review. Record the exact base-commit baseline before implementation, and assess any failure touching authority, routing, or admission before using it as a rollout exclusion.

## Review evidence and limits

The review inspected the pasted plan, accepted ADR/PRD, fleet implementation, configuration handover, startup, deployment mounts, authentication call sites, and relevant market-data dependencies. Four foundation behaviors were reproduced with a temporary fake-provider harness against the reviewed commit. Temporary databases were removed after the probes. HTTPX behavior was checked against the pinned upstream source and official documentation.

This was an architectural review, not a production implementation or a full-suite qualification run. No broker calls, real credentials, production volumes, deployment changes, commits, or publishing were performed.
