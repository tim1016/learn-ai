# Dev-stack two-lane fleet posture (coordinator + Live + Paper clerks)

**Status:** Operational record for the dev machine, 2026-09-14. The Live desk
is qualified through the browser path end-to-end. The Paper lane is provisioned
and configured but is **blocked at paper custody activation** — a real product
gap, not a deployment error (see [Paper activation
boundary](#paper-activation-boundary)). Do not paper over that gap by copying
custody data across lanes.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md),
the [two-Clerk rollout runbook](fleet-d-two-clerk-rollout.md) (this posture is
that role split applied to the dev compose project instead of the separate
`learn-ai-fleet` overlay), and the [provider recovery
procedure](alpaca-sqlite-clerk-recovery-and-cutover.md) for custody questions.

## Why the combined role cannot host a second lane

`coordinator_delivery_for` (`app/broker/fleet/routing.py`) with `local_app`
set — the `combined` role — dispatches **every** clerk in-process and ignores
the session's endpoint. A remote agent registered to a combined coordinator
would have its operations served by the combined process's own lane; the
identity echo refuses (`DeliveryIdentityMismatch`) rather than leaking, but
the remote clerk stays permanently unroutable. Two simultaneous clerks
therefore require the role split: one coordinator process, one `clerk_agent`
container per lane.

One clerk lane also serves exactly one effective account by design:
`installation_selection` is singular, and `FleetControlService._descriptor`
flags multiple effective assignments as corruption (PRD §9.6).

## Topology on this machine

| Service (`compose.override.yaml`) | Role | Volume | State |
|---|---|---|---|
| `python-service` (`polygon-data-service`) | `fleet_coordinator` | `learn-ai_alpaca-fleet-control` at `/app/artifacts/fleet` | ready; owns data-plane core + fleet routing; **no** clerk volume, no broker credential |
| `alpaca-live-clerk` | `clerk_agent` | `learn-ai-alpaca-clerk-data` (the migrated lane volume) | ready; account `318420190` confirmed, shadow authority, heartbeating |
| `alpaca-paper-clerk` | `clerk_agent` | `learn-ai-alpaca-paper-clerk-data` (fresh) | provisioned; paper profile applied; `ACTIVATION_REQUIRED` |

Clerk IDs: Live `clrk_57f90423a8504d2d3dd4af77`, Paper
`clrk_ae24bafc273728d023ff0eac`. Approved endpoints: `alpaca-live-agent` →
`http://alpaca-live-clerk:8000`, `alpaca-paper-agent` →
`http://alpaca-paper-clerk:8000`. Both agents are private-only on
`app-network` (no host port); all browser traffic enters through the
coordinator.

## Secrets layout (all gitignored)

- `compose.override.yaml` — per-clerk `FLEET_CLERK_ID`, `FLEET_WORKER_KEY`,
  both transport tokens per lane, and the coordinator's
  `FLEET_AGENT_SERVICE_TOKENS_JSON` / `FLEET_COORDINATOR_SERVICE_TOKENS_JSON`
  maps. This file is the durable copy of every fleet credential.
- `deploy/fleet/env/live.env` — `ALPACA_CREDENTIAL_LIVE_*` (slot `live`).
- `deploy/fleet/env/paper.env` — `ALPACA_API_KEY_ID`/`_SECRET_KEY`
  (slot `default`). Both `chmod 600`.
- `PythonDataService/.env` — the four `ALPACA_*` credential lines were
  **removed** when this posture was installed; the coordinator must hold no
  Alpaca execution credential. `ALPACA_MARKET_ENDPOINT` /
  `ALPACA_FAULT_INJECTION_ENABLED` remain (coordinator-inert).

If a token is lost, `rotate-credentials --clerk-id <id> --slot agent|coordinator`
mints a fresh one; update compose.override.yaml and restart both sides.

## Ceremonies performed (host-side, one-shot containers)

```bash
# 0 — insurance before any registry mutation (output kept in ~/.fleet-recovery/)
podman run --rm -e POLYGON_API_KEY=standin \
  -v learn-ai_alpaca-fleet-control:/app/artifacts/fleet:z -v ~/.fleet-recovery:/backup:z \
  learn-ai-python-service:latest python -m scripts.manage_broker_fleet backup-registry \
    --control-dir /app/artifacts/fleet --backup-dir /backup/registry-<utc>

podman volume create learn-ai-alpaca-paper-clerk-data

# 1 — provision the paper lane (distinct ceremony mount path avoids the
#     root-collision fence; runtime mounts at /app/artifacts/alpaca_clerk)
podman run --rm -e POLYGON_API_KEY=standin \
  -v learn-ai_alpaca-fleet-control:/app/artifacts/fleet:z \
  -v learn-ai-alpaca-paper-clerk-data:/paper-volume:z \
  learn-ai-python-service:latest python -m scripts.manage_broker_fleet provision \
    --control-dir /app/artifacts/fleet --broker alpaca --label "Paper" \
    --volume-root /paper-volume --attestation-id alpaca-paper-clerk-data \
    --deployment-namespace compose:learn-ai

# 2 — endpoints for both lanes
... approve-endpoint --clerk-id clrk_ae24... --endpoint-ref alpaca-paper-agent \
    --base-url http://alpaca-paper-clerk:8000
... approve-endpoint --clerk-id clrk_57f... --endpoint-ref alpaca-live-agent \
    --base-url http://alpaca-live-clerk:8000

# 3 — the live lane's agent-direction token was never persisted (the combined
#     posture did not use it); mint one before its first clerk_agent boot
... rotate-credentials --clerk-id clrk_57f... --slot agent
```

One-time outputs went straight into `compose.override.yaml`; never into shell
history, tickets, or logs.

## Fresh-lane bootstrap (first binding) and its window caveat

A lane registers its session early in startup but uvicorn only accepts
connections after startup completes, and `start_heartbeat` begins only after a
**confirmed binding** (`app/main.py`). Until the first binding is confirmed
the session goes stale after 30 s and `resolve_route` refuses even
`configuration_access` operations. The bootstrap sequence that worked for the
paper lane:

1. `podman restart alpaca-paper-clerk`, then wait for its HTTP surface **from
   the coordinator's network**: `podman exec polygon-data-service python -c
   "import urllib.request; urllib.request.urlopen('http://alpaca-paper-clerk:8000/health',
   timeout=2).read()"`.
2. Within the remaining session window, through the coordinator (control
   headers), exactly the calls the configuration page makes: create profile
   (`credential_slot` + `endpoint_mode`) → `verify-account` → `account-pin` →
   stage selection → `selection/apply` (202). Mutating calls need a
   `command_context` envelope with `capability: "configuration_manage"`.
3. Restart the lane again: the applied selection binds, the account is
   reserved, and — if the clerk authority composes — the binding confirms and
   the heartbeat starts.

This window dance is a workaround for a product gap: a fresh lane should be
able to keep its session observable (or expose a sanctioned first-binding
path) without a confirmed binding. Flagged as follow-up; not fixed here.

## Paper activation boundary (open)

The paper lane reaches `ACTIVATION_REQUIRED`:
`select_active_clerk_runtime` composes paper custody only from an activated
SQLite authority, and the paper cutover (`cutover-initialize`) **refuses an
empty legacy inventory for paper accounts** (`_live_evidence_permits_empty_legacy`
— only live-mode evidence may stand in). The paper account's legacy artifacts
and activation records live on `learn-ai-alpaca-clerk-data` (the Live lane's
volume), and every documented move is fenced:

- copying profiles/DBs/receipts/custody across lanes is forbidden (fleet E),
- `restore` accepts only bundles matching the *same volume's* registry and
  mirror head,
- `reassign-assignment` moves an account between clerks, not custody between
  volumes.

So an existing paper account cannot be split onto its own lane with today's
ceremonies. Filling this needs a product decision (a cross-lane custody
migration ceremony, an activation path for legacy-carrying accounts on a new
volume, or accepting fresh-paper-account-only lanes). Until then the paper
desk stays dark and the paper account's assignment stays reserved to the
paper clerk — correctly fenced, not orphaned to another lane.

## Operations quick reference

```bash
# Directory (what the browser sees)
curl -s localhost:8000/api/broker-clerks \
  -H "X-Data-Plane-Control-Intent: learn-ai-browser-control" \
  -H "X-Data-Plane-Control-Secret: $(grep ^DATA_PLANE_CONTROL_SECRET= .env | cut -d= -f2-)"

podman logs -f alpaca-live-clerk     # binding / reserve / confirm / observe cadence
podman logs -f polygon-data-service  # coordinator; lane delivery failures land here
```

Expected: Live lane `lifecycle: ready`; heartbeats every 10 s
(`/internal/fleet/sessions/observe`). The live desk serves through
`localhost:4200/api/brokers/alpaca/clerks/clrk_57f.../account` (browser path
attaches the secret via `proxy.conf.js`).

## Rollback to the combined posture

1. Restore the credentials into `PythonDataService/.env` (the four
   `ALPACA_*` pairs now in `deploy/fleet/env/*.env`).
2. Replace `compose.override.yaml`'s fleet block with the pre-2026-09-14
   combined block: `python-service` with `FLEET_CONTROL_DIR`,
   `FLEET_CLERK_ID=clrk_57f...`, `FLEET_WORKER_KEY`,
   `FLEET_COORDINATOR_SERVICE_TOKEN`, the fenced roots and
   `TRUSTED_HOSTS=...,fleet-local`, the `alpaca-clerk-data` volume back on
   `python-service`, and no lane services.
3. `podman compose -f compose.yaml -f compose.override.yaml up -d python-service`
   then `podman rm -f alpaca-live-clerk alpaca-paper-clerk`.
4. Optionally retire the paper clerk host-side (`manage_broker_fleet retire`)
   to drop it from the directory.
