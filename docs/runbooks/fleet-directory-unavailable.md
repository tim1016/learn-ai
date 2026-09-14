# Alpaca desk: "The fleet directory is unavailable" — operator runbook

**Status:** Incident and recovery procedure for the Alpaca broker desk's lane-directory failure state. Covers the combined-role dev deployment (`compose.yaml` + `compose.override.yaml`) and points to the overlay procedures where they apply.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md), the [multi-broker Clerk PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md) §10.1 (directory) and §13/FR-092 (canonical clerk routes), [Delivery D two-Clerk rollout](fleet-d-two-clerk-rollout.md) for the overlay posture, and [registry recovery](fleet-e-registry-recovery-exercise.md) for control-volume backup/restore.

## What the message means

The banner renders from `Frontend/src/app/components/brokers/alpaca-desk/lane-directory/alpaca-lane-directory.component.html` when `FleetDirectoryService`'s single `GET /api/broker-clerks` request fails (any non-2xx or network error). It is **not** GraphQL and **not** the .NET backend: the directory is a REST route served by the **Python fleet coordinator surface**, which mounts only when `FLEET_CONTROL_DIR` is set on a `combined` or `fleet_coordinator` role process (`PythonDataService/app/main.py`, `_FLEET_COORDINATOR_SURFACE`).

Two distinct states are easy to confuse:

- *Error banner* ("unavailable") — the HTTP call failed. This runbook.
- *"No Alpaca clerk lanes are registered"* — the call succeeded (`200`) and the registry is simply empty; run the enrolment ceremony in §4.

While the directory is down, **clerk-scoped routes keep working** — `/brokers/alpaca/clerks/{clerkId}...` are statically declared with no directory-dependent guard. A down directory degrades discovery, not operation. Deep-link URLs are stable: `brokers/alpaca/clerks/:clerkId/configuration`, `.../accounts/:accountId` (desk), `.../bots`, `.../gallery`.

## 1. Reproduce and classify (the feedback loop)

One curl, replicating the browser's exact request through the dev proxy (which is what the banner observes):

```bash
curl -i 'http://localhost:4200/api/broker-clerks' \
  -H 'Origin: http://localhost:4200' \
  -H 'Referer: http://localhost:4200/brokers/alpaca' \
  -H 'sec-fetch-site: same-origin' \
  -H 'X-Data-Plane-Control-Intent: learn-ai-browser-control'
```

Also check the data plane directly (no proxy in the path):

```bash
curl -i http://localhost:8000/api/broker-clerks
curl -s http://localhost:8000/openapi.json | python3 -c 'import json,sys; print(any("broker-clerks" in p for p in json.load(sys.stdin)["paths"]))'
```

Classify by what you see:

| Observation | Cause | Go to |
|---|---|---|
| Proxy 404, and the direct/openapi probe shows no `broker-clerks` route | Fleet coordinator surface not installed: `FLEET_CONTROL_DIR` unset on the Python process (the default combined deployment), or the frontend's `DATA_PLANE_PROXY_TARGET` points at a process without the surface | §2 |
| Proxy 403 `missing or wrong X-Data-Plane-Control-Secret` | The dev proxy did not attach the control secret: the path is missing from `contracts/data-plane-control-surfaces.json` `protected_read_prefixes`, or the request lacked the intent header / same-origin browser metadata | §3 |
| 503 `no fleet registry is installed on this process` | `FLEET_CONTROL_DIR` is set but no registry exists at that root | §4 (init), §5 (wiring) |
| 503 naming `DATA_PLANE_CONTROL_SECRET` (retired/required) | The control secret uses the retired public value or is unset while unauthenticated control is disallowed | Rotate the secret in `PythonDataService/.env`; never commit it |
| 200 with empty `clerks` | Registry has no enrolled lanes | §4 (enrol existing volume) |
| 200 but lane `lifecycle_state` ≠ `ready` | Lane registered but presence stale (heartbeat window ~30 s) or boot refused — check `podman logs polygon-data-service` for `FleetBootRefused` | §5 |

Always also read `podman logs polygon-data-service` — a `FleetBootRefused` line names the exact fence that refused boot (writable-root fence, missing marker, unreachable coordinator, evidence mismatch).

## 2. Coordinator surface not installed (the 404 family)

The base `compose.yaml` Python service runs the legacy combined posture with no fleet configuration, so `broker_clerks` (and the clerk-scoped operation routes under `/api/brokers/{broker}/clerks/...`) never mount. The frontend at fleet-delivery HEAD expects them. Recovery is the enrolment + wiring in §4–§5. The other 404 shape — the proxy target pointing at a process without the surface — is checked with:

```bash
podman exec my-frontend printenv DATA_PLANE_PROXY_TARGET   # default http://127.0.0.1:8000
```

In the overlay posture this must be `http://fleet-coordinator:8000` (set by `compose.fleet.yaml`).

## 3. Control secret not attached (the 403 family)

`/api/broker-clerks` carries the always-on `require_data_plane_control_secret_always` guard. The dev proxy attaches the secret only for paths under `protected_read_prefixes` in `contracts/data-plane-control-surfaces.json`, and only when the request carries the browser intent header and same-origin fetch metadata. `/api/broker-clerks` was added to that manifest on 2026-09-14 (it sat outside the `/api/brokers` prefix); the pinning tests are `PythonDataService/tests/test_data_plane_control_security.py::test_fleet_directory_read_is_declared_in_shared_manifest` and the fleet-directory case in `Frontend/src/app/security/data-plane-control-intent.interceptor.spec.ts`. If the banner persists with 403 at HEAD, re-check those two places first.

## 4. Host ceremony: enrol the existing clerk lane (offline, resumable)

`scripts.manage_broker_fleet migrate-existing` enrols an **existing** Alpaca lane volume into a fleet registry — it preserves custody, profiles, and the confirmed binding, and seeds the binding generation from the volume's effective tuple. It is offline by contract: stop the Python service first.

```bash
# 0. stop the combined process (offline boundary)
podman compose stop python-service

# 1. create the control volume's registry (idempotent)
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_broker_fleet init --control-dir /app/artifacts/fleet

# 2. enrol the existing clerk volume
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_broker_fleet migrate-existing \
  --control-dir /app/artifacts/fleet \
  --volume-root /app/artifacts/alpaca_clerk \
  --attestation-id alpaca-clerk-data \
  --deployment-namespace compose:learn-ai

# 3. verify the volume identity gate
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_broker_fleet verify \
  --control-dir /app/artifacts/fleet \
  --clerk-id <clerk-id-from-step-2> \
  --volume-root /app/artifacts/alpaca_clerk
```

The `identities-issued` step prints `clerk_id`, `worker_key`, and two service tokens **once**. Persist them immediately in the uncommitted deployment environment (see §5). If they are lost: the worker key is recoverable from the registry (`read_clerk(...).worker_key` via the store API); the transport tokens are not — mint fresh ones with `rotate-credentials --clerk-id <id> --slot agent|coordinator`.

The registry lives on a **named volume, not the artifacts bind**: `registry.db` is SQLite WAL and the macOS virtiofs bind is not a valid WAL authority. The override masks it: `alpaca-fleet-control:/app/artifacts/fleet`.

## 5. Wire the combined-posture environment

Declare the posture in `compose.override.yaml` (gitignored) — **never** in `PythonDataService/.env`: pydantic reads that file from the pytest CWD too, and `FLEET_CONTROL_DIR` there flips the test-time app into the coordinator posture and breaks route-shape tests. The required `python-service.environment` entries:

```yaml
FLEET_CONTROL_DIR: /app/artifacts/fleet
FLEET_CLERK_ID: <clerk-id>
FLEET_WORKER_KEY: <worker-key>
FLEET_COORDINATOR_SERVICE_TOKEN: <rotated-token>   # lane-forward auth for the
                                                    # in-process dispatch
ALPACA_CLERK_DIR: /app/artifacts/alpaca_clerk
IBKR_LIVE_RUNS_ROOT: /app/artifacts/alpaca_clerk/live_runs    # fence: must live
IBKR_LIVE_BARS_ROOT: /app/artifacts/alpaca_clerk/live_bars    # inside the clerk
BROKER_CAPTURE_DIR: /app/artifacts/alpaca_clerk/broker_captures  # volume
TRUSTED_HOSTS: <defaults...,fleet-local>  # the in-process dispatch presents
                                          # Host: fleet-local
volumes:
  - alpaca-fleet-control:/app/artifacts/fleet
```

Notes from the 2026-09-14 incident:

- The **writable-root fence** refuses boot while `IBKR_LIVE_RUNS_ROOT`'s parent resolves outside the clerk volume. `compose.yaml` pins the shared-artifacts default for the deprecated host-side IBKR reconcile workflow, so the override must re-point it and the existing `live_state`/`live_bars` trees should be copied into the volume first (`cp -a` from a one-shot container, service stopped).
- `fleet-local` in `TRUSTED_HOSTS` is required because the combined posture's raw-ASGI lane dispatch presents that synthetic host and no default trusted-host list includes it.
- The combined posture dispatches in-process (`local_app`); `approve-endpoint` is not needed. It becomes required only in the split overlay posture.

Then recreate and verify:

```bash
podman compose up -d --force-recreate python-service
podman compose restart frontend   # proxy.conf.js re-reads the control-surfaces
                                  # manifest only at dev-server start
```

## 6. Verify recovery end to end

1. The §1 proxy curl returns `200` and the lane shows `lifecycle_state: "ready"` with a fresh `last_seen_at_ms`.
2. A clerk-scoped operating read succeeds through the browser path:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://localhost:4200/api/brokers/alpaca/clerks/<clerk-id>/account" \
  -H 'Origin: http://localhost:4200' -H 'sec-fetch-site: same-origin' \
  -H 'X-Data-Plane-Control-Intent: learn-ai-browser-control'
```

Expect `200` with live account figures, and `x-fleet-*` provenance headers on the response. The desk page (`/brokers/alpaca/clerks/<clerk-id>/accounts/<account>`) should render the account summary with no alert.

3. Regression suites stay green: `pytest tests/test_data_plane_control_security.py tests/broker/fleet/test_b_scoped_contracts.py` and `ng test --include='src/app/security/data-plane-control-intent.interceptor.spec.ts'`.

## 7. Posture boundaries and known gaps

- This runbook recovers the **combined** dev deployment. The production split posture (coordinator + Paper/Live agents) is the [Delivery D rollout](fleet-d-two-clerk-rollout.md); its enrolment provisions fresh volumes, not `migrate-existing`.
- Registry backup/restore (control volume) is the [Delivery E recovery exercise](fleet-e-registry-recovery-exercise.md). The clerk volume's own custody procedures are [provider-owned](alpaca-sqlite-clerk-recovery-and-cutover.md).
- `Frontend/scripts/verify-proxy-control-guard.cjs` fails on any host whose repo-root `.env` carries a valid `DATA_PLANE_CONTROL_SECRET` (the spawned child falls back to the root `.env` file and no longer rejects). Pre-existing, environment-only; CI (no root `.env`) is unaffected.
- Fixed at HEAD on 2026-09-14 and pinned by tests: the missing `/api/broker-clerks` manifest entry (§3), and the combined local delivery attaching the coordinator-token header with a mixed-case ASGI name so the lane-side guard never saw it (403 on every clerk-scoped read in the combined posture; `tests/broker/fleet/test_b_scoped_contracts.py::test_combined_local_delivery_routes_in_process` now enforces the secret so a broken dispatch cannot pass unauthenticated).
