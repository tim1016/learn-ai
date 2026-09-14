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
| Coordinator | `fleet-coordinator` / `fleet_coordinator` | Registry, directory, approved endpoint mapping, assignment fence, session facts, routing-attempt correlation, and the existing research/data-lake plane | Alpaca execution credentials, a Clerk-local IBKR live-feed client, lane profiles/custody/orders/fills/positions/arming/runner state | The backend's Python ingress terminates here; it is the only fleet ingress |
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
| Research/data lake and market data | Existing research/data-lake ownership; it may require Polygon, data-lake, Postgres, and Redis configuration, but has no Alpaca execution credential, Clerk-local IBKR live-feed client, or lane custody mount | Egress only to the approved Alpaca API and retained read-only IBKR market-data source; unique client ID; clerk-scoped market-status source | The same permitted egress, but with a distinct client ID and lane-local state | Rendered dependency/egress policy plus lane-local status evidence; a fake source can exercise wiring but cannot qualify a production upstream |
| Capacity | Its own CPU/memory/PID/tmpfs limits | Its own CPU/memory/PID/tmpfs limits, request budget, SSE budget, and queue limit | Its own CPU/memory/PID/tmpfs limits, request budget, SSE budget, and queue limit | Rendered configuration and probe results; actual host contention/fault isolation needs a separately recorded deployment qualification |

The host and the upstream feed remain shared failure domains. Delivery D claims
application fault containment only within the tested supported-host capacity; it
does not claim independent host, power, network, or upstream-provider availability.

## Accepted runtime interfaces

| Interface | Producer → consumer | Immutable/fenced facts | Operational rule |
|---|---|---|---|
| Backend ingress | Browser → Backend → coordinator | The Backend Python base URL is the coordinator service, never a legacy combined lane | The Coordinator is the one fleet-facing Python ingress. The backend and browser do not select an agent endpoint. |
| Private presence | Lane → coordinator | Clerk ID, worker identity, approved endpoint reference, protocol/adapter version, session instance/epoch | Agent transport token authenticates the call; worker identity is not a bearer credential. The agent cannot choose or retarget an endpoint. |
| Private forwarding | Coordinator → lane | Coordinator token plus the pinned routing attempt context | The lane revalidates identity, volume, epoch, account, generation, operation/capability, and all provider gates. |
| Market status | Supported read-only feed → same lane | Clerk-scoped status evidence | No coordinator fallback and no cross-lane shared status client. A failed feed refuses the dependent admission according to existing provider rules. |
| Host ceremonies | Operator → Compose/management CLI | Issued opaque identities, actual mount evidence, offline/process-stop proof, separate credential material | Enrollment, remount, backup restore, reassignment, endpoint change, retirement, recovery, and Live arming stay host-only. |

## Evidence classifications and Delivery D gates

The rollout owner opens one dated record for every attempt. A blank field is not
evidence and blocks the next stage.

| Gate | Required record fields | Pass condition | Abort / hold condition |
|---|---|---|---|
| Code completion | PR SHA; reviewed diff; config/render command and output location; targeted test names/results | Code and documentation are merged/review-ready | This gate proves no deployment behavior. Do not call it qualification. |
| Fake Compose harness exercised | Harness SHA/version; generated Compose project; fake endpoint identities; redacted transcript; probe JSON; cleanup result | The canned fake-provider cases completed as recorded | This is test evidence only. It cannot prove production mounts, production credentials, real provider/feed behavior, host contention, or operator readiness. |
| Deployment qualified | Exact supported fleet invocation; `config --services` output with no legacy combined role; rendered config; actual mount and egress inspection; image digests; resource limits; reviewed fault record | Real deployed roles, their actual mounts, ingress/egress policy, and bounded resource behavior meet the D checklist | Any legacy combined service, shared lane root/credential, wrong backend ingress, unexpected exposed agent, or cross-lane fault impact. |
| Paper canary | Paper account/Clerk ID; admission receipt; current program/validation/build evidence; bounded duration; operator decision | Existing Paper gates admit and recorded observations meet the canary plan | Any refusal, unexpected command, stale/uncertain outcome, resource breach, or missing evidence. |
| Live read-only | Live account/Clerk ID; read-only credential/posture proof; route/response/stream correlation; duration | Reads and streams preserve lane identity while all mutation gates remain closed | Any mutation attempt, cross-lane data, stale identity, unavailable market-status evidence, or degraded Live capacity. |
| Live-command authorization | Separate named approver; eligible existing command; frozen target; arming/envelope/custody/risk evidence; idempotency/receipt plan | A separately authorized command passes unchanged existing Live gates | No separate authorization, no complete gates, unsupported capability, unknown outcome, or any widened authority. |
| Operational rollout | Owner acceptance; compatibility observation package; rollback readiness; incident contacts | All preceding D gates are complete and E prerequisites are scheduled | A missing prior-stage evidence field, missing pre-cutover observation package, or any unperformed gate presented as evidence. |

The `Live-command authorization` row is deliberately not a Delivery D completion
claim. It is a per-command host/operator decision after successful Paper and
read-only evidence, never a Compose setting.

## Compatibility observation begins before the cutover

The only compatibility behavior observed in D is a browser-direct, unscoped **read**
route. Fleet mutations never have an implicit target. Start the observation while the
existing combined role is still serving retained reads, before its traffic is cut over.
The same runtime collector is installed directly in both `combined` and
`clerk_agent` roles, so the evidence continues across the cutover without an
access-log-exporter substitute.

The combined role writes the compact aggregate to
`$ALPACA_CLERK_DIR/compatibility/route_hits.json` before cutover; each Clerk agent
writes the same artifact in its own lane after cutover. Schema v2 contains
`schema_version`, `updated_at_ms`, and sorted `route_hits` entries with
`route_family`, `response_class`, `count`, `first_observed_at_ms`, and
`last_observed_at_ms`. `method` is intentionally absent because only `GET` and `HEAD`
are eligible. The artifact contains no account or Clerk ID, URL/query value, header,
token, request body, broker credential, or raw access log. Retain the combined and
lane aggregates plus their consumer-inventory reference in the restricted rollout
record until E's retirement decision is closed.

Zero hits in an idle deployment do not qualify retirement. E owns the representative
window decision, consumer-inventory reconciliation, acceptance decision, and final
retirement; D neither selects the duration nor removes a compatibility route.
