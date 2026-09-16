# Dev-stack two-lane fleet posture (coordinator + Live + Paper clerks)

**Status:** Operational record for the dev machine, updated 2026-09-15. The
Live desk is qualified through the browser path end-to-end. The Paper lane
**activated 2026-09-15** — the fresh-account cutover ceremony was executed
(generation 1; both lanes `ready` side by side; see
[Paper activation boundary](#paper-activation-boundary)). Two gaps remain,
both product decisions, not deployment errors: the desk cannot discover or
drive the activation ceremony (#2139), and recovering the lane's
pre-existing *legacy* paper account is still fenced. Do not paper over
either by copying custody data across lanes.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md),
the [two-Clerk rollout runbook](fleet-d-two-clerk-rollout.md) (this posture is
that role split applied to the dev compose project instead of the separate
`learn-ai-fleet` overlay), and the [provider recovery
procedure](alpaca-sqlite-clerk-recovery-and-cutover.md) for custody questions.
For onboarding a new account onto one of these lanes end to end, see
[`add-an-alpaca-account.md`](add-an-alpaca-account.md), which cross-links the
provisioning and secrets-layout sections below by name.

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
| `alpaca-paper-clerk` | `clerk_agent` | `learn-ai-alpaca-paper-clerk-data` (fresh) | ready; fresh `PA*` account activated 2026-09-15 (cutover ceremony, generation 1), real_paper authority, heartbeating |

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

## Fresh-lane bootstrap (first binding)

Every **online** lane heartbeats from the moment it opens — before the
installation lock, before the profiles database, before any binding
(`start_heartbeat`, `app/broker/alpaca/clerk/fleet_boot.py`, called from
`lifespan`). Whether or not a profile exists, and whether or not a binding
installs at all, the lane reports `binding_pending` and projects `starting`,
so `configuration_access` reads route to it normally and there is no window to
race. Its summary shows endpoint mode `unidentified` until the first binding
installs; `confirm_and_report` then swaps the reported facts under the running
beat and the directory shows `paper` or `live`. The beat stops when the lane
closes. Execution routing still stays closed until a binding is confirmed. The
bootstrap sequence for a fresh lane:

1. `podman restart alpaca-paper-clerk`, then wait for its HTTP surface **from
   the coordinator's network**: `podman exec polygon-data-service python -c
   "import urllib.request; urllib.request.urlopen('http://alpaca-paper-clerk:8000/health',
   timeout=2).read()"`.
2. Bind it through the configuration page: create profile (`credential_slot`
   + `endpoint_mode`) → `verify-account` → `account-pin` → stage selection →
   `selection/apply` (202). Driving the same calls through the coordinator by
   hand needs control headers, and each mutating call needs a
   `command_context` envelope with `capability: "configuration_manage"`.
3. Restart the lane again: the applied selection binds, the account is
   reserved, and — if the clerk authority composes — the binding confirms and
   the heartbeat switches to `binding_confirmed`.

## Paper activation boundary

The paper lane reached `ACTIVATION_REQUIRED`: `select_active_clerk_runtime`
composes paper custody only from an activated SQLite authority, and the paper
cutover (`cutover-initialize`) used to **refuse an empty legacy inventory for
paper accounts** (`_live_evidence_permits_empty_legacy` — only live-mode
evidence stood in).

**Resolved 2026-09-14 (owner decision) for a fresh account:** that live-only
exception is gone. A never-legacy account of either mode now completes
`initialize`/`plan`/`apply` with an empty legacy artifact set and an empty
runner roster — the flat-and-order-free check, unconditional for both modes,
is what makes this safe. See ADR 0059's 2026-09-14 amendment.

**Executed 2026-09-15.** The fresh-account ceremony ran against this lane
(owner decision on record: the pinned `PA*` account's prior trading history
is ignored; the flat-and-order-free gate — verified against the live paper
API — is the guard that matters):

1. Create the empty `live_state/` directory the fresh volume lacked
   (`read_quiescent_alpaca_roster` refuses on a missing root *before* the
   empty-roster allowance can apply).
2. Run `cutover-initialize → backup → cutover-plan → cutover-apply` inside
   `alpaca-paper-clerk` (`/opt/venv/bin/python -m
   scripts.manage_alpaca_sqlite_clerk`, flags per the [provider recovery
   procedure](alpaca-sqlite-clerk-recovery-and-cutover.md)), each
   evidence-gated step using a broker-evidence JSON captured seconds earlier
   from the paper API; retain the API responses on the volume (e.g. under
   `broker_captures/cutover-<date>/`) as the `proof_reference` targets.
3. Restart the lane: authority opens, `effective_binding_generation` moves
   0→1, and the directory shows both lanes `ready` (paper summary
   `paper real_paper`). Receipts live on the paper volume under
   `accounts/alpaca/<account>/cutover-evidence/` and `verified-backups/`.

Two operational facts this execution pinned:

- **A WAL/SHM sidecar refuses planning, and the fix is a checkpoint, never
  file deletion.** A lane whose clerk stays up can accumulate a stale
  0-byte `clerk.db-wal` + `clerk.db-shm` pair from a connection that exited
  without a clean close (the refusal text itself says "remove no files
  manually"). With no process holding the database, open it once —
  `PRAGMA wal_checkpoint(TRUNCATE)` — and close cleanly; SQLite removes the
  sidecars itself.
- **`cutover-apply` must reuse the plan's exact evidence file.** Apply
  refuses broker evidence whose normalized form differs from the plan's
  (`normalized_broker != plan.broker_evidence`) and separately re-checks
  the `--max-evidence-age-ms` window — so capture evidence **once**, run
  `cutover-plan`, then `cutover-apply` with the same file, all inside that
  one capture's window.

The ceremony itself remains CLI-only by design; no desk or fleet-CLI path
can discover or drive it (#2139).

**Still open — recovering the *existing* legacy paper account onto this
lane.** That account's legacy artifacts and activation records live on
`learn-ai-alpaca-clerk-data` (the Live lane's volume), and every documented
move is fenced:

- copying profiles/DBs/receipts/custody across lanes is forbidden (fleet E),
- `restore` accepts only bundles matching the *same volume's* registry and
  mirror head,
- `reassign-assignment` moves an account between clerks, not custody between
  volumes.

So the *existing* paper account still cannot be split onto its own lane with
today's ceremonies — that needs a cross-lane custody migration ceremony or an
activation path for legacy-carrying accounts on a new volume, which is a
separate, still-open product decision. Until then that specific account's
assignment stays reserved to wherever its custody lives — correctly fenced,
not orphaned to another lane.

## Migrating to the committed topology

**Status:** written, not executed. This section is the operator sequence for
moving this machine from the untracked `compose.override.yaml` (the sole
durable copy of every fleet credential today) onto the committed
`compose.fleet.dev.yaml` + gitignored `deploy/fleet/env/*.env` — Delivery
F-1's code half (issue #2066). Running it is a separate, deliberate owner
action in a no-bot window; nothing in this section executes on its own.

**The trap this migration exists to fix:** `env_file: required: true` was
already wired for the live and paper clerks before this migration, but the
override *also* re-declares the same four keys (`FLEET_CLERK_ID`,
`FLEET_WORKER_KEY`, `FLEET_AGENT_SERVICE_TOKEN`,
`FLEET_COORDINATOR_SERVICE_TOKEN`) as literals under `environment:`, and
Compose's `environment:` wins over `env_file:` key-for-key. M3–M4 below is a
**deletion** of those literals, not an addition of env-file content — adding
env-file content alone changes nothing observable.

### Prerequisites

- Outside market hours, no bot running. M6 restarts both clerks.
- `podman machine` running; free disk for the registry backup below.
- Read this whole section once before running anything in it.

### M1–M9

```bash
set -euo pipefail
cd /Users/inkant/learn-ai
STAMP=$(date -u +%Y%m%dT%H%M%SZ); SAFE=~/.fleet-migration/$STAMP
mkdir -p "$SAFE"; chmod 700 ~/.fleet-migration "$SAFE"

# M1 — capture the running truth BEFORE anything changes. Outside the repo,
# 0600. compose.override.yaml is still the only durable copy of every fleet
# credential at this point — nothing may touch it before it is backed up.
cp compose.override.yaml "$SAFE/compose.override.yaml.bak"; chmod 600 "$SAFE"/*
for c in polygon-data-service alpaca-live-clerk alpaca-paper-clerk; do
  podman inspect "$c" --format '{{json .Config.Env}}'  > "$SAFE/$c.env.json"
  podman inspect "$c" --format '{{json .Mounts}}'      > "$SAFE/$c.mounts.json"
done
chmod 600 "$SAFE"/*.json
podman volume ls --format '{{.Name}}' | sort > "$SAFE/volumes.before.txt"
podman ps --format '{{.Names}}\t{{.Status}}' > "$SAFE/ps.before.txt"

# M2 — registry insurance (the ceremony already documented above in this
# runbook, § Ceremonies performed).
podman run --rm -e POLYGON_API_KEY=standin \
  -v learn-ai_alpaca-fleet-control:/app/artifacts/fleet:z -v "$SAFE":/backup:z \
  learn-ai-python-service:latest python -m scripts.manage_broker_fleet backup-registry \
    --control-dir /app/artifacts/fleet --backup-dir /backup/registry-$STAMP

# M3 — move the four identity/token keys per lane out of the override and
# into the env files that already exist (deploy/fleet/env/live.env,
# deploy/fleet/env/paper.env). Read them from $SAFE/<clerk>.env.json, append
# to the matching lane file, chmod 600. Create deploy/fleet/env/coordinator.env
# from deploy/fleet/env/coordinator.env.example and move
# FLEET_AGENT_SERVICE_TOKENS_JSON / FLEET_COORDINATOR_SERVICE_TOKENS_JSON
# into it. Never echo a value; never use shell history for a secret value.
#
# DATA_PLANE_CONTROL_SECRET is DIFFERENT — do NOT remove it from the root
# .env, even though coordinator.env.example also lists it. compose.yaml's
# OWN `environment:` entries on python-service, backend, and frontend all
# interpolate `${DATA_PLANE_CONTROL_SECRET:?Set ... before starting the
# stack}` directly from the root .env / process environment, and that
# `environment:` entry always outranks whatever coordinator.env's `env_file:`
# supplies for the same key — the identical rule that makes the four
# identity/token keys above env_file-only in the first place, just cutting
# the other way here because compose.yaml (not compose.fleet.dev.yaml) is
# the one declaring the literal. Removing it from .env does not "move" it to
# the coordinator file; it just breaks M5's render with a missing-variable
# error, before the coordinator env file gets a chance to matter. Leave it in
# .env; copying it into coordinator.env as well is harmless documentation,
# not a functional relocation.
chmod 600 deploy/fleet/env/coordinator.env deploy/fleet/env/paper.env deploy/fleet/env/live.env

# M4 — the deletion. Remove the fleet content from compose.override.yaml
# entirely — it now lives in the committed compose.fleet.dev.yaml. Keep ONLY
# the dev ergonomics that were never fleet-specific: the backend port 5050
# remap, the frontend 6G memory bump, and the Frontend/angular.json bind.

# M4a — verify the deletion actually took. Skipping this is how the old
# literal credentials keep silently overriding the new env files with
# nothing detecting it: M5's `config --services` reports the same seven
# service names whether or not the override still carries fleet content
# (the override never added or removed a service, only environment/volumes
# on ones compose.yaml already declares); render_fleet_topology.py is
# hard-coded to compose.yaml + compose.fleet.dev.yaml and never reads
# compose.override.yaml at all; and M8 below compares key NAMES only, which
# match whether the override's copy is a literal or absent. Key names only,
# never a value — `grep -q` never prints what it matches.
if grep -qE '^\s*(alpaca-live-clerk|alpaca-paper-clerk):' compose.override.yaml; then
  echo "compose.override.yaml still declares a clerk service — M4 is incomplete." >&2
  exit 1
fi
if grep -qE '\b(FLEET_CLERK_ID|FLEET_WORKER_KEY|FLEET_AGENT_SERVICE_TOKEN|FLEET_COORDINATOR_SERVICE_TOKEN|FLEET_AGENT_SERVICE_TOKENS_JSON|FLEET_COORDINATOR_SERVICE_TOKENS_JSON):' compose.override.yaml; then
  echo "compose.override.yaml still declares a fleet credential key — M4 is incomplete." >&2
  exit 1
fi
echo "M4 verified: compose.override.yaml carries no fleet service or credential key."

# M5 — render and diff BEFORE starting anything. This is the gate. Run it
# with the SAME engine the host actually uses (podman compose), not docker
# compose — CI's render gate only proves the docker-compose half; the two
# engines can disagree on `!override` merge order, `deploy.resources` vs
# top-level `cpus`/`mem_limit`, and `:z` relabel suffixes.
podman compose --project-name learn-ai \
  -f compose.yaml -f compose.fleet.dev.yaml -f compose.override.yaml \
  config --services | sort > "$SAFE/services.after.txt"
diff -u <(printf '%s\n' alpaca-live-clerk alpaca-paper-clerk backend db frontend python-service redis | sort) \
        "$SAFE/services.after.txt"
python scripts/render_fleet_topology.py --engine "podman compose" --check   # must exit 0
```

**Do not proceed past M5 if `--check` fails.** podman and docker rendered the
same file differently, and that mismatch is itself the finding — stop and
reconcile it before any container is touched, not after.

```bash
# M6 — the restart. THIS DROPS BOTH CLERKS. Confirm (again) outside market
# hours, no bot running.
./restart.sh

# M7 — verify SAME volumes, SAME markers. A wrong volume name here orphans
# Live custody.
podman inspect alpaca-live-clerk  --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}'
#   must contain: learn-ai-alpaca-clerk-data /app/artifacts/alpaca_clerk
podman inspect alpaca-paper-clerk --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}'
#   must contain: learn-ai-alpaca-paper-clerk-data /app/artifacts/alpaca_clerk
podman inspect polygon-data-service --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}'
#   must contain: learn-ai_alpaca-fleet-control /app/artifacts/fleet

podman exec alpaca-live-clerk  cat /app/artifacts/alpaca_clerk/.learn-ai-clerk-volume.json
#   clerk_id must be clrk_57f90423a8504d2d3dd4af77, volume_id vol_aea84c3d8bcfa734eed761ba,
#   attestation_id alpaca-clerk-data   (the marker is documented non-secret; see
#   § Topology on this machine above)
podman exec alpaca-paper-clerk cat /app/artifacts/alpaca_clerk/.learn-ai-clerk-volume.json
#   clerk_id clrk_ae24bafc273728d023ff0eac, attestation_id alpaca-paper-clerk-data

podman volume ls --format '{{.Name}}' | sort | diff -u "$SAFE/volumes.before.txt" -
#   MUST be empty. Any NEW volume means a rename happened — stop and roll back.

# M8 — env-key parity (names only, never values)
for c in polygon-data-service alpaca-live-clerk alpaca-paper-clerk; do
  diff -u <(jq -r '.[]|split("=")[0]' "$SAFE/$c.env.json" | sort) \
          <(podman inspect "$c" --format '{{json .Config.Env}}' | jq -r '.[]|split("=")[0]' | sort)
done

# M9 — the lanes are actually serving
curl -sf localhost:8000/api/broker-clerks \
  -H "X-Data-Plane-Control-Intent: learn-ai-browser-control" \
  -H "X-Data-Plane-Control-Secret: $(grep ^DATA_PLANE_CONTROL_SECRET= .env | cut -d= -f2-)" \
  | jq '.clerks[] | {clerk_id, lifecycle_state}'
podman logs --since 5m alpaca-live-clerk | grep -Ei 'marker|refus' || true   # expect nothing
```

**A fail-closed property worth knowing before M6, not discovering at 16:30:**
`alpaca-clerk-data` is declared `external: true` in `compose.yaml`. `down
--volumes` will not remove it (good — Live custody survives a teardown), but
`up` **fails fast** if that volume is missing rather than silently creating a
fresh, empty one in its place. A missing-volume failure at M6 is the safe
outcome, not a surprise to work around.

**Rollback:** *not* `cp "$SAFE/compose.override.yaml.bak" compose.override.yaml`
— M4 already deleted the fleet content from the live override, and restoring
the backup verbatim would resurrect the four literal-credential lines this
migration exists to remove.

**Remove both clerk containers BEFORE dropping the overlay — never after.**
Moving `compose.fleet.dev.yaml` away and running `./restart.sh` calls `podman
compose down` against a model that no longer declares either clerk service.
Compose does not remove orphaned services unless told to, so both clerks can
keep running while `python-service` restarts in the default `combined` role
— which mounts the SAME Live custody volume (`alpaca-clerk-data`) the still-running
live clerk already holds. That is two processes with concurrent authority
over one SQLite custody state, not a cosmetic ordering nit. Remove the
clerks explicitly, first:

```bash
podman rm -f alpaca-live-clerk alpaca-paper-clerk
mv compose.fleet.dev.yaml /tmp/ && ./restart.sh
```

Volumes were never renamed at any point in M1–M9, so nothing is lost either
way.

### Reclaiming a failed qualification run's isolation

If `scripts/run_broker_fleet_compose_qualification.py` (issue #2070) has been
run on this host, its own rule is that unreclaimed isolation invalidates a
pass — reclaim before a new run and before any migration step above:

```bash
podman volume ls --format '{{.Name}}' | grep '^fleetqualification' | sort
# Confirm each has NO container attached before removing anything:
for v in $(podman volume ls --format '{{.Name}}' | grep '^fleetqualification'); do
  printf '%s -> ' "$v"; podman ps -a --filter volume="$v" --format '{{.Names}}' | tr '\n' ' '; echo
done
# Every line must show no container. Only then:
podman volume ls --format '{{.Name}}' | grep '^fleetqualification' | xargs -r podman volume rm
```

**Never widen this to `podman volume prune`.** While the stack is down,
`learn-ai_alpaca-fleet-control` — the Live registry — has no attached
container and is exactly the kind of volume a prune removes. That volume must
never be a prune target; the explicit `grep '^fleetqualification' | xargs`
form above is the only form used against this host.

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
3. `podman rm -f alpaca-live-clerk alpaca-paper-clerk` **before** starting
   `python-service` — not after. The combined role mounts the same Live
   custody volume (`alpaca-clerk-data`) the live clerk still holds; starting
   `python-service` first would give two processes concurrent authority over
   one SQLite custody state for however long it takes to reach the `rm -f`.
   Then `podman compose -f compose.yaml -f compose.override.yaml up -d python-service`.
4. Optionally retire the paper clerk host-side (`manage_broker_fleet retire`)
   to drop it from the directory.
