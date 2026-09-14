# Fleet D two-Clerk rollout and qualification

**Status:** Delivery D runbook. It is a gate sequence, not deployment evidence. Mark a gate complete only with the evidence fields in [the D ownership matrix](../design/fleet-d-runtime-ownership-matrix.md). The accepted decision remains [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md).

## Scope and language

This runbook authorizes preparation, fake-endpoint Compose qualification, a Paper canary, and Live read-only qualification. It does not authorize Live commands. A Live command requires separately recorded authorization and must still satisfy existing provider-owned envelope, host-only arming, custody, capability, exact-binding, idempotency, risk, and outcome-reconciliation gates.

Enrollment, endpoint changes, remount, restore, reassignment, recovery, retirement, and Live arming remain host-only. Do not put a container-runtime socket in a service or use the browser or `FLEET_ROLE=combined` for a ceremony.

Use exactly one of these status statements: `code complete`, `fake Compose harness exercised`, `deployment qualified`, `Paper canary qualified`, `Live read-only qualified`, `Live command authorized`, or `operational rollout complete`. Never infer a later statement from an earlier one.

## 0. Baseline and environment files

Record target commit, image digest, operator, host, expected Clerk IDs, accounts, and planned time window. The clean pre-D integration baseline at `0ca2200e` was:

```bash
DATA_PLANE_CONTROL_SECRET="" /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -m pytest tests/broker/fleet -q -p no:cacheprovider
```

It passed `175` tests in `69.38s`. This is code baseline evidence only; it is not Compose, broker, Paper, Live, or operator qualification.

Copy the committed examples and set strict owner-only permissions before entering secrets:

```bash
cp deploy/fleet/env/coordinator.env.example deploy/fleet/env/coordinator.env
cp deploy/fleet/env/paper.env.example deploy/fleet/env/paper.env
cp deploy/fleet/env/live.env.example deploy/fleet/env/live.env
chmod 600 deploy/fleet/env/coordinator.env deploy/fleet/env/paper.env deploy/fleet/env/live.env
```

The coordinator file has the installation control secret, coordinator-side per-Clerk transport-token maps, and the existing research/data-lake configuration it needs (for example Polygon, data-lake, Postgres, and Redis). It has no Alpaca execution credential, Clerk-local IBKR live-feed client configuration, or lane-custody path. Each lane file has only its own broker credential slot, issued Clerk/worker identity, and two directional fleet tokens. Never reuse a file, token, broker credential, client ID, or opaque Clerk ID between Paper and Live.

Each lane file explicitly sets `FLEET_ROLE=clerk_agent`, `FLEET_CLERK_ID`, `FLEET_WORKER_KEY`, `FLEET_COORDINATOR_URL=http://fleet-coordinator:8000`, `FLEET_AGENT_ENDPOINT_REF`, `FLEET_AGENT_SERVICE_TOKEN`, `FLEET_COORDINATOR_SERVICE_TOKEN`, and `FLEET_DEPLOYMENT_NAMESPACE`. It also sets reviewed positive values for `FLEET_MAX_INFLIGHT_REQUESTS`, `FLEET_MAX_INFLIGHT_STREAMS`, `FLEET_REQUEST_QUEUE_LIMIT`, and `FLEET_REQUEST_QUEUE_TIMEOUT_MS`.

Those four capacity controls are per lane. A `503 fleet_lane_capacity_exhausted` is a qualification observation, never a reason to raise limits during an incident.

## 1. Provision distinct coordinator, Paper, and Live identities

Create the three named volumes before Compose starts: `fleet-coordinator-control`, `fleet-alpaca-paper-data`, and `fleet-alpaca-live-data`. Record every actual engine volume source and its container destination. The expected destinations are `/app/artifacts/fleet` for the coordinator and `/app/artifacts/alpaca_clerk` for each lane; equal destination strings do not prove shared storage.

Use the host ceremony against the mounted roots. Record one-time outputs only in the corresponding uncommitted environment file or restricted evidence store, never in shell history, a ticket, registry, receipt, or log.

```bash
cd PythonDataService
.venv/bin/python -m scripts.manage_broker_fleet init --control-dir <coordinator-control-root>
.venv/bin/python -m scripts.manage_broker_fleet provision --control-dir <coordinator-control-root> --broker alpaca --label "Paper" --volume-root <paper-lane-root> --attestation-id <paper-host-attestation> --deployment-namespace <namespace>
.venv/bin/python -m scripts.manage_broker_fleet provision --control-dir <coordinator-control-root> --broker alpaca --label "Live" --volume-root <live-lane-root> --attestation-id <live-host-attestation> --deployment-namespace <namespace>
```

Approve only deployment-owned distinct endpoints, then verify both lane volumes:

```bash
.venv/bin/python -m scripts.manage_broker_fleet approve-endpoint --control-dir <coordinator-control-root> --clerk-id <paper-clerk-id> --endpoint-ref <paper-ref> --base-url http://alpaca-paper-clerk:8000
.venv/bin/python -m scripts.manage_broker_fleet approve-endpoint --control-dir <coordinator-control-root> --clerk-id <live-clerk-id> --endpoint-ref <live-ref> --base-url http://alpaca-live-clerk:8000
.venv/bin/python -m scripts.manage_broker_fleet verify --control-dir <coordinator-control-root> --clerk-id <paper-clerk-id> --volume-root <paper-lane-root>
.venv/bin/python -m scripts.manage_broker_fleet verify --control-dir <coordinator-control-root> --clerk-id <live-clerk-id> --volume-root <live-lane-root>
```

Any refusal stops the rollout. Do not reissue an identity, copy a marker, or release an assignment to make a service start.

## 2. Use the fleet-only invocation and inspect production ingress

Use only the release-owned fleet invocation documented with the D Compose bundle. It must render the fleet profile and suppress the legacy `combined` service; do not add the ordinary development compose file or run a generic `up` as a shortcut. Save both the rendered configuration and `config --services` output. The latter must show the coordinator, the Paper Clerk, the Live Clerk, and only explicitly approved supporting services; it must not show a legacy combined Python service.

The backend's Python base URL terminates at `fleet-coordinator`. The browser reaches the backend, then the coordinator; it never addresses an agent directly. `fleet-coordinator` is the only host-published fleet service. `alpaca-paper-clerk` and `alpaca-live-clerk` have no host port, are reachable only on the fleet network, and must have explicit `fleet_coordinator`/`clerk_agent` roles respectively.

Verify three different named volume sources; no shared writable artifact tree; and lane-local `ALPACA_CLERK_DIR`, `IBKR_LIVE_RUNS_ROOT`, `IBKR_LIVE_BARS_ROOT`, and `BROKER_CAPTURE_DIR`. Verify each role has CPU, memory, PID, and tmpfs limits, each lane has its own request/SSE/queue budget, and no service mounts a Docker/Podman socket. Verify the coordinator contains no Alpaca execution credential, Clerk-local IBKR live-feed client, or lane custody mount; its research/data-lake configuration remains permitted. Verify Paper has no Live credential slot and Live has no Paper credential slot.

The fleet network permits lane egress only to the approved Alpaca API and retained read-only IBKR market-data source through the reviewed host/gateway path. Both lanes use distinct IBKR client IDs and clerk-scoped market-status forwarding. The coordinator receives neither an Alpaca execution credential nor a Clerk custody mount and has no Clerk-local IBKR live-feed client; its existing research/data-lake dependencies remain permitted. Do not replace the market-data path with a deprecated IBKR control endpoint or a coordinator proxy. Rendering is validation, not qualification.

## 3. Fake-endpoint Compose harness exercise

Run the checked-in host qualification with fake endpoints only. Its evidence path must be outside Git and access-controlled.

```bash
cd PythonDataService
.venv/bin/python -m scripts.run_broker_fleet_compose_qualification --evidence-path <safe-output>/fleet-compose-qualification.json
```

The command uses a scoped random Compose project and tears it down with volumes unless `--keep` is explicitly needed for failure investigation. Preserve the redacted transcript and evidence JSON. It exercises fake volume/source probes, coordinator-custody absence probes, Paper kill, Live read, and Live mutation refusal through containers. Do not replace it with mocks, in-process transport, a unit test, or an unrelated full suite.

Its fake/canned probes do **not** qualify production physical mounts, secret injection, a real broker or market-data upstream, host resource contention, or operator readiness. Do not write `deployment qualified` or `operational rollout complete` from this command alone.

Abort on a shared writable mount, secret leakage, exposed lane port, missing/incorrect lane identity, unexpected mutation, coordinator custody data, or any Paper fault that prevents correctly identified Live reads within its own resource budget. Record capacity overloads as `fleet_lane_capacity_exhausted`; do not tune around a fault without a new reviewed capacity plan.

## 4. Deployment-qualification fault matrix

Before Paper credentials are introduced, run and record the D fault matrix against the actual fleet invocation and actual role images. A canned harness may help choose cases; it cannot close this checklist.

| Fault or boundary | Required observation | Abort condition |
|---|---|---|
| Legacy-role suppression | `config --services` contains no combined Python service; backend base URL is the coordinator | Any fleet invocation starts or routes through combined |
| Ingress and agent reachability | Coordinator alone is host-published; agents are private-only; backend requests reach coordinator | Published agent port, direct agent ingress, or backend base URL to legacy Python |
| Volumes and writable roots | Three actual named sources differ; each lane-local writable root is under its own mount; coordinator has no lane custody | Shared/mis-mounted/copyable root or coordinator custody data |
| Credentials and transport tokens | Coordinator lacks broker credentials; Paper/Live files and two direction tokens remain separate | Cross-lane token/credential visibility or a secret in an artifact/log |
| Market-data wiring | Each lane connects with a distinct read-only client ID and retains its own status evidence | Coordinator feed use, cross-lane client ID/state, or deprecated control route |
| Paper process, DB/volume, credential, stream, and queue faults | Live remains readable, identity-correct, and within its own resource budget | Live restart, retarget, corruption, lost bounded capacity, or mutation |
| Coordinator outage/restart | Existing verified same-Clerk recovery remains narrow; new authority changes and browser commands close | Assignment release/takeover, widened recovery, or implicit target |
| Live mutation boundary | Live read-only can run, but unapproved mutation refuses | An unarmed/unapproved command reaches a broker |

Only a complete, reviewed actual-deployment record can mark `deployment qualified`.

## 5. Paper canary

Only after the fake Compose record passes, replace only Paper's fake credential slot with the approved Paper account credential. Keep Live on fake/no-command posture. Complete existing Paper validation, program-build, exact account/program canary admission, profile/binding, custody, and market-data gates. Record the immutable Clerk/account/binding target, bounded duration, command identities, provider receipts, resource observations, and stop criteria.

Stop and leave Paper stopped for missing or stale proof, route/account mismatch, unexpected command/outcome, feed-health failure, capacity breach, or inability to reconcile a command identity. A successful Paper canary is not numerical-equivalence proof and does not authorize Live.

## 6. Live read-only qualification

Only after the Paper canary record is accepted, replace only Live's fake credential with the approved read-only Live posture. Run scoped reads and streams through `fleet-coordinator`; retain exact broker/Clerk/account/binding/epoch correlation; and prove mutation remains closed absent independently satisfied existing gates. Do not arm Live.

Abort on a mutation, implicit target, cross-lane response or event, stale provenance, missing market-status evidence, degraded Live capacity, or unverified legacy/combined fallback. Read-only success does not authorize a Live command.

## 7. Separately authorize an eligible Live command

This is an operator decision, not a deployment step. The named authorizer records the existing eligible command, frozen target, idempotency identity, reconciliation plan, and proof that the unchanged Live envelope, host-only arming, custody, risk, capability, exact binding/generation, and readiness gates admit it. An `outcome_unknown` triggers read-only reconciliation by the same command identity; it never triggers automatic resubmission or a replacement-agent command.

Without that separate record, stop at `Live read-only qualified`.

## 8. Begin compatibility observation before cutover and retain it

Before switching off the combined role, retain its direct runtime aggregate from `$ALPACA_CLERK_DIR/compatibility/route_hits.json`; the same collector runs in `combined` and later in each `clerk_agent`. Schema v2 is `schema_version`, `updated_at_ms`, and sorted route-hit entries (`route_family`, `response_class`, `count`, `first_observed_at_ms`, `last_observed_at_ms`). `method` is intentionally omitted because only `GET`/`HEAD` reads are eligible. Retain the combined and post-cutover lane aggregates plus consumer-inventory reference in the restricted rollout record until E closes its retirement decision. Do not retain raw URLs, parameters, headers, bodies, identities, credentials, or raw access logs in fleet evidence.

E, not D, selects whether the window is representative, reconciles consumers, and accepts or rejects route retirement. Zero traffic in an idle window is not retirement proof.
