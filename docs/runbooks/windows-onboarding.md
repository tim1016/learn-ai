# Onboarding — running MarketScope (learn-ai) on a Windows machine

Written 2026-09-12 while bringing the stack up on a fresh Windows 11 PC
(Git Bash shell). Everything runs as containers under Podman Compose on the
WSL2 backend; the host only needs Git, WSL2, Podman, and docker-compose.
Last verified state: all five containers healthy, app at http://localhost:4200,
live Polygon / FRED / Alpaca keys configured.

## 1. First-time machine setup (one-time)

1. **WSL2** — elevated PowerShell: `wsl --install --no-distribution`, then
   **reboot**. Verify with `wsl --status` (Default Version: 2). If a
   "virtualization is not enabled" error appears right after install, it
   usually clears after the reboot + `wsl --update`.
2. **Podman** — `winget install RedHat.Podman`. Binary:
   `C:\Program Files\RedHat\Podman\podman.exe` (add to PATH).
3. **docker-compose** — `winget install Docker.DockerCompose` (podman
   `compose` shells out to it). Binary:
   `C:\Users\inkan\AppData\Local\Microsoft\WinGet\Packages\Docker.DockerCompose_Microsoft.Winget.Source_8wekyb3d8bbwe\`
   (add to PATH; shells opened before install don't see it).
4. **Podman machine** — `podman machine init --cpus 4 --memory 8192`.

PATH for a Git Bash session, in short:
```bash
export PATH="/c/Program Files/RedHat/Podman:/c/Users/inkan/AppData/Local/Microsoft/WinGet/Packages/Docker.DockerCompose_Microsoft.Winget.Source_8wekyb3d8bbwe:$PATH"
```

## 2. Host directories (compose bind-mounts them)

Rootless Podman does NOT auto-create missing host paths — each missing dir
fails `compose up` with `statfs ... no such file or directory`. Create:

```
../Lean/Data                      # sibling of the repo (LEAN data, read-only)
data-lake-volume/                 # repo root (data-lake writer root)
PythonDataService/cache/
PythonDataService/lean-cache/
PythonDataService/artifacts/alpaca_clerk/
```

## 3. Environment files (all gitignored — never commit)

Three files must exist before first start. See the current machine's files
for working examples; required values:

**Repo-root `.env`** (compose interpolation; copy from `.env.example`):
- `POLYGON_API_KEY`, `FRED_API_KEY` — real API keys
- `POSTGRES_PASSWORD`, `REDIS_PASSWORD` (URL-safe) — generate random
- `DATA_PLANE_CONTROL_SECRET` — generate 32 url-safe random bytes; the SAME
  value must appear in `PythonDataService/.env` (see below)
- Optional: `IBKR_*`, `LEAN_LAUNCHER_URL`, `PRIMEUI_LICENSE_KEY` (inert here
  — the license belongs in the Angular env file, §4)
- Do NOT set `LEAN_DATA_VOLUME_HOST_PATH` to an old machine's absolute path
  (e.g. the macOS `/Users/...` path); keep it commented so compose uses the
  repo-relative default.

**`PythonDataService/.env`** (the running data plane reads ONLY this file —
compose `env_file`; copy from its `.env.example`):
- `POLYGON_API_KEY` — same key as root `.env` (must be in BOTH)
- `DATA_PLANE_CONTROL_SECRET` — MUST equal the root `.env` value; a mismatch
  breaks host-side tooling and the "same secret" contract with backend/proxy
- Alpaca credential slots (optional, for broker features):
  `ALPACA_API_KEY_ID`/`ALPACA_API_SECRET_KEY` (paper, "default" slot) and
  `ALPACA_CREDENTIAL_LIVE_KEY_ID`/`ALPACA_CREDENTIAL_LIVE_SECRET_KEY` ("live"
  slot). One `ALPACA_MARKET_ENDPOINT` serves both — with paper creds in the
  default slot it must stay `https://paper-api.alpaca.markets/v2`. Per ADR
  0060 the effective endpoint mode is a broker profile saved in the UI.
- `DATA_LAKE_ROOT_ID` — the UUID from §5; losing this makes the service
  refuse to start (falls back to the legacy all-zeros id and aborts)
- Do NOT re-add the RETIRED `ALPACA_MODE` / `ALPACA_LIVE_*` variables (see
  the file's own ADR 0060 note).

**`Frontend/src/environments/environment.development.ts`** — copy from
`environment.development.ts.example`, then fill `primeUiLicense` (PrimeNG
license key) and optionally `polygonApiKey`. Without this file `ng serve`
crash-loops with "file replacements does not exist".

## 4. First-run ceremonies (one-time, both required)

**a) Alpaca Clerk volume.** compose declares `learn-ai-alpaca-clerk-data`
as `external: true`, and python-service exits 78 until the volume contains
a regular empty file `_compose_volume_ready`. For a deliberately empty
first install (legacy host tree empty):
```bash
podman volume create learn-ai-alpaca-clerk-data
podman run --rm -v learn-ai-alpaca-clerk-data:/data alpine \
  sh -c ': > /data/_compose_volume_ready'
```
When migrating an existing authority tree instead, follow
`docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` (copy the whole
tree, never just `clerk.db`; verify before sealing).

**b) Data-lake root identity (#1876).** python-service aborts startup until
the lake root is claimed. Generate a UUID once, then:
```bash
podman compose up -d redis db python-service   # boots once marker (a) exists
podman exec polygon-data-service python -m scripts.manage_data_root init --root-id <uuid>
```
Put `DATA_LAKE_ROOT_ID=<uuid>` in `PythonDataService/.env`, then recreate:
`podman compose up -d python-service`.
This machine's UUID: `77a98bfb-e395-4dfc-ab52-3c0928fa20a2`.

## 5. Build & start

```bash
export COMPOSE_BAKE=false     # podman socket has no BuildKit; avoid the bake fallback warning
podman compose build          # first build ~5-10 min (python-service + frontend images)
./restart.sh                  # or: podman compose up -d
```

`restart.sh` also recovers dependents left in `Created` when a
`depends_on: service_healthy` gate misses, and polls health for 240 s.

## 6. Daily run / shutdown

After a reboot:
```bash
podman machine start
./restart.sh                  # or: podman compose up -d
```

- App: http://localhost:4200
- Python data plane: http://localhost:8000/health
- .NET backend: http://localhost:5000/health

Shutdown: `podman compose down` (add `podman machine stop` to stop the VM).
All ports are loopback-only (127.0.0.1).

## 7. Gotchas learned on 2026-09-12

- **Stale PATH**: after installing Podman/docker-compose, existing Git Bash
  sessions can't find them. Re-add to PATH or open a new shell.
- **Bind mounts don't auto-create** (§2) — the two `statfs` failures during
  first `compose up` were exactly this.
- **Frontend crash-loop** = missing `environment.development.ts` (§3).
- **python-service exit 3** at startup with `LakeRootIdentityError` = §4b
  (missing marker) or a `DATA_LAKE_ROOT_ID` that doesn't match the marker.
- **python-service exit 78** = clerk volume marker missing (§4a).
- **IBKR "Connection refused"** warnings in python-service logs are normal
  without IB Gateway running; they don't affect health. IBKR bot-control is
  deprecated — new broker work targets Alpaca V2 (`/brokers/alpaca/...`).
- **Repo lives in OneDrive** — works, but the dev server uses `--poll 2000`
  file watching (already configured in compose) to tolerate sync latency.
- Editing `PythonDataService/.env` requires recreating the service to take
  effect: `podman compose up -d python-service`.
- Verify the whole stack: `podman ps` should show all five containers
  `(healthy)`; then curl the three URLs in §6.
