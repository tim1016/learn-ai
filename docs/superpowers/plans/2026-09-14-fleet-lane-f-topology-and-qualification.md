# Lane F: fleet topology durability and the one-time fault-isolation run (#2066, #2070, #2072 framing) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Commit a faithful transcription of the running topology as compose.fleet.dev.yaml with credentials env_file-only, gate its secret-free render in CI, teach restart.sh the extra -f, then migrate the host in a no-bot window with volume-identity verification; reclaim and run the qualification harness; frame the audit read surface.

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose, GitHub Actions.

**Spec:** the GitHub issues named in the title, plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md` — read it first; its Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular: never switch branches in `/Users/inkant/learn-ai` (the clerks bind-mount it); every `./restart.sh` drops both clerks; import-time changes need a no-bot window; run the thermo review once before the first push; fleet tests run unsandboxed.

## Corrections applied by the integrating session

- Podman was unreachable from the agent's sandbox; the running-host facts in this lane come from issue #2055's recorded evidence. Re-verify volume names with the M1/M7 commands before and after the migration.
- The override's inline environment: literals OUTRANK env_file. The four secret keys are externalised by DELETING the literals, not by adding files.

---

*The report below was produced by an Opus planning agent on 2026-09-14 against origin/master `e2fdb23f` plus the branches of PR #2081 and PR #2078. Every file:line claim was verified by that agent at that time; re-verify after those PRs merge.*

---

# Fleet topology durability, the one-time fault-isolation run, and the audit read-surface decision

Planned against `origin/master` (`e2fdb23f`) **+** `origin/fix/fleet-production-safety` (#2081) **+** `origin/docs/broker-clerk-fleet-authority` (#2078). Read-only: no files changed, no branch/HEAD touched.

**Podman is not reachable from this session.** `podman machine list` fails with `open /Users/inkant/.config/containers/podman/machine/applehv/podman-machine-default.lock: operation not permitted` — a sandbox denial on a path outside this task's write set. I did not retry unsandboxed and ran no other podman command. Every claim about live volumes/containers below is therefore sourced from committed files or from the recorded evidence in issue #2055, and is labelled as such.

---

## 1. Verified facts

### 1.1 Claim-by-claim verdicts

| Claim (from #2066 / #2070 / trust register) | Verdict | Evidence |
|---|---|---|
| No `FLEET_*` anywhere in `compose.yaml` | **CONFIRMED** | `grep -c 'FLEET_' /Users/inkant/learn-ai/compose.yaml` → `0` |
| `fleet_boot.py:108-116` makes `combined` unfenced, announces at INFO | **CONFIRMED, exactly** | `/Users/inkant/learn-ai/PythonDataService/app/broker/alpaca/clerk/fleet_boot.py:108` `if settings.ROLE == "combined":` … `:110 return None` (silent) … `:112-114 logger.info(...)` … `:116 return None` |
| `FLEET_ROLE` defaults to `combined` | **CONFIRMED** | `PythonDataService/app/config.py:27` — `ROLE: Literal[...] = "combined"` (issue said `:28`; off by one, harmless) |
| `run_host_qualification` at `scripts/run_broker_fleet_compose_qualification.py:714` | **CONFIRMED on line, DRIFTED on path** | Real path `/Users/inkant/learn-ai/PythonDataService/scripts/run_broker_fleet_compose_qualification.py:714` |
| Zero callers in tests and CI | **CONFIRMED** | Repo-wide grep: only `:714` (def) and `:973` (its own `main`) |
| Nothing in CI renders any compose file | **CONFIRMED** | `grep -rn compose .github/workflows/` → no matches |
| Its own test "asserts a 5-string tuple equals itself" | **DRIFTED (half true)** | `PythonDataService/tests/scripts/test_run_broker_fleet_compose_qualification.py:124-130` is exactly that tautology. But `:131-135` and `test_partial_fault_matrix_cannot_be_labelled_passed` (`:138-153`) do exercise `_fault_result` / `_assert_all_faults_passed` for real. **The real gap is that nothing asserts `run_host_qualification` itself populates all five scenarios.** |
| Running clerks: `ReadonlyRootfs=false`, `PidsLimit=0`, no tmpfs | **CONFIRMED by absence** | `grep -nE 'read_only\|pids_limit\|tmpfs' compose.override.yaml` → no hits. Only `deploy.resources.limits.cpus/memory` at `:54-55`. |
| All three on `app-network`, not `fleet-private` | **CONFIRMED** | `compose.override.yaml:49-50` `networks: - app-network` in the clerk anchor; no `fleet-private` anywhere in the file |
| Host source bind-mounted into the Live-credential container | **CONFIRMED** | `compose.override.yaml:25` (anchor) and `:122`, `:141` — `- ./PythonDataService/app:/app/app:z` |
| "The clerks currently depend on the bind-mount for **code reload**" | **WRONG** | `compose.override.yaml:17` — the clerk command is `uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10`. **No `--reload`.** The clerks never hot-reload. The coordinator's `command: !override` (`:102-113`) defaults `${UVICORN_RELOAD:-false}` → reload **off** too (base `compose.yaml:249` defaults it `true`; the override flips it). This materially cheapens the containment step — see §2. |
| `compose.override.yaml` is the sole durable copy of every fleet credential | **CONFIRMED, and worse than stated** | `env_file: - path: ./deploy/fleet/env/live.env / paper.env` already exist (`:119`, `:138`, both `required: true`), **but** `environment:` at `:127-130` and `:146-149` re-declares `FLEET_WORKER_KEY`, `FLEET_AGENT_SERVICE_TOKEN`, `FLEET_COORDINATOR_SERVICE_TOKEN` as 37-char literals. Compose `environment:` **wins over** `env_file:`, so the env files are decorative for exactly the four identity/token keys. The coordinator's `FLEET_AGENT_SERVICE_TOKENS_JSON` / `FLEET_COORDINATOR_SERVICE_TOKENS_JSON` (`:81-82`) are literals with no env-file at all. |
| A fresh clone reverts to `combined` | **CONFIRMED** | `compose.yaml` has no `FLEET_*`; `config.py:27` default is `combined`; `fleet_boot.py:110` returns `None` **without logging** when no fleet config exists |
| `compose.fleet.yaml` never deployed; zero `learn-ai-fleet_*` volumes | **CONFIRMED as of #2055's recorded host evidence**, not re-verifiable here | #2055 comment: "no `learn-ai-fleet_*` volumes exist; no container carries that project label" |
| Three unreclaimed `fleetqualification…` volumes from 2026-09-13 | **CONFIRMED as of #2055's recorded host evidence**, not re-verifiable here | `fleetqualification…_fleet-alpaca-paper-data` (has marker), `…_fleet-alpaca-live-data` (no marker), `…_fleet-coordinator-control`, created 18:55-18:56 |
| `restart.sh` five-name allowlist destroys a stuck clerk | **CONFIRMED on master (`restart.sh:28`, `:64-65`, `:88-89`), FIXED on #2081** | #2081 replaces it with `label!=com.docker.compose.project` (`:45-46`) / `label=${COMPOSE_LABEL}` (`:84`, `:111`, `:130`) |

### 1.2 `.env` / `env_file` handling today

- **Root `.env`** (gitignored, `.gitignore:14`) is Compose's default env file: every `${VAR}` in `compose.yaml` resolves from it. Five are hard-required (`:?`): `POLYGON_API_KEY`, `DATA_PLANE_CONTROL_SECRET`, `ALPACA_QUALIFICATION_API_KEY_ID`, `ALPACA_QUALIFICATION_API_SECRET_KEY`, `ALPACA_CLERK_PRODUCTION_ACCOUNT_ID`. `compose config` **fails hard** if any is unset — this is what a CI render gate must supply placeholders for.
- **`compose.yaml:83-85`**: `python-service` has `env_file: - path: ./PythonDataService/.env / required: false`. That file exists on this host.
- **`compose.yaml` `environment:` is list-form** (`- KEY=${VAR:?...}`), 25 keys on `python-service` (`:140-234`). The override uses **map-form**, which merges key-wise — fine.
- **`deploy/fleet/env/`** already exists: `coordinator.env.example`, `live.env.example`, `paper.env.example` are **tracked**; `live.env` and `paper.env` exist untracked; **`coordinator.env` does not exist** (the coordinator's token maps live inline in the override).
- **`restart.sh` passes no `-f` and no `--project-name`.** Master and #2081 both rely on Compose's implicit `compose.yaml` + `compose.override.yaml`. #2081 additionally derives the project as `${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}` = `learn-ai` (`:29`) and calls `podman compose config --services` (`:98`) with that same implicit file set.

### 1.3 `.gitignore` coverage (`/Users/inkant/learn-ai/.gitignore`)

```
compose.override.yaml            # :11
compose.override.yml             # :12
.env                             # :15
deploy/fleet/env/coordinator.env # :16
deploy/fleet/env/paper.env       # :17
deploy/fleet/env/live.env        # :18
!deploy/fleet/                   # :19  (no-op — parent was never excluded)
!deploy/fleet/env/               # :20  (dir-only negation; does not re-include the three files)
!deploy/fleet/env/*.env.example  # :21
```
Correct today, but **brittle**: a fourth lane file (`ibkr-live.env`) would be committed silently. Tighten to `deploy/fleet/env/*.env` + keep the `.example` negation.

### 1.4 CI today

`ubuntu-latest` on all jobs in `.github/workflows/ci.yml`, `daily-tests.yml`, `frontend-e2e.yml`. GitHub's `ubuntu-latest` image ships **Docker Engine + the Compose v2 plugin** preinstalled; it does **not** ship a working `podman compose` provider. This host has **no `docker` binary** (`which docker` → not found) but has `podman` and `docker-compose` **5.1.4** (so `!override`/`!reset` tags are supported both places). **Consequence: the CI render gate proves `docker compose` semantics while the host runs `podman compose` through `docker-compose`.** That divergence must be closed by running the same script on the host during migration and comparing, not by assuming.

The repo already has the exact gate pattern to copy — **regenerate + `git diff --exit-code` + `git status --porcelain`**: `ci.yml:157-161` (GraphQL schema), `:218-225` (operator manual), `:245-259` (vocabulary snapshot, including the untracked-file guard). Use it verbatim.

### 1.5 Override (deployed) vs `compose.fleet.yaml` (reviewed) — keys only

| Axis | `compose.override.yaml` (running) | `compose.fleet.yaml` (reviewed) |
|---|---|---|
| Project name | none → `learn-ai` (dir basename) | `name: learn-ai-fleet` (`:4`) |
| Coordinator service | `python-service` (container `polygon-data-service`), modified in place | **new service** `fleet-coordinator` (`:52`) |
| Clerk services | `alpaca-live-clerk`, `alpaca-paper-clerk` (`:115`, `:134`) | same names (`:122`, `:135`) |
| Legacy combined | n/a — `python-service` *is* the coordinator | `python-service: profiles: ["legacy-combined"]` (`:152-153`) |
| Profiles | none (always up) | everything `profiles: ["fleet"]` |
| Live custody volume | `alpaca-clerk-data` → **`learn-ai-alpaca-clerk-data`, `external: true`** (`compose.yaml:464-466`) | `fleet-alpaca-live-data`, **new, non-external** (`:148`, `:179`) |
| Paper custody volume | `alpaca-paper-clerk-data` → `name: learn-ai-alpaca-paper-clerk-data` (`:157-158`) | `fleet-alpaca-paper-data`, new (`:133`, `:178`) |
| Coordinator control volume | `alpaca-fleet-control` → `learn-ai_alpaca-fleet-control` (`:101`, `:156`) | `fleet-coordinator-control` + `fleet-coordinator-artifacts` (`:89-90`, `:176-177`) |
| Networks | `app-network` only, all three (`:49-50`) | clerks `fleet-private` only (`:48-49`); coordinator both (`:104-109`) |
| Clerk ports | none | none; coordinator `127.0.0.1:${FLEET_COORDINATOR_PORT:-8100}:8000` (`:100`) |
| `read_only` | **absent** | `true` (`:12`, `:58`) |
| `tmpfs` | **absent** | `/tmp:size=128m` + `/app/cache:size=256m` (`:13-17`, `:59-60`) |
| `pids_limit` | **absent** | `256` agents / `192` coordinator (`:18`, `:61`) |
| cpu / mem | `deploy.resources.limits.cpus "1.0"`, `memory 768m` (`:54-55`) | top-level `cpus: "1.00"`, `mem_limit: 768m` (`:19-20`, `:62-63`) |
| Host source bind | `./PythonDataService/app:/app/app:z` on **both clerks** (`:122`, `:141`) | **none** |
| `env_file` | live/paper, `required: true` (`:118-120`, `:137-139`); **no coordinator env_file** | all three via `${FLEET_*_ENV_FILE:-…}`, `required: false` (`:64-66`, `:124-126`, `:137-139`) |
| Secrets location | **`environment:` literals win over `env_file`** (`:81-82`, `:127-130`, `:146-149`) | `env_file` only |
| Clerk env KEYS (shared) | `HOST PORT POLYGON_API_KEY FLEET_ROLE FLEET_DEPLOYMENT_NAMESPACE FLEET_COORDINATOR_URL FLEET_MAX_INFLIGHT_REQUESTS FLEET_MAX_INFLIGHT_STREAMS FLEET_REQUEST_QUEUE_LIMIT FLEET_REQUEST_QUEUE_TIMEOUT_MS ALPACA_CLERK_DIR IBKR_LIVE_RUNS_ROOT IBKR_LIVE_BARS_ROOT BROKER_CAPTURE_DIR IBKR_READONLY IBKR_BROKER_ENABLED IBKR_HOST IBKR_PORT` (`:27-45`) | same minus `HOST`/`PORT`, plus `TRUSTED_HOSTS` (`:28-44`) |
| Clerk env KEYS (per lane) | `FLEET_CLERK_ID FLEET_WORKER_KEY FLEET_AGENT_ENDPOINT_REF FLEET_AGENT_SERVICE_TOKEN FLEET_COORDINATOR_SERVICE_TOKEN IBKR_CLIENT_ID TRUSTED_HOSTS` (`:126-132`, `:145-152`) | `FLEET_ROLE FLEET_COORDINATOR_URL FLEET_AGENT_ENDPOINT_REF` only — identities come from `env_file` |
| Coordinator env KEYS | `FLEET_ROLE FLEET_CONTROL_DIR FLEET_DEPLOYMENT_NAMESPACE FLEET_AGENT_SERVICE_TOKENS_JSON FLEET_COORDINATOR_SERVICE_TOKENS_JSON ALPACA_CLERK_DIR(="") TRUSTED_HOSTS` (`:77-86`) | `FLEET_ROLE FLEET_CONTROL_DIR LEAN_DATA_ROOT LEAN_DATA_CACHE LEAN_DATA_WRITE_ROOT LEAN_LAUNCHER_URL BACKEND_URL POLYGON_API_KEY POLYGON_RATE_LIMIT_PER_MIN FRED_API_KEY FLEET_DEPLOYMENT_NAMESPACE FLEET_MAX_* TRUSTED_HOSTS` (`:68-87`) |
| Backend/frontend retarget | not present (backend still points at `python-service`) | `PolygonService__BaseUrl` / `DATA_PLANE_PROXY_TARGET` → `fleet-coordinator` (`:158`, `:170`) |

**The single most consequential row is the volume row.** `compose.fleet.yaml` would provision brand-new empty custody volumes in a brand-new project; the Live lane would boot, find no `.learn-ai-clerk-volume.json`, and refuse (`fleet_boot.py:117-121`) — fail-closed, but a total outage, and the registry in `learn-ai_alpaca-fleet-control` would not be mounted either.

---

## 2. Decisions for the owner

**D1 — Commit a new `compose.fleet.dev.yaml` that transcribes what runs today; do *not* deploy `compose.fleet.yaml`.** Because `compose.fleet.yaml` renames the project *and* every custody volume, and a volume rename here orphans the Live custody store (`learn-ai-alpaca-clerk-data`, `external: true`) — the one failure mode that is not a fail-closed refusal.

**D2 — Take the topology commit + credential externalisation in one restart window, and the containment hardening in a *second* window a trading day later.** Because a wrong-token failure and a read-only-rootfs failure look identical from outside the container, and separating them keeps every rollback single-variable.

**D3 — Take the clerk-side containment (read-only rootfs, tmpfs, pids limit, drop the `./PythonDataService/app` bind-mount) sooner than #2066 implies; defer only the `app-network` → `fleet-private` move.** Because the clerks run plain `uvicorn` with no `--reload` (`compose.override.yaml:17`), so the bind-mount buys nothing at runtime, whereas the network move changes coordinator→clerk name resolution and the approved-endpoint base URLs.

**D4 — Run `run_host_qualification` once on the host, and additionally wire it into `daily-tests.yml`.** Because it runs in its own randomly named `fleetqualification<hex>` project against `compose.fleet.yaml` + `compose.fleet.qualification.yaml` only (`run_broker_fleet_compose_qualification.py:38-41`, `:730`) — **it never touches the `learn-ai` project and cannot kill the Live clerks** — so the only real cost is host contention, and a nightly CI run converts "never run" into "run every night" for a fraction of the effort.

**D5 — Give the audit trail a read surface, starting with one coordinator-only GET.** Because the trust register names Observability the highest-risk axis and "a refusal you cannot read" the realistic bad night; `store.list_routing_receipts` (`app/broker/fleet/store.py:832`) already exists, so the first slice is a router and a schema.

---

## 3. Task breakdown

### Issue #2066 — Track A: commit the topology

#### Task 1 — Externalise the four literal credential keys so `env_file` actually governs

**Files:** modify `/Users/inkant/learn-ai/deploy/fleet/env/coordinator.env.example`; create `/Users/inkant/learn-ai/PythonDataService/tests/contracts/test_fleet_env_example_completeness.py`; modify `/Users/inkant/learn-ai/.gitignore`.

**Failing test:**
```python
# PythonDataService/tests/contracts/test_fleet_env_example_completeness.py
from __future__ import annotations
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[3]
SECRET_KEYS = frozenset({
    "FLEET_WORKER_KEY", "FLEET_AGENT_SERVICE_TOKEN", "FLEET_COORDINATOR_SERVICE_TOKEN",
    "FLEET_AGENT_SERVICE_TOKENS_JSON", "FLEET_COORDINATOR_SERVICE_TOKENS_JSON",
    "DATA_PLANE_CONTROL_SECRET", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY",
    "POSTGRES_URL", "REDIS_URL",
})

def _example_keys(name: str) -> set[str]:
    text = (ROOT / "deploy/fleet/env" / name).read_text(encoding="utf-8")
    return {line.split("=", 1)[0].strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line}

def test_no_committed_compose_file_carries_a_fleet_secret_literal() -> None:
    """Every secret-bearing key is env_file-only; none is inline in a tracked compose file."""
    for name in ("compose.yaml", "compose.fleet.yaml", "compose.fleet.dev.yaml"):
        document = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        for service, spec in (document.get("services") or {}).items():
            environment = spec.get("environment") or {}
            if isinstance(environment, list):
                environment = {entry.split("=", 1)[0] for entry in environment}
            leaked = SECRET_KEYS & set(environment)
            assert not leaked, f"{name}:{service} declares secret keys inline: {sorted(leaked)}"

def test_coordinator_example_declares_every_key_the_running_coordinator_needs() -> None:
    assert {"FLEET_AGENT_SERVICE_TOKENS_JSON", "FLEET_COORDINATOR_SERVICE_TOKENS_JSON",
            "DATA_PLANE_CONTROL_SECRET"} <= _example_keys("coordinator.env.example")
```

**Expected failure:** `FileNotFoundError: .../compose.fleet.dev.yaml` (A2 creates it). Run A1's second test alone first — it **passes** today, which is the point: the examples are already correct and only the override is wrong.

**Implementation — `.gitignore` lines 16-21 become:**
```
deploy/fleet/env/*.env
!deploy/fleet/env/*.env.example
```

**Expected pass:** `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts/test_fleet_env_example_completeness.py -q` → 2 passed (after A2).

**Commit:** `chore(fleet): widen the lane env-file ignore and pin the secret-free compose contract`

---

#### Task 2 — Commit `compose.fleet.dev.yaml`

**Files:** create `/Users/inkant/learn-ai/compose.fleet.dev.yaml`.

**Failing check (run first, expect failure):**
```bash
cd /Users/inkant/learn-ai && podman compose -f compose.yaml -f compose.fleet.dev.yaml config --services
# expected: "no such file or directory: compose.fleet.dev.yaml"
```

**Implementation** — a faithful transcription of `compose.override.yaml`'s fleet content with **no project rename, no volume rename, no containment change**, and every secret removed:

```yaml
# The deployed dev-stack fleet topology: ADR 0062's role split applied to the
# `learn-ai` Compose project. Committed so the running posture is reproducible
# from the repository alone; credentials live only in gitignored
# deploy/fleet/env/*.env, referenced by env_file below.
#
# Load explicitly — Compose auto-loads compose.override.yaml, not this file:
#   podman compose -f compose.yaml -f compose.fleet.dev.yaml <command>
# restart.sh does this for you (see A4).
#
# No `name:` on purpose. This overlay stays in the `learn-ai` project so it
# keeps the EXISTING external Live custody volume (learn-ai-alpaca-clerk-data)
# and the existing coordinator control volume (learn-ai_alpaca-fleet-control).
# Renaming the project renames every default-named volume and orphans Live
# custody. compose.fleet.yaml is the separate, harder production topology.

x-alpaca-clerk-agent: &alpaca-clerk-agent
  build:
    context: ./PythonDataService
  restart: unless-stopped
  # Deliberately no --reload: a clerk ships code by image rebuild, never by
  # watching a host bind. The bind below is convenience for the next restart.
  command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]
    interval: 10s
    timeout: 5s
    retries: 6
    start_period: 30s
  volumes:
    - ./PythonDataService/app:/app/app:z
  environment: &alpaca-clerk-agent-env
    HOST: 0.0.0.0
    PORT: "8000"
    # Lane market data is the retained read-only IBKR feed; a clerk is never a
    # data-plane market-data owner. Settings construction requires the key.
    POLYGON_API_KEY: ""
    FLEET_ROLE: clerk_agent
    FLEET_DEPLOYMENT_NAMESPACE: ${FLEET_DEPLOYMENT_NAMESPACE:-compose:learn-ai}
    FLEET_COORDINATOR_URL: ${FLEET_COORDINATOR_URL:-http://polygon-data-service:8000}
    FLEET_MAX_INFLIGHT_REQUESTS: ${FLEET_MAX_INFLIGHT_REQUESTS:-16}
    FLEET_MAX_INFLIGHT_STREAMS: ${FLEET_MAX_INFLIGHT_STREAMS:-4}
    FLEET_REQUEST_QUEUE_LIMIT: ${FLEET_REQUEST_QUEUE_LIMIT:-64}
    FLEET_REQUEST_QUEUE_TIMEOUT_MS: ${FLEET_REQUEST_QUEUE_TIMEOUT_MS:-5000}
    # Fleet writable-root fence: lane state lives inside the clerk volume.
    ALPACA_CLERK_DIR: /app/artifacts/alpaca_clerk
    IBKR_LIVE_RUNS_ROOT: /app/artifacts/alpaca_clerk/live_runs
    IBKR_LIVE_BARS_ROOT: /app/artifacts/alpaca_clerk/live_bars
    BROKER_CAPTURE_DIR: /app/artifacts/alpaca_clerk/broker_captures
    IBKR_READONLY: "true"
    IBKR_BROKER_ENABLED: "true"
    IBKR_HOST: auto
    IBKR_PORT: "4002"
    TRUSTED_HOSTS: localhost,127.0.0.1,alpaca-paper-clerk,alpaca-live-clerk,polygon-data-service
  depends_on:
    python-service:
      condition: service_healthy
  networks:
    - app-network
  deploy:
    resources:
      limits:
        cpus: "1.0"
        memory: 768m

services:
  python-service:
    environment:
      # Coordinator-only posture: this process keeps the whole data-plane core
      # and serves fleet routing; the two lanes below own custody. FR-041.
      FLEET_ROLE: fleet_coordinator
      FLEET_CONTROL_DIR: /app/artifacts/fleet
      FLEET_DEPLOYMENT_NAMESPACE: ${FLEET_DEPLOYMENT_NAMESPACE:-compose:learn-ai}
      # No clerk volume is mounted on this role; neutralize the base path so a
      # future clerk-surface mount cannot silently reappear.
      ALPACA_CLERK_DIR: ""
      TRUSTED_HOSTS: localhost,127.0.0.1,polygon-data-service,backend,my-backend
    # Per-clerk transport-token maps are secrets: env-file only, never inline.
    env_file:
      - path: ${FLEET_COORDINATOR_ENV_FILE:-./deploy/fleet/env/coordinator.env}
        required: false
    volumes: !override
      - ./PythonDataService/app:/app/app:z
      - ./.git:/app/.git:ro,z
      - ./PythonDataService/pytest.ini:/app/pytest.ini:ro,z
      - ./PythonDataService/ruff.toml:/app/ruff.toml:ro,z
      - ./PythonDataService/tests/fixtures/golden/manifest.json:/app/tests/fixtures/golden/manifest.json:ro,z
      - ../Lean/Data:/lean-data:ro,z
      - ./PythonDataService/lean-cache:/lean-cache:z
      - ./PythonDataService/cache:/app/cache:z
      - ./PythonDataService/artifacts:/app/artifacts:z
      - ./PythonDataService/scripts:/app/scripts:ro,z
      - ${LEAN_DATA_VOLUME_HOST_PATH:-./data-lake-volume}:/lean-data-writer:rw,z
      - alpaca-fleet-control:/app/artifacts/fleet
    command: !override
      # The base command's alpaca-clerk volume guard no longer applies (no
      # clerk volume on this role); keep the reload opt-in semantics.
      - /bin/sh
      - -c
      - |
        case "$$(printf '%s' "$${UVICORN_RELOAD:-false}" | tr '[:upper:]' '[:lower:]')" in
          false|0|no|off)
            exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10
            ;;
        esac
        exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app --reload-exclude 'artifacts/*' --reload-exclude 'artifacts/**' --timeout-graceful-shutdown 10

  alpaca-live-clerk:
    <<: *alpaca-clerk-agent
    container_name: alpaca-live-clerk
    env_file:
      - path: ${FLEET_LIVE_ENV_FILE:-./deploy/fleet/env/live.env}
        required: true
    volumes:
      - ./PythonDataService/app:/app/app:z
      - alpaca-clerk-data:/app/artifacts/alpaca_clerk
    environment:
      <<: *alpaca-clerk-agent-env
      FLEET_AGENT_ENDPOINT_REF: alpaca-live-agent
      # Distinct read-only market-data client ID per lane (Delivery D).
      IBKR_CLIENT_ID: ${FLEET_LIVE_IBKR_CLIENT_ID:-1202}

  alpaca-paper-clerk:
    <<: *alpaca-clerk-agent
    container_name: alpaca-paper-clerk
    env_file:
      - path: ${FLEET_PAPER_ENV_FILE:-./deploy/fleet/env/paper.env}
        required: true
    volumes:
      - ./PythonDataService/app:/app/app:z
      - alpaca-paper-clerk-data:/app/artifacts/alpaca_clerk
    environment:
      <<: *alpaca-clerk-agent-env
      FLEET_AGENT_ENDPOINT_REF: alpaca-paper-agent
      IBKR_CLIENT_ID: ${FLEET_PAPER_IBKR_CLIENT_ID:-1201}

volumes:
  # Default-named → learn-ai_alpaca-fleet-control. Holds fleet/registry.db.
  alpaca-fleet-control:
  # Explicit name, no prefix. Never change this string: it is the Paper lane's
  # custody store and its marker attestation_id.
  alpaca-paper-clerk-data:
    name: learn-ai-alpaca-paper-clerk-data
  # alpaca-clerk-data is declared external in compose.yaml (:464-466) as
  # learn-ai-alpaca-clerk-data — the Live custody store. Not redeclared here.
```

`FLEET_CLERK_ID`, `FLEET_WORKER_KEY`, `FLEET_AGENT_SERVICE_TOKEN`, `FLEET_COORDINATOR_SERVICE_TOKEN` are **absent from `environment:` on purpose** — they come from `env_file` only. That is the whole fix.

**Expected pass:**
```bash
podman compose -f compose.yaml -f compose.fleet.dev.yaml config --services | sort
# db, redis, python-service, alpaca-live-clerk, alpaca-paper-clerk, backend, frontend
```

**Commit:** `feat(fleet): commit the deployed dev-stack topology as compose.fleet.dev.yaml`

---

#### Task 3 — Secret-free render snapshot + CI gate

**Files:** create `/Users/inkant/learn-ai/scripts/render_fleet_topology.py`, `/Users/inkant/learn-ai/deploy/fleet/topology.snapshot.json`, `/Users/inkant/learn-ai/deploy/fleet/ci-render.placeholders`, `/Users/inkant/learn-ai/PythonDataService/tests/contracts/test_fleet_topology_snapshot.py`; modify `/Users/inkant/learn-ai/.github/workflows/ci.yml`.

**Failing test:**
```python
# PythonDataService/tests/contracts/test_fleet_topology_snapshot.py
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "deploy/fleet/topology.snapshot.json"

def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))

def test_snapshot_pins_the_deployed_role_split() -> None:
    detail = _snapshot()["service_detail"]
    assert detail["python-service"]["environment_keys"].count("FLEET_ROLE") == 1
    for lane in ("alpaca-live-clerk", "alpaca-paper-clerk"):
        assert "FLEET_AGENT_ENDPOINT_REF" in detail[lane]["environment_keys"]

def test_snapshot_pins_the_custody_volume_names() -> None:
    """A renamed custody volume orphans a lane. This is the tripwire."""
    volumes = _snapshot()["volumes"]
    assert volumes["alpaca-clerk-data"] == {"name": "learn-ai-alpaca-clerk-data", "external": True}
    assert volumes["alpaca-paper-clerk-data"]["name"] == "learn-ai-alpaca-paper-clerk-data"

def test_snapshot_carries_no_environment_values() -> None:
    """The snapshot is committed; it must be secret-free by construction."""
    text = SNAPSHOT.read_text(encoding="utf-8")
    assert "environment\":" not in text and '"environment"' not in text
    for service in _snapshot()["service_detail"].values():
        assert isinstance(service["environment_keys"], list)
        assert all(isinstance(key, str) and "=" not in key for key in service["environment_keys"])
```
**Expected failure:** `FileNotFoundError: .../deploy/fleet/topology.snapshot.json`.

**Implementation — `scripts/render_fleet_topology.py`:**
```python
"""Render the committed fleet topology to a secret-free, committable snapshot.

`compose config` inlines every env_file value into its output, so the rendered
document itself is never safe to commit. This projects it down to what a
topology review actually needs — services, images, env KEY NAMES, mount
sources and targets, networks, ports and containment settings — and drops
every value on the way through.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("compose.yaml", "compose.fleet.dev.yaml")
SNAPSHOT_PATH = REPOSITORY_ROOT / "deploy" / "fleet" / "topology.snapshot.json"
PLACEHOLDERS = REPOSITORY_ROOT / "deploy" / "fleet" / "ci-render.placeholders"
CONTAINMENT_FIELDS = ("read_only", "pids_limit", "tmpfs", "cpus", "mem_limit", "privileged", "cap_add")


def render(engine: list[str]) -> dict[str, object]:
    command = [*engine, "--project-name", "learn-ai",
               "--project-directory", str(REPOSITORY_ROOT),
               "--env-file", str(PLACEHOLDERS)]
    for name in COMPOSE_FILES:
        command += ["--file", str(REPOSITORY_ROOT / name)]
    command += ["config", "--format", "json"]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
    document = json.loads(completed.stdout)
    detail: dict[str, object] = {}
    for name, spec in sorted((document.get("services") or {}).items()):
        environment = spec.get("environment") or {}
        detail[name] = {
            "image": spec.get("image"),
            "container_name": spec.get("container_name"),
            "environment_keys": sorted(environment),
            "env_file": sorted(
                entry.get("path") if isinstance(entry, dict) else entry
                for entry in (spec.get("env_file") or [])
            ),
            "volumes": sorted(
                f"{mount.get('source')}->{mount.get('target')}:{mount.get('type')}"
                for mount in (spec.get("volumes") or [])
            ),
            "networks": sorted(spec.get("networks") or {}),
            "ports": sorted(
                f"{port.get('host_ip')}:{port.get('published')}->{port.get('target')}"
                for port in (spec.get("ports") or [])
            ),
            "containment": {field: spec[field] for field in CONTAINMENT_FIELDS if field in spec},
        }
    return {
        "compose_files": list(COMPOSE_FILES),
        "services": sorted(detail),
        "service_detail": detail,
        "volumes": {
            name: {"name": spec.get("name"), "external": bool(spec.get("external"))}
            for name, spec in sorted((document.get("volumes") or {}).items())
        },
        "networks": sorted(document.get("networks") or {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman compose",
                        help="Compose invocation, e.g. 'docker compose' or 'podman compose'.")
    parser.add_argument("--check", action="store_true",
                        help="Compare against the committed snapshot instead of writing it.")
    args = parser.parse_args(argv)
    snapshot = json.dumps(render(shlex.split(args.engine)), indent=2, sort_keys=True) + "\n"
    if args.check:
        if SNAPSHOT_PATH.read_text(encoding="utf-8") != snapshot:
            sys.stderr.write("Rendered fleet topology does not match the committed snapshot.\n")
            return 1
        return 0
    SNAPSHOT_PATH.write_text(snapshot, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**`deploy/fleet/ci-render.placeholders`** (committed; not named `.env` so it cannot be mistaken for one):
```
# Placeholder interpolation values so `compose config` can render without
# secrets. NOT credentials. The render gate discards every value it reads.
POLYGON_API_KEY=render-placeholder
DATA_PLANE_CONTROL_SECRET=render-placeholder
POSTGRES_PASSWORD=render-placeholder
REDIS_PASSWORD=render-placeholder
FRED_API_KEY=render-placeholder
ALPACA_QUALIFICATION_API_KEY_ID=render-placeholder
ALPACA_QUALIFICATION_API_SECRET_KEY=render-placeholder
ALPACA_CLERK_PRODUCTION_ACCOUNT_ID=render-placeholder
ALPACA_MODE=paper
GIT_SHA=render-placeholder
```

**`.github/workflows/ci.yml`** — new job, placed after `fleet-tooling-tests`:
```yaml
  fleet-topology-render:
    name: Fleet Topology Render
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Record the Compose implementation under test
        run: docker compose version
      - name: Render the topology and require the committed snapshot to match
        run: |
          python scripts/render_fleet_topology.py --engine "docker compose"
          git diff --exit-code -- deploy/fleet/topology.snapshot.json
          # `git diff` ignores untracked files, so a snapshot deleted and
          # regenerated as a never-committed file would pass unnoticed.
          status=$(git status --porcelain -- deploy/fleet/topology.snapshot.json)
          if [ -n "$status" ]; then
            echo "Rendered topology snapshot is untracked or otherwise dirty:"
            echo "$status"
            exit 1
          fi
      - name: Require the fleet service set to be exactly the reviewed one
        run: |
          docker compose --project-name learn-ai \
            --env-file deploy/fleet/ci-render.placeholders \
            -f compose.yaml -f compose.fleet.dev.yaml config --services | sort > /tmp/services.txt
          printf '%s\n' alpaca-live-clerk alpaca-paper-clerk backend db frontend python-service redis \
            | sort | diff -u - /tmp/services.txt
```

**Expected pass:** job green; the two contract tests pass in the normal Python shards (no container engine needed — they read JSON).

**Commit:** `feat(fleet): render the committed topology to a secret-free snapshot and gate it in CI`

---

#### Task 4 — Teach `restart.sh` the extra `-f`

**Files:** modify `/Users/inkant/learn-ai/restart.sh` (**on top of #2081**); modify `/Users/inkant/learn-ai/PythonDataService/tests/contracts/test_restart_script_reap_behaviour.py`.

**Failing test** — extend #2081's fake-podman harness (`_FAKE_PODMAN` already records calls and already answers `compose … config --services`):
```python
def test_restart_passes_the_committed_fleet_overlay_to_every_compose_call(
    fake_podman_environment,
) -> None:
    """A committed overlay that restart.sh does not pass is a topology that
    does not run. Compose auto-loads compose.override.yaml only."""
    result = _run_restart(fake_podman_environment)
    compose_calls = _calls(fake_podman_environment)["compose"]
    assert compose_calls, "restart.sh made no compose call"
    for call in compose_calls:
        assert call.count("--file") >= 2, call
        assert "compose.fleet.dev.yaml" in " ".join(call), call
    assert result.returncode == 0
```
(The fake's `compose` branch must be extended to append `args` to `calls["compose"]` before its `sys.exit(0)`.)

**Expected failure:** `AssertionError: ['compose', 'down'] ... assert 0 >= 2`.

**Implementation** — insert after `export COMPOSE_BAKE=false` (#2081 `restart.sh:12`), and replace every bare `podman compose` with `podman compose "${COMPOSE_ARGS[@]}"`:
```bash
# Compose auto-loads only compose.yaml and compose.override.yaml. The committed
# fleet topology is a third file, so it must be named explicitly or the stack
# silently reverts to the unfenced `combined` posture (fleet_boot.py:108-116).
# Order matters: later files win. An explicit COMPOSE_FILE in the environment
# takes over entirely.
COMPOSE_ARGS=()
if [[ -z "${COMPOSE_FILE:-}" ]]; then
  COMPOSE_ARGS+=(--file compose.yaml)
  [[ -f compose.fleet.dev.yaml ]] && COMPOSE_ARGS+=(--file compose.fleet.dev.yaml)
  [[ -f compose.override.yaml ]] && COMPOSE_ARGS+=(--file compose.override.yaml)
fi
echo "==> Compose files: ${COMPOSE_ARGS[*]:-\$COMPOSE_FILE=$COMPOSE_FILE}"
```

**Expected pass:** `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts/test_restart_script_reap_behaviour.py tests/contracts/test_restart_script_compose_ownership.py -q` → all pass.

**Commit:** `fix(fleet): make restart.sh load the committed fleet overlay explicitly`

---

#### Task 5 — Host migration runbook

**Files:** modify `/Users/inkant/learn-ai/docs/runbooks/fleet-dev-two-lane-posture.md` (new "§ Migrating to the committed topology" before "Operations quick reference").

**Failing check:** `grep -q 'compose.fleet.dev.yaml' docs/runbooks/fleet-dev-two-lane-posture.md` → exit 1.

**Content — the operator sequence, in order:**

```bash
set -euo pipefail
cd /Users/inkant/learn-ai
STAMP=$(date -u +%Y%m%dT%H%M%SZ); SAFE=~/.fleet-migration/$STAMP
mkdir -p "$SAFE"; chmod 700 ~/.fleet-migration "$SAFE"

# M1 — capture the running truth BEFORE anything changes. Outside the repo, 0600.
cp compose.override.yaml "$SAFE/compose.override.yaml.bak"; chmod 600 "$SAFE"/*
for c in polygon-data-service alpaca-live-clerk alpaca-paper-clerk; do
  podman inspect "$c" --format '{{json .Config.Env}}'  > "$SAFE/$c.env.json"
  podman inspect "$c" --format '{{json .Mounts}}'      > "$SAFE/$c.mounts.json"
done
chmod 600 "$SAFE"/*.json
podman volume ls --format '{{.Name}}' | sort > "$SAFE/volumes.before.txt"
podman ps --format '{{.Names}}\t{{.Status}}' > "$SAFE/ps.before.txt"

# M2 — registry insurance (the ceremony already documented in this runbook).
podman run --rm -e POLYGON_API_KEY=standin \
  -v learn-ai_alpaca-fleet-control:/app/artifacts/fleet:z -v "$SAFE":/backup:z \
  learn-ai-python-service:latest python -m scripts.manage_broker_fleet backup-registry \
    --control-dir /app/artifacts/fleet --backup-dir /backup/registry-$STAMP

# M3 — move the four identity/token keys per lane out of the override into the
# env files that already exist. Read them from $SAFE/<clerk>.env.json, append to
# deploy/fleet/env/<lane>.env, chmod 600. Create coordinator.env from the example
# and move FLEET_AGENT_SERVICE_TOKENS_JSON / FLEET_COORDINATOR_SERVICE_TOKENS_JSON
# and DATA_PLANE_CONTROL_SECRET into it. Never echo a value; never use shell history.
chmod 600 deploy/fleet/env/coordinator.env deploy/fleet/env/paper.env deploy/fleet/env/live.env

# M4 — retire the fleet content of the override. Keep ONLY the dev ergonomics
# (backend port 5050 remap, frontend 6G memory bump, the angular.json bind).
# Everything else now lives in compose.fleet.dev.yaml.

# M5 — render and diff BEFORE starting anything. This is the gate.
podman compose --project-name learn-ai \
  -f compose.yaml -f compose.fleet.dev.yaml -f compose.override.yaml \
  config --services | sort > "$SAFE/services.after.txt"
diff -u <(printf '%s\n' alpaca-live-clerk alpaca-paper-clerk backend db frontend python-service redis | sort) \
        "$SAFE/services.after.txt"
python scripts/render_fleet_topology.py --engine "podman compose" --check   # must exit 0
```
**Do not proceed if M5's `--check` fails**: podman and docker rendered the same file differently, which is itself the finding.

```bash
# M6 — the restart. THIS DROPS BOTH CLERKS. Outside market hours, no bot running.
./restart.sh

# M7 — verify SAME volumes, SAME markers. A wrong volume name here orphans Live custody.
podman inspect alpaca-live-clerk  --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}'
#   must contain: learn-ai-alpaca-clerk-data /app/artifacts/alpaca_clerk
podman inspect alpaca-paper-clerk --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}'
#   must contain: learn-ai-alpaca-paper-clerk-data /app/artifacts/alpaca_clerk
podman inspect polygon-data-service --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}'
#   must contain: learn-ai_alpaca-fleet-control /app/artifacts/fleet

podman exec alpaca-live-clerk  cat /app/artifacts/alpaca_clerk/.learn-ai-clerk-volume.json
#   clerk_id must be clrk_57f90423a8504d2d3dd4af77, volume_id vol_aea84c3d8bcfa734eed761ba,
#   attestation_id alpaca-clerk-data   (the marker is documented non-secret)
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
**Rollback:** `cp "$SAFE/compose.override.yaml.bak" compose.override.yaml && git stash list` is *not* the path — instead `mv compose.fleet.dev.yaml /tmp/` and `./restart.sh`. Compose returns to the auto-loaded override; volumes were never renamed, so nothing is lost.

**Commit:** `docs(fleet): add the committed-topology migration runbook with volume-identity verification`

---

### Issue #2070 — Track B: the one-time run + the test fix

#### Task 6 — Reclaim the three leftover volumes

**Files:** modify `/Users/inkant/learn-ai/docs/runbooks/fleet-d-two-clerk-rollout.md` (new "§ 3a. Reclaiming a failed run's isolation").

**Failing check:** `podman volume ls --format '{{.Name}}' | grep -c '^fleetqualification'` → expect `3`.

**Implementation (runbook text + commands):**
```bash
# The harness's own rule (run_broker_fleet_compose_qualification.py:682-709):
# unreclaimed isolation invalidates a pass. Reclaim before a new run.
podman volume ls --format '{{.Name}}' | grep '^fleetqualification' | sort
# Confirm each has NO container attached before removing anything:
for v in $(podman volume ls --format '{{.Name}}' | grep '^fleetqualification'); do
  printf '%s -> ' "$v"; podman ps -a --filter volume="$v" --format '{{.Names}}' | tr '\n' ' '; echo
done
# Every line must show no container. Only then:
podman volume ls --format '{{.Name}}' | grep '^fleetqualification' | xargs -r podman volume rm
podman network ls --format '{{.Name}}' | grep '^fleetqualification' | xargs -r podman network rm || true
podman volume ls --format '{{.Name}}' | grep -c '^fleetqualification'   # must print 0
```
Never widen this to `podman volume prune` — `learn-ai_alpaca-fleet-control` has no container attached while the stack is down and would be pruned. **That is the Live registry.**

**Expected pass:** the final `grep -c` prints `0`.

**Commit:** `docs(fleet): reclaim a failed qualification's isolation before re-running`

---

#### Task 7 — The one-time host run

**Files:** modify `/Users/inkant/learn-ai/docs/runbooks/fleet-d-two-clerk-rollout.md` §3; modify `/Users/inkant/learn-ai/docs/broker-clerk-fleet-authority.md` §4 and §6 (**on #2078's branch** — this file does not exist on master).

**Prerequisites:** B1 done; `podman machine` running; ≥8 GB free RAM and ≥15 GB free disk; `podman images | grep learn-ai-fleet-agent` may be absent (the harness builds it — distinct tag from `learn-ai-python-service:latest`, so the running stack's image is never clobbered).

**What it does to the running stack:** *nothing directly.* `COMPOSE_FILES` is `compose.fleet.yaml` + `compose.fleet.qualification.yaml` only (`:38-41`), the project is `fleetqualification<uuid4[:12]>` (`:730`), the coordinator port is a freshly-bound loopback port (`:717`, `:464-468`), and both networks are project-scoped. **It does not kill the Live clerks.** It does add ~8 containers (postgres, redis, coordinator, 3 fakes, 2 clerks, plus one-shot enrollers) alongside the running 5, so it is a **host-contention** event, not a lane event.

**Still run it outside market hours with no bot running**, for two reasons the issue did not state: the CPU/IO of `compose build fleet-coordinator` (120 s budget, `:736`) competes with the Live clerk's heartbeat and market-data session; and the first command the harness runs is `config` **with the bootstrap env**, before `_host_ceremony` installs the temp env files — at that instant `${FLEET_LIVE_ENV_FILE:-./deploy/fleet/env/live.env}` resolves to the **real** Live credential file and `compose config` **inlines its values into stdout**. Neutralise it:

```bash
cd /Users/inkant/learn-ai/PythonDataService
SAFE=~/.fleet-qualification/$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$SAFE"; chmod 700 "$SAFE"
: > "$SAFE/empty.env"; chmod 600 "$SAFE/empty.env"

# Fence the real credential files out of the bootstrap render (see risk R3).
export FLEET_LIVE_ENV_FILE="$SAFE/empty.env"
export FLEET_PAPER_ENV_FILE="$SAFE/empty.env"
export FLEET_COORDINATOR_ENV_FILE="$SAFE/empty.env"

DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.run_broker_fleet_compose_qualification \
  --timeout-s 120 --evidence-path "$SAFE/fleet-compose-qualification.json"
echo "exit=$?"
chmod 600 "$SAFE/fleet-compose-qualification.json"
```
(The harness re-sets all three variables from `_host_ceremony` at `:548-566`, so this only affects the pre-ceremony stages.)

**Expected duration:** 10–20 min (build ≤120 s, support startup ≤90 s, lanes ≤60 s, five fault stages each bounded by `--timeout-s`, teardown ≤60 s). Add `--keep` only on failure, and re-run B1 afterwards.

**Evidence produced:** `fleet-compose-qualification.json` with `result`, `volume_sources` (three distinct engine sources), `resources` (NanoCpus/Memory/PidsLimit per role), `paper_fault_matrix` (five scenarios, each `state: passed`), `live_mutation_status` + `live_mutation_denial`, `coordinator_custody_check`, `bounded_exclusions`. **It contains no credential**, but it does contain clerk IDs and volume paths — keep it 0600 outside the repo.

**Where to record it:** `docs/broker-clerk-fleet-authority.md` — replace §4's "The fault matrix has never been run" block with the run date, commit SHA, engine + compose version, evidence-file location, and the five scenario verdicts; move row **#6** out of §6's ranked table and note in §6's Axis table that **State** risk has narrowed. Cross-link from `docs/runbooks/fleet-d-two-clerk-rollout.md` §3.

**Blocker or accepted residual?** **Accepted residual, with a deadline.** The trust register's own argument holds — Paper is dark, so every cross-lane risk is latent — but the register also says *"the day the Paper lane activates, this block flips from latent to live."* Make the run a **precondition of Paper activation**, not of the next Live bot launch.

**Commit:** `docs(fleet): record the one-time isolated Compose qualification run and its evidence`

---

#### Task 8 — Fix the self-asserting test

**Files:** modify `/Users/inkant/learn-ai/PythonDataService/tests/scripts/test_run_broker_fleet_compose_qualification.py`.

**Failing test** (replaces the tautology at `:124-130`; keep `:131-135`):
```python
def test_every_declared_fault_scenario_is_actually_populated_by_the_host_run() -> None:
    """The tuple is a manifest; this asserts the ceremony honours it.

    Comparing FAULT_SCENARIOS to a literal copy of itself cannot catch the one
    drift that matters — a scenario declared in the vocabulary and never
    assigned in run_host_qualification, which _assert_all_faults_passed would
    then fail at runtime on the host, hours into a maintenance window.
    """
    import ast

    source = Path(qualification.__file__).read_text(encoding="utf-8")
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "run_host_qualification"
    )
    populated = {
        target.slice.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == "faults"
        and isinstance(target.slice, ast.Constant)
        and isinstance(target.slice.value, str)
    }
    assert populated == set(qualification.FAULT_SCENARIOS)
```
**Expected failure:** rename one entry of `FAULT_SCENARIOS` (or delete one `faults[...] = _fault_result(...)` assignment) and it fails with the exact set difference. Against today's code it **passes** — which is correct: today's code is consistent; the old test just could not have told you.

**Expected pass:** `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/scripts/test_run_broker_fleet_compose_qualification.py -q`.

**Commit:** `test(fleet): assert the host qualification populates every declared fault scenario`

---

#### Task 9 — CI-runnable subset (recommended: yes, in `daily-tests.yml`, not the PR gate)

**Files:** modify `/Users/inkant/learn-ai/.github/workflows/daily-tests.yml`.

```yaml
  fleet-compose-qualification:
    name: Fleet Compose Qualification
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: read
    defaults:
      run:
        working-directory: PythonDataService
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: PythonDataService/requirements*.txt
      - run: pip install -r requirements-heavy.txt -r requirements-light.txt -r requirements-dev.txt
      - run: docker compose version
      - name: Run the isolated actual-role Compose qualification
        env:
          DATA_PLANE_CONTROL_SECRET: ""
        run: |
          mkdir -p TestResults
          # The runner has no deploy/fleet/env/*.env, so the pre-ceremony render
          # is already secret-free; pin it anyway so the job cannot regress.
          : > TestResults/empty.env
          FLEET_LIVE_ENV_FILE=TestResults/empty.env \
          FLEET_PAPER_ENV_FILE=TestResults/empty.env \
          FLEET_COORDINATOR_ENV_FILE=TestResults/empty.env \
          python -m scripts.run_broker_fleet_compose_qualification \
            --timeout-s 120 --evidence-path TestResults/fleet-compose-qualification.json
      - name: Upload qualification evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: fleet-compose-qualification
          path: PythonDataService/TestResults/fleet-compose-qualification.json
          if-no-files-found: error
```
This mirrors `ci.yml:408-438` (`alpaca-sqlite-qualification-smoke`) exactly. **It does not replace the one-time host run**: the runner has Docker, not podman, and no host contention — the podman-specific `HostConfig.PidsLimit` / mount-source semantics that `_assert_limits` (`:359-367`) and `_mount_at` check are only proven on the host.

**Commit:** `ci(fleet): run the isolated Compose qualification nightly with uploaded evidence`

---

## 4. Grouping and windows

Market hours: **08:30–15:00 America/Chicago on trading days.** The Live lane may carry a running bot.

**The critical, non-obvious rule:** the clerks bind-mount `./PythonDataService/app` (`compose.override.yaml:122`, `:141`) but run uvicorn **without** `--reload` (`:17`), and the coordinator's reload is off by default (`:108`). So editing files under `PythonDataService/app/` does **not** hot-restart a lane today — **but only because `UVICORN_RELOAD` is unset.** Confirm with `podman exec polygon-data-service ps -o args= -p 1` before assuming, and note that **switching branches or applying a patch under `PythonDataService/app/` in this checkout would reload the coordinator if that variable is ever set.** None of the tasks below touch `PythonDataService/app/`.

| Group | Tasks | Window | Why |
|---|---|---|---|
| **G1 — repo-only, no stack effect** | A1, A2, A3, A4, B3, B4 | **Any time**, including market hours | Touches only `compose*.yaml`, `.github/`, `scripts/`, `deploy/`, `docs/`, `PythonDataService/tests/`. None of these paths is mounted into a clerk (`compose.override.yaml:25` mounts only `app/`), and none triggers a reload. Merging them changes no running container until someone runs `./restart.sh`. |
| **G2 — volume reclamation** | B1 | Any time; prefer outside market hours | `podman volume rm` on `fleetqualification*` touches nothing the stack uses — but the adjacent typo (`prune`) does. Do it when you are not rushed. |
| **G3 — the qualification run** | B2 | **Outside 08:30–15:00 CT, no bot running.** Weekend or ≥16:00 CT preferred | Host contention only, not a lane change — but ~8 extra containers and a full image build alongside a live market-data session. Budget 30 min including B1 re-check. |
| **G4 — the migration restart** | A5 execution | **Outside 08:30–15:00 CT, no bot running, after a clean close.** ~45 min | `./restart.sh` runs `podman compose down` (`restart.sh:21`) — both clerks drop. Requires #2081 merged first, or a stuck clerk gets `rm -f`'d mid-migration. |
| **G5 — containment hardening** | separate PR, not planned here | **A different window, ≥1 full trading day after G4** | Per D2/D3: single-variable rollback. Clerk-side (read_only, tmpfs `/tmp` + `/app/cache`, `pids_limit: 256`, drop the `app/` bind) first; `fleet-private` network move last, since it changes coordinator→clerk resolution and the approved-endpoint base URLs in the registry. |

**Merge order:** #2081 → #2078 → A1-A4 (A3 edits `ci.yml`, which #2078 also edits by −24 lines; rebase A3 on #2078 to avoid a conflict) → B3, B4 → then the G4 window → then B2.

---

## 5. Risks the issue text missed

**R1 — The env files are already wired; `environment:` silently outranks them.** `compose.override.yaml:118-120`/`:137-139` set `env_file` with `required: true`, and `:127-130`/`:146-149` then re-declare the same four keys as literals. Anyone who "fixes" this by populating `deploy/fleet/env/*.env` and restarting will see **no change**, conclude the env-file path doesn't work, and leave the secrets in place. The fix is a deletion, not an addition.

**R2 — `compose.fleet.yaml` is a project rename, not just a topology.** `name: learn-ai-fleet` (`:4`) plus #2081's `COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"` (`restart.sh:29`) = **`restart.sh` would filter on `com.docker.compose.project=learn-ai` while every container carries `learn-ai-fleet`.** `TOTAL` would be 0, `MISSING` would list every service, and the script would loop 240 s and exit 1 on a perfectly healthy fleet. If the reviewed overlay is ever deployed, `COMPOSE_PROJECT_NAME` must be exported in the same change.

**R3 — `compose config` inlines `env_file` values, and the qualification harness renders before it fences.** `run_host_qualification` calls `bootstrap.run(project, ["--profile","fleet-qualification","config"])` at `:734` using the **bootstrap** ComposeCommand; `_host_ceremony` only installs `FLEET_LIVE_ENV_FILE` at `:566`. Between those two points, `${FLEET_LIVE_ENV_FILE:-./deploy/fleet/env/live.env}` (`compose.fleet.yaml:138`) resolves to the real Live credential file and its values land in captured stdout — and into a `CalledProcessError` string on failure. Mitigated by the `export FLEET_*_ENV_FILE` step in B2; worth fixing in the harness itself.

**R4 — `podman volume prune` is one keystroke from destroying the fleet registry.** While the stack is down, `learn-ai_alpaca-fleet-control` (holding `fleet/registry.db`, ~110 KB + a 1 MB WAL) has no attached container and is a prune target. Every cleanup instruction must be an explicit `grep '^fleetqualification' | xargs podman volume rm`, never a prune.

**R5 — CI proves `docker compose`; the host runs `podman compose`.** `ubuntu-latest` has no usable podman-compose provider. A render gate that is green in CI can still differ on the host in `!override` merge order, `deploy.resources` vs top-level `cpus`/`mem_limit` translation, and `:z` SELinux relabel suffixes. `scripts/render_fleet_topology.py --engine "podman compose" --check` in the migration runbook (M5) is what closes it; without that step the gate is a half-truth.

**R6 — a lost `compose.override.yaml` is currently unrecoverable, and the migration is the moment of maximum exposure.** During A5/M3-M4 the file is being edited while it is still the only durable copy of five secrets. M1's `cp … "$SAFE/compose.override.yaml.bak"` (0600, outside the repo, before anything else) is not optional.

**R7 — `alpaca-clerk-data` is `external: true` (`compose.yaml:464-466`).** `podman compose down --volumes` will **not** remove it — good — but `podman compose up` will **fail fast** if it does not exist, rather than creating it. Any host that loses that volume cannot be brought up by compose at all; that is a fail-closed property worth stating in the runbook rather than discovering at 16:30.

**R8 — the `deploy/fleet/env` ignore list is three literal filenames.** Adding a fourth lane (an IBKR clerk, a second Alpaca account) creates a credential file that `git add -A` will happily stage. Fixed by A1's `deploy/fleet/env/*.env`.

**R9 — `ALPACA_CLERK_DIR: ""` on the coordinator is load-bearing and undefended.** `compose.override.yaml:85` neutralises the base clerk path so no clerk surface can reappear on the coordinator. It is a *comment* today. A3's snapshot makes it a diffable fact; consider also a contract test asserting the coordinator's rendered `ALPACA_CLERK_DIR` is empty.

---

## 6. Issue #2072 — decision framing only

The durable audit trail (routing receipts, assignment history, session history) is append-only, trigger-protected, and reachable only by opening `learn-ai_alpaca-fleet-control:/app/artifacts/fleet/registry.db` by hand. `aggregate_lane_reads` (`PythonDataService/app/broker/fleet/service.py:1592`) has one caller and it is a test (`tests/broker/fleet/test_directory_and_aggregation.py:190`). `X-Fleet-Correlation-Id` / `X-Fleet-Routing-State` are written at `app/routers/broker_clerks.py:296,298` and read by nothing in `Frontend/src/`, `Backend/`, or `app/`.

**Option 1 — Leave it CLI-only.** Ship a `manage_broker_fleet audit` subcommand that dumps receipts as JSON. *Cheapest; changes no wire contract; keeps the "ceremonies are host-only" posture intact. But it does not help the actual failure mode the trust register names — an operator staring at a blank error in a browser at 09:15 CT.*

**Option 2 — One read-only HTTP route on the coordinator, secret-gated.** A single `GET`, reusing `require_data_plane_control_secret_always` (the same dependency the directory routes already use, `app/routers/broker_clerks.py:93`) and the query that already exists, `store.list_routing_receipts(clerk_id=..., limit=...)` (`app/broker/fleet/store.py:832`, already `ORDER BY created_at_ms DESC`). *Small, additive, no new storage, no ceremony change. Adds one route to the exported OpenAPI contract and one more surface behind the browser control secret.*

**Option 3 — A full audit surface: receipts + assignment history + session history + ceremony evidence, with a UI.** *Answers the whole gap, including "was this lane retired, by whom, when" — but it needs the `ready`-means-what and `lifecycle_state`-typing decisions from the register's Half B settled first, and it is weeks, not days.*

**Recommendation: Option 2 now, Option 3 scoped after the Paper lane activates.** One route converts the highest-ranked risk axis (Observability) from "open the SQLite file by hand" to "curl it", and it does not depend on any undecided semantics.

**On ceremony evidence:** yes, make it addressable even while the ceremony stays CLI-only — but as a *record written by the CLI*, not as a new mutation surface. The registry already carries `clerk_session_history` and `account_assignment_history`; exposing them read-only under the same route family is the same shape of work and keeps enrolment/retirement host-only.

**Smallest first slice** (respecting `.claude/rules/temporal-rigor.md` — all times `int64 ms UTC`, no ISO strings anywhere on the wire):

```
GET /api/broker-clerks/audit/routing-receipts
  ?since_ms=<int64 ms UTC>        required, inclusive lower bound on created_at_ms
  &clerk_id=<opaque clerk id>     optional
  &limit=<1..500, default 100>
  header: X-Data-Plane-Control-Secret   (require_data_plane_control_secret_always)

200 {"observed_at_ms": <int64>, "receipts": [
      {"correlation_id": …, "clerk_id": …, "broker": …, "capability": …,
       "routing_state": …, "created_at_ms": <int64>, "dispatched_at_ms": <int64|null>,
       "outcome": …}]}
```
Coordinator-only (refuse with the existing 503 when `request.app.state.fleet_service` is absent, as `_fleet_service` already does at `app/routers/broker_clerks.py:52-62`). Needs one store change: a `since_ms: int` parameter on `list_routing_receipts`. No idempotency key, no command envelope, no ceremony — it is a read.

---

### Critical Files for Implementation

- `/Users/inkant/learn-ai/compose.override.yaml` — the untracked source of truth being transcribed; lines `:17`, `:25`, `:49-50`, `:77-86`, `:102-113`, `:115-158`
- `/Users/inkant/learn-ai/compose.yaml` — `:83-85` env_file, `:240-254` command guard, `:462-468` volumes (`alpaca-clerk-data` is `external: true`)
- `/Users/inkant/learn-ai/compose.fleet.yaml` — the reviewed overlay the new file diverges from deliberately; `:4`, `:12-20`, `:64-66`, `:88-109`, `:175-189`
- `/Users/inkant/learn-ai/restart.sh` (plus `git show origin/fix/fleet-production-safety:restart.sh`) — `:29-30`, `:45-46`, `:98`, `:109-123`
- `/Users/inkant/learn-ai/PythonDataService/scripts/run_broker_fleet_compose_qualification.py` — `:38-41`, `:471-478`, `:548-566`, `:682-709`, `:714-908`
- `/Users/inkant/learn-ai/.github/workflows/ci.yml` — `:42-56` (fleet job placement), `:157-161` / `:245-259` (the snapshot-gate pattern to copy)
- `/Users/inkant/learn-ai/docs/runbooks/fleet-dev-two-lane-posture.md` — the operator record the migration section extends
