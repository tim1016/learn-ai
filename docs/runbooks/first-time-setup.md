# First-time setup — macOS

How to bring the stack up on a Mac. For Windows, use
[`windows-onboarding.md`](windows-onboarding.md).

Moved out of the README on 2026-09-28 and checked that day against
`compose.yaml`, `setup-macos.sh`, and `restart.sh`. Everything runs as
containers under Podman Compose; the host needs Homebrew and little else.

## 1. Prerequisites

- macOS on Apple Silicon, with [Homebrew](https://brew.sh/).
- The repo checked out **under `$HOME`**. The Podman VM bind-mounts `$HOME`
  into the guest, so a checkout elsewhere starts with empty mounts.
- A [Polygon.io](https://polygon.io/) API key. Optional: a
  [FRED](https://fred.stlouisfed.org/) key for risk-free rate curves.

`setup-macos.sh` installs the rest (`podman`, `docker-compose`, `node`).

## 2. Before the first start

These steps are the same on every OS, so they live in one place — sections
2–4 of [`windows-onboarding.md`](windows-onboarding.md):

- **§2 Host directories.** Rootless Podman does not create missing bind-mount
  paths; `compose up` fails with `statfs ... no such file or directory`.
- **§3 Environment files.** The repo-root `.env`, `PythonDataService/.env`,
  and `Frontend/src/environments/environment.development.ts`.
- **§4a Alpaca Clerk volume.** `learn-ai-alpaca-clerk-data` is an external
  volume; the data plane exits 78 until it holds `_compose_volume_ready`.
- **§4b Data-lake root identity.** The data plane exits 3 until the lake
  root is claimed; this one needs the data plane booted once, so do it after
  step 3 below.

**The data-plane control secret.** Compose refuses to start the data plane and
the frontend proxy while `DATA_PLANE_CONTROL_SECRET` in the root `.env` is
missing or blank; there is no checked-in development credential.
`setup-macos.sh` generates one on a fresh checkout and rotates the retired
`local-dev-control-secret` value on an upgrade, without overwriting a value
you set. By hand:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Broker configuration is not an environment setting.** Alpaca API credentials
stay in `PythonDataService/.env` — they are the credential *slots* a profile
refers to by name — but the endpoint mode (paper/live) and the six real-money
risk-envelope values are a **saved broker profile** on the Clerk volume,
created and edited in the browser and made effective by pressing Apply and
restarting (ADR 0060). An installation upgrading from the
environment-configured layout imports its existing values once with
`python -m scripts.manage_broker_configuration plan --plan-out …`, then deletes
the retired `ALPACA_MODE` / `ALPACA_LIVE_*` lines; the worker refuses to bind
while any of them is still set, and names the ones to remove. See
[`alpaca-credential-slots.md`](../references/alpaca-credential-slots.md).

## 3. Run the setup script

```bash
./setup-macos.sh
```

It sizes and starts the Podman VM, copies any missing `.env` files from their
templates, sets the control secret, builds the images, starts the stack, and
waits for the data plane and backend to report healthy. It is safe to re-run.
The first build takes 5–10 minutes.

Its closing message still tells you to run `ng serve` on the host. Skip that:
the frontend runs in its own container on port 4200, and a host `ng serve`
would collide with it.

## 4. Check it

`podman compose ps` should show five containers, all `(healthy)`. Every port
is bound to `127.0.0.1` only.

| Service | Container | URL |
|---|---|---|
| Frontend (Angular dev server) | `my-frontend` | http://localhost:4200 |
| Backend (GraphQL) | `my-backend` | http://localhost:5000/graphql |
| Data plane (FastAPI) | `polygon-data-service` | http://localhost:8000/health |
| PostgreSQL | `my-postgres` | `localhost:5432` |
| Redis | `my-redis` | `localhost:6379` |

Running the frontend on the host instead: the proxy config reads
`DATA_PLANE_CONTROL_SECRET` from the repo-root `.env` (an exported shell value
takes precedence). For a non-default service location set
`BACKEND_PROXY_TARGET` or `DATA_PLANE_PROXY_TARGET`; do not replace
`proxy.conf.js` with a target-only proxy config.

## 5. Day to day

```bash
./restart.sh               # fresh containers, rebuilding changed layers
./restart.sh --no-cache    # full rebuild from scratch (~5 min)
```

To rebuild one service after changing its code, recreate it rather than
restarting it:

```bash
podman compose down backend && podman compose up -d --build backend
podman compose down python-service && podman compose up -d --build python-service
```

The backend applies EF Core migrations on startup, so a new empty database is
created and upgraded automatically. Reset the database only when you mean to
discard local data:

```bash
podman compose down db
podman volume rm learn-ai_pgdata
podman compose up -d
```

Do not use a volume reset as a schema-change workflow. To adopt a populated
database that was created with `EnsureCreated()`, follow
[`ef-migrations-adoption.md`](ef-migrations-adoption.md) after taking a
restorable backup.

**Debugging:**

```bash
podman compose logs -f python-service                                # live logs
podman compose exec python-service bash                              # shell in
podman inspect --format='{{json .State.Health}}' polygon-data-service # health
```

**Backups:** PostgreSQL data lives in the `pgdata` named volume and survives
`podman compose down`.

```bash
podman exec my-postgres pg_dump -U postgres postgres > backup.sql
```

`podman compose down -v` deletes the non-external named volumes: `pgdata`
and the qualification Clerk's volume. It leaves the live Clerk volume
(`external: true`) and the bind-mounted data lake alone.

**Production images:** `compose.yaml` is the dev config (source mounted,
hot reload). The Dockerfiles build lean runtime images; to check they still
build:

```bash
podman build -t learn-ai-backend ./Backend
podman build -t learn-ai-python ./PythonDataService
```

## 6. Tests

```bash
podman exec my-frontend npm test                                               # Frontend (Vitest)
cd Backend.Tests && dotnet test --filter "Category!=PostgresIntegration"      # Backend (xUnit)
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.run_fast_tests  # Python
```

The Python suite runs from a host venv; create it once with
`./bootstrap-host-venv.sh`. Frontend and Python change-gating tests have a
hard two-minute budget, and CI applies the same limit to backend tests. The
complete Python suite, PostgreSQL-backed .NET integration tests, and browser
end-to-end coverage run daily in GitHub Actions. See
[`.claude/rules/testing.md`](../../.claude/rules/testing.md).

## 7. Stopping

```bash
podman compose down
podman machine stop    # also stop the VM
```
