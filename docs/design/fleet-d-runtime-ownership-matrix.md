# Fleet D runtime ownership and qualification matrix

**Status:** Delivery D implementation and operations contract. This is supporting
evidence, not an authority that changes [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md)
or the [multi-broker Clerk PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md).

## Delivery boundary

Delivery D makes the already accepted fleet protocol deployable as three Compose
roles on one private host network. It does not authorize a new broker, a generic
broker capability, browser enrollment, browser arming, browser reassignment,
browser restore, or any change to Alpaca custody, arming, sealed-envelope, or
Live-admission rules.

The only production provider in this delivery is Alpaca. The retained IBKR
market-data feed is an input dependency only; it is not an IBKR broker-control
surface and this delivery must not revive one.

| Role | Compose service / role | Owns | Must not own | External exposure |
|---|---|---|---|---|
| Coordinator | `fleet-coordinator` / `fleet_coordinator` | Registry, directory, approved endpoint mapping, assignment fence, session facts, routing-attempt correlation | Broker credentials, lane profiles, custody, orders, fills, positions, arming, runner state, market-data clients | The sole public fleet service |
| Paper lane | `alpaca-paper-clerk` / `clerk_agent` | One opaque Clerk identity; Paper profile/custody/lease/ledger/streams/runner state/backups; its two transport tokens and Paper broker credential slot | Live root, Live credentials, coordinator registry write access other than authenticated presence protocol, container socket | Internal-only private network |
| Live lane | `alpaca-live-clerk` / `clerk_agent` | One opaque Clerk identity; Live profile/custody/lease/ledger/streams/runner state/backups; its two transport tokens and Live broker credential slot | Paper root, Paper credentials, coordinator registry write access other than authenticated presence protocol, container socket | Internal-only private network |

`FLEET_ROLE=combined` is a legacy/development compatibility posture only. It is
not a production fleet role. A fleet-enrolled volume with absent fleet settings,
a missing marker, a copied marker, a mis-mounted root, or a non-matching deployment
attestation must refuse before a database writer or broker client opens.

## Storage, credential, dependency, and capacity boundaries

| Boundary | Coordinator | Paper | Live | Acceptance evidence |
|---|---|---|---|---|
| Named volume | Unique coordinator control volume only | Unique Paper volume only | Unique Live volume only | Actual Compose volume source, destination, backend marker, and nonsecret host attestation agree; all three sources differ |
| Writable roots | Registry and coordinator-local routing evidence only | Profile selection, custody, lease, receipts, streams, bot bindings, runner/arming evidence, backups, recovery | Same classes, but only for Live | Root fence reports each writable root under that lane's verified volume; no shared `/app/artifacts` writer path |
| Broker credentials | None | Only the Paper credential slot in its uncommitted env file | Only the Live credential slot in its uncommitted env file | Secret-absence scan and redacted service inspection; no values, names, lengths, or hashes copied to registry/log/receipt |
| Fleet transport credentials | Per-Clerk coordinator maps are environment-only | Its agent-to-coordinator and coordinator-to-agent tokens only | Its agent-to-coordinator and coordinator-to-agent tokens only | Separate files/slots; a Paper token is rejected by Live and vice versa |
| Market data | No broker or market-data client | Retained read-only feed, unique client ID, clerk-scoped market-status source | Retained read-only feed, unique client ID, clerk-scoped market-status source | Both lanes receive usable evidence without sharing a client ID; status failure is visible per lane |
| Capacity | Its own CPU/memory/PID/tmpfs limits | Its own CPU/memory/PID/tmpfs limits, request budget, SSE budget, and queue limit | Its own CPU/memory/PID/tmpfs limits, request budget, SSE budget, and queue limit | Rendered Compose configuration plus the fault run proves Paper exhaustion does not consume Live's bounded capacity |

The host and the upstream feed remain shared failure domains. Delivery D claims
application fault containment only within the tested supported-host capacity; it
does not claim independent host, power, network, or upstream-provider availability.

## Accepted runtime interfaces

| Interface | Producer → consumer | Immutable/fenced facts | Operational rule |
|---|---|---|---|
| Public fleet route | Browser/API → coordinator | `broker`, `clerk_id`; command target also carries its exact account, binding generation, routing epoch, capability, and idempotency identity as applicable | No fleet mutation can select an implicit lane. Browser selection never retargets an open command. |
| Private presence | Lane → coordinator | Clerk ID, worker identity, approved endpoint reference, protocol/adapter version, session instance/epoch | Agent transport token authenticates the call; worker identity is not a bearer credential. The agent cannot choose or retarget an endpoint. |
| Private forwarding | Coordinator → lane | Coordinator token plus the pinned routing attempt context | The lane revalidates identity, volume, epoch, account, generation, operation/capability, and all provider gates. |
| Market status | Supported read-only feed → same lane | Clerk-scoped status evidence | No coordinator fallback and no cross-lane shared status client. A failed feed refuses the dependent admission according to existing provider rules. |
| Host ceremonies | Operator → Compose/management CLI | Issued opaque identities, actual mount evidence, offline/process-stop proof, separate credential material | Enrollment, remount, backup restore, reassignment, endpoint change, retirement, recovery, and Live arming stay host-only. |

## Delivery D acceptance record

The rollout owner opens one dated record for every attempt. A blank field is not
evidence and blocks the next stage.

| Gate | Required record fields | Pass condition | Abort / hold condition |
|---|---|---|---|
| Code completion | PR SHA; reviewed diff; config/render command and output location; targeted test names/results | Code and documentation are merged/review-ready | This gate proves no deployment behavior. Do not call it qualification. |
| Fake Compose qualification | Image digests; exact Compose config; fake endpoint identity; three actual volume sources/destinations; resource limits; command transcript; per-case results | All isolation/refusal/fault cases pass against real containers | Any shared writable mount, secret leakage, unexpected host port, route/identity mismatch, or Paper fault affecting Live. |
| Paper canary | Paper account/Clerk ID; admission receipt; current program/validation/build evidence; bounded duration; operator decision | Existing Paper gates admit and recorded observations meet the canary plan | Any refusal, unexpected command, stale/uncertain outcome, resource breach, or missing evidence. |
| Live read-only | Live account/Clerk ID; read-only credential/posture proof; route/response/stream correlation; duration | Reads and streams preserve lane identity while all mutation gates remain closed | Any mutation attempt, cross-lane data, stale identity, unavailable market-status evidence, or degraded Live capacity. |
| Live-command authorization | Separate named approver; eligible existing command; frozen target; arming/envelope/custody/risk evidence; idempotency/receipt plan | A separately authorized command passes unchanged existing Live gates | No separate authorization, no complete gates, unsupported capability, unknown outcome, or any widened authority. |
| Operational rollout | Owner acceptance; compatibility measurement report; rollback readiness; incident contacts | All earlier gates are complete and E prerequisites are scheduled | A missing prior-stage evidence field or unmeasured compatibility traffic. |

The `Live-command authorization` row is deliberately not a Delivery D completion
claim. It is a per-command host/operator decision after successful Paper and
read-only evidence, never a Compose setting.

## Compatibility measurement started in D

The only compatibility behavior measured in D is a browser-direct, unscoped
**read** route. Fleet mutations never have an implicit target. Instrumentation
records a stable route-family identifier, UTC bucket, response class, and count;
it records no account ID, Clerk ID, URL/query value, header, token, request body,
or broker credential. Record an inventory of known consumers and a representative
traffic window at the same time.

Zero hits in an idle deployment do not qualify retirement. Delivery E may retire
the compatibility read only after the measured representative window, consumer
inventory reconciliation, explicit owner acceptance, and its recovery/rollback
qualification are complete.
