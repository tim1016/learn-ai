# First-time setup — macOS

How to bring the stack up on a Mac. For Windows, use
[`windows-onboarding.md`](windows-onboarding.md).

Moved out of the README on 2026-09-28 and checked that day against
`compose.yaml`, `compose.fleet.dev.yaml`, `setup-macos.sh`, and `restart.sh`.
Everything runs as containers under Podman Compose.

## 1. Prerequisites

- macOS on Apple Silicon, with [Homebrew](https://brew.sh/).
- The repo checked out **under `$HOME`**. The Podman VM bind-mounts `$HOME`
  into the guest, so a checkout elsewhere starts with empty mounts.
- A [Polygon.io](https://polygon.io/) API key. Optional: a
  [FRED](https://fred.stlouisfed.org/) key for risk-free rate curves.

`setup-macos.sh` installs `podman`, `docker-compose`, and `node`. Backend
tests also need the .NET 10 SDK on the host, which the script does not
install.

## 2. First run

Order matters, because `setup-macos.sh` is what installs Podman and starts its
VM. The host directories, environment files, and first-run ceremonies are the
same on every OS, so they live in one place: sections 2–4 of
[`windows-onboarding.md`](windows-onboarding.md).

1. **Host directories and environment files** — Windows runbook §2 and §3.
   Rootless Podman does not create missing bind-mount paths, and the frontend
   crash-loops without `Frontend/src/environments/environment.development.ts`.
2. **Run the setup script:**

   ```bash
   ./setup-macos.sh
   ```

   It sizes and starts the Podman VM, copies any missing `.env` file from its
   template, sets the data-plane control secret, builds the images (5–10
   minutes the first time), and starts the stack. On a fresh machine this
   first run ends with "❌ … the stack is NOT usable": the data plane refuses
   to start until step 3 is done. That is expected.
3. **First-run ceremonies** — Windows runbook §4a (the Alpaca Clerk volume
   marker; without it the data plane exits 78) and §4b (the data-lake root
   identity; without it the data plane exits 3).
4. **Start the stack:** `podman compose up -d`, then check it (section 3).

The script's closing message still tells you to run `ng serve` on the host.
Skip that: the frontend runs in its own container on port 4200 (#2575).

**The data-plane control secret.** Compose refuses to start the data plane and
the frontend proxy while `DATA_PLANE_CONTROL_SECRET` in the root `.env` is
missing or blank; there is no checked-in development credential. The script
generates one on a fresh checkout and rotates the retired
`local-dev-control-secret` value on an upgrade, without overwriting a value
you set. By hand:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Broker settings are not environment settings.** Alpaca credentials go in
`PythonDataService/.env` as named credential slots; paper/live mode and the
risk limits are a broker profile saved in the browser (ADR 0060). See
[`alpaca-credential-slots.md`](../references/alpaca-credential-slots.md).

## 3. Check it

`podman compose ps` should show five containers, all `(healthy)`. Every port
is bound to `127.0.0.1` only.

| Service | Container | URL |
|---|---|---|
| Frontend (Angular dev server) | `my-frontend` | http://localhost:4200 |
| Backend (GraphQL) | `my-backend` | http://localhost:5000/graphql |
| Data plane (FastAPI) | `polygon-data-service` | http://localhost:8000/health |
| PostgreSQL | `my-postgres` | `localhost:5432` |
| Redis | `my-redis` | `localhost:6379` |

To run the frontend on the host instead, stop its container first
(`podman stop my-frontend`; it has `restart: always`, so the next `up` brings
it back). The proxy config reads `DATA_PLANE_CONTROL_SECRET` from the
repo-root `.env` (an exported shell value takes precedence). For a
non-default service location set `BACKEND_PROXY_TARGET` or
`DATA_PLANE_PROXY_TARGET`; do not replace `proxy.conf.js` with a target-only
proxy config.

## 4. Two postures: combined and fleet

What section 2 gives you is the **combined** posture: one data-plane process
does every job. `setup-macos.sh` and any plain `podman compose` command run
it.

`./restart.sh` runs the **fleet** posture. It also loads
`compose.fleet.dev.yaml`, which adds one clerk container per Alpaca account
(`alpaca-live-clerk`, `alpaca-paper-clerk`), so seven containers. It needs the
gitignored lane files `deploy/fleet/env/live.env` and `paper.env`; until they
exist it fails with `env file … not found`. Setting them up:
[`fleet-dev-two-lane-posture.md`](fleet-dev-two-lane-posture.md) and
[`add-an-alpaca-account.md`](add-an-alpaca-account.md).

**Once the fleet runs, every compose command must name the same files
`restart.sh` does.** A plain `podman compose up` recreates the data plane in
combined posture on the Live account's volume while `alpaca-live-clerk` still
runs on it: two processes writing one account's state. For the same reason,
don't re-run `setup-macos.sh` on a fleet machine; use `./restart.sh`.

The commands below take the file list as `COMPOSE_ARGS`:

```bash
# Fleet posture (the list restart.sh builds):
COMPOSE_ARGS=(--file compose.yaml --file compose.fleet.dev.yaml)
[[ -f compose.override.yaml ]] && COMPOSE_ARGS+=(--file compose.override.yaml)

# Combined posture:
COMPOSE_ARGS=(--file compose.yaml)
```

## 5. Day to day

```bash
./restart.sh               # fleet: fresh containers, rebuilding changed layers
./restart.sh --no-cache    # fleet: full rebuild from scratch (~5 min)
```

To rebuild one service after changing its code, recreate it rather than
restarting it (a restart keeps the old image and environment):

```bash
podman compose "${COMPOSE_ARGS[@]}" up -d --build --no-deps --force-recreate backend
```

The backend applies EF Core migrations on startup, so a new empty database is
created and upgraded automatically. Reset the database only when you mean to
discard local data:

```bash
podman compose "${COMPOSE_ARGS[@]}" down
podman volume rm learn-ai_pgdata
podman compose "${COMPOSE_ARGS[@]}" up -d
```

**Never run `podman compose down -v` here.** With the fleet file loaded it
deletes the Paper account's custody volume (`learn-ai-alpaca-paper-clerk-data`)
and the fleet registry (`learn-ai_alpaca-fleet-control`) along with the
database. To reset only Postgres, remove `learn-ai_pgdata` as above.

Do not use a database reset as a schema-change workflow. To adopt a populated
database that was created with `EnsureCreated()`, follow
[`ef-migrations-adoption.md`](ef-migrations-adoption.md) after taking a
restorable backup.

**Debugging:**

```bash
podman logs -f polygon-data-service                                  # live logs
podman exec -it polygon-data-service bash                            # shell in
podman inspect --format='{{json .State.Health}}' polygon-data-service # health
```

**Backups:** PostgreSQL data lives in the `learn-ai_pgdata` volume and survives
`podman compose down`.

```bash
podman exec my-postgres pg_dump -U postgres postgres > backup.sql
```

**Production images:** `compose.yaml` is the dev config (source mounted,
hot reload). The Dockerfiles build lean runtime images; to check they still
build:

```bash
podman build -t learn-ai-backend ./Backend
podman build --target runtime -t learn-ai-python ./PythonDataService
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
podman compose "${COMPOSE_ARGS[@]}" down
podman machine stop    # also stop the VM
```
