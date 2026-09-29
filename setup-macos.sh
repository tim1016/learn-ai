#!/usr/bin/env bash
# First-run bootstrap for macOS (Apple Silicon, Tahoe 26+).
#
# Provisions the Podman VM with generous resources, installs the host
# toolchain, wires up .env files, creates the bind-mount host directories,
# builds the container stack, performs the one-time Clerk volume and
# data-lake identity ceremonies, and brings the stack up in the combined
# posture. The frontend is one of those containers (my-frontend,
# http://localhost:4200); nothing runs on the host. Idempotent -- safe to
# re-run on a machine that has not moved to the fleet posture.
#
# It REFUSES to run once the fleet lane env files exist
# (deploy/fleet/env/live.env, paper.env): it stops the Podman VM and would
# bring the stack back from compose.yaml alone, without the clerks. On that
# machine ./restart.sh is the way to rebuild and restart.
#
# Usage:
#   ./setup-macos.sh
#
# Resource overrides (env vars, optional — defaults are auto-computed):
#   PODMAN_CPUS=8 PODMAN_MEMORY_MB=16384 PODMAN_DISK_GB=120 ./setup-macos.sh

set -euo pipefail

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '2,21p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "       Try --help." >&2
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# 0. Sanity: this script is macOS-only.
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: setup-macos.sh is for macOS. On Linux use ./restart.sh." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f "$ROOT_DIR/compose.yaml" ]]; then
  echo "ERROR: compose.yaml not found in $ROOT_DIR." >&2
  echo "       Run this script from the repo root (clone the repo first)." >&2
  exit 1
fi

# The Podman VM bind-mounts \$HOME into the guest. Source code is mounted in
# for hot-reload, so the repo MUST live under your home directory or the
# containers will start with empty mounts.
case "$ROOT_DIR" in
  "$HOME"/*) : ;;
  *)
    echo "ERROR: repo is at $ROOT_DIR, which is outside \$HOME ($HOME)." >&2
    echo "       The Podman VM only mounts your home directory — move the" >&2
    echo "       repo under \$HOME (e.g. ~/Documents/learn-ai) and re-run." >&2
    exit 1
    ;;
esac

echo "==> Repo root: $ROOT_DIR"

# Last assignment of KEY in an env file, without surrounding quotes; empty if
# the file or the key is absent.
env_file_value() {
  local file="$1" key="$2" value
  [[ -f "$file" ]] || return 0
  value="$(sed -n "s/^${key}=//p" "$file" | tail -n1)"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s\n' "$value"
}

# The value Compose interpolates for KEY: the shell environment wins, then the
# repo-root .env, then DEFAULT — Compose's own precedence.
compose_setting() {
  local key="$1" default="$2" value
  value="${!key:-}"
  if [[ -z "$value" ]]; then
    value="$(env_file_value "$ROOT_DIR/.env" "$key")"
  fi
  printf '%s\n' "${value:-$default}"
}

# Compose resolves a relative host path against the project directory.
project_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)  printf '%s\n' "$ROOT_DIR/${1#./}" ;;
  esac
}

# ---------------------------------------------------------------------------
# 0b. A fleet machine belongs to restart.sh.
#     restart.sh layers compose.fleet.dev.yaml onto compose.yaml; that overlay
#     needs the gitignored lane env files (FLEET_LIVE_ENV_FILE and
#     FLEET_PAPER_ENV_FILE, defaulting under deploy/fleet/env/). Where they
#     exist the machine runs the fleet posture. This script stops the Podman VM
#     and then brings the stack up from compose.yaml alone: the data plane
#     would come back in the combined posture on the Live account's volume,
#     with no clerks. Refuse before anything is touched.
# ---------------------------------------------------------------------------
if [[ -f "$ROOT_DIR/compose.fleet.dev.yaml" ]]; then
  for lane_env_file in \
    "$(project_path "$(compose_setting FLEET_LIVE_ENV_FILE deploy/fleet/env/live.env)")" \
    "$(project_path "$(compose_setting FLEET_PAPER_ENV_FILE deploy/fleet/env/paper.env)")"
  do
    if [[ -f "$lane_env_file" ]]; then
      echo "ERROR: this machine runs the fleet posture — found $lane_env_file." >&2
      echo "       setup-macos.sh would stop the Podman VM and bring the stack back" >&2
      echo "       without the broker clerks (compose.yaml alone), leaving the data" >&2
      echo "       plane running in the combined posture on the Live account's volume." >&2
      echo "       Nothing has been changed. To rebuild and restart the stack, run:" >&2
      echo "         ./restart.sh              (add --no-cache for a from-scratch rebuild)" >&2
      exit 1
    fi
  done
fi

# ---------------------------------------------------------------------------
# 1. Homebrew + host toolchain.
# ---------------------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew not found. Install it first:" >&2
  echo '       /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
  exit 1
fi

# podman:         the container engine
# docker-compose: the compose provider that `podman compose` delegates to on
#                 macOS (without it, `podman compose` has no backend)
# node:           runs Frontend/scripts/data-plane-control-secret.cjs below (the
#                 Angular dev server itself runs in the my-frontend container)
for pkg in podman docker-compose node; do
  if brew list --formula "$pkg" >/dev/null 2>&1; then
    echo "==> $pkg already installed"
  else
    echo "==> Installing $pkg via Homebrew..."
    brew install "$pkg"
  fi
done

# ---------------------------------------------------------------------------
# 2. Compute generous VM resources from this machine's hardware.
#    Cores drive build speed (parallel .NET/Python compilation), so we hand
#    the VM (cpus - 2), leaving 2 for the host + editor. RAM does NOT speed
#    things up past the working set: this five-container stack peaks ~15-20 GB
#    even mid-build, so we target half of physical RAM but CAP at 32 GB —
#    plenty of headroom without reserving memory the stack can't use. On a
#    128 GB machine that's 32 GB to the VM, 96 GB left for the host.
#    Override either with PODMAN_CPUS / PODMAN_MEMORY_MB if you really want.
# ---------------------------------------------------------------------------
HOST_CPUS="$(sysctl -n hw.ncpu)"
HOST_MEM_BYTES="$(sysctl -n hw.memsize)"
HOST_MEM_MB=$(( HOST_MEM_BYTES / 1024 / 1024 ))

DEFAULT_CPUS=$(( HOST_CPUS > 6 ? HOST_CPUS - 2 : 4 ))

MEM_CAP_MB=32768   # 32 GB — ~2x the stack's real peak; raise via PODMAN_MEMORY_MB
DEFAULT_MEM_MB=$(( HOST_MEM_MB / 2 ))
if (( DEFAULT_MEM_MB < 8192 )); then DEFAULT_MEM_MB=8192; fi
if (( DEFAULT_MEM_MB > MEM_CAP_MB )); then DEFAULT_MEM_MB=$MEM_CAP_MB; fi

VM_CPUS="${PODMAN_CPUS:-$DEFAULT_CPUS}"
VM_MEM_MB="${PODMAN_MEMORY_MB:-$DEFAULT_MEM_MB}"
VM_DISK_GB="${PODMAN_DISK_GB:-100}"

echo "==> Host: ${HOST_CPUS} CPUs, ${HOST_MEM_MB} MB RAM"
echo "==> Provisioning Podman VM with: ${VM_CPUS} CPUs, ${VM_MEM_MB} MB RAM, ${VM_DISK_GB} GB disk"

# ---------------------------------------------------------------------------
# 3. Provision / reconfigure the Podman VM.
#    init if no machine exists; otherwise `set` the resources (disk can only
#    grow — a smaller value is silently ignored by podman, not an error).
# ---------------------------------------------------------------------------
MACHINE_NAME="$(podman machine list --format '{{.Name}}' 2>/dev/null | head -n1 | sed 's/\*$//')"

if [[ -z "$MACHINE_NAME" ]]; then
  echo "==> No Podman machine found — initializing default..."
  podman machine init \
    --cpus "$VM_CPUS" \
    --memory "$VM_MEM_MB" \
    --disk-size "$VM_DISK_GB"
  MACHINE_NAME="$(podman machine list --format '{{.Name}}' | head -n1 | sed 's/\*$//')"
else
  echo "==> Reusing existing Podman machine: $MACHINE_NAME"
  # `set` requires the machine stopped. Stop, reconfigure, then start below.
  if podman machine inspect "$MACHINE_NAME" --format '{{.State}}' 2>/dev/null | grep -qi running; then
    echo "==> Stopping $MACHINE_NAME to apply resource settings..."
    podman machine stop "$MACHINE_NAME"
  fi
  # Apply CPU/memory strictly — a failure here is a real provisioning error
  # (bad value, podman error) and must abort, not be masked.
  podman machine set "$MACHINE_NAME" \
    --cpus "$VM_CPUS" \
    --memory "$VM_MEM_MB"
  # Disk resize is best-effort: an existing machine's disk can only grow, so a
  # smaller/equal --disk-size is rejected. Tolerate ONLY that, scoped to the
  # disk call, instead of swallowing every `set` failure.
  if ! podman machine set "$MACHINE_NAME" --disk-size "$VM_DISK_GB"; then
    echo "    (disk resize skipped — existing Podman machines can only grow disk)"
  fi
fi

if ! podman machine inspect "$MACHINE_NAME" --format '{{.State}}' 2>/dev/null | grep -qi running; then
  echo "==> Starting Podman machine: $MACHINE_NAME"
  podman machine start "$MACHINE_NAME"
fi

# Confirm the compose provider is reachable before we lean on it.
if ! podman compose version >/dev/null 2>&1; then
  echo "ERROR: \`podman compose\` could not find a provider. Ensure" >&2
  echo "       docker-compose is on PATH (brew install docker-compose)." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Environment files (gitignored — never carried by `git clone`).
#    Copy from templates only if absent; never clobber real secrets.
# ---------------------------------------------------------------------------
copy_env_if_missing() {
  local example="$1" target="$2"
  if [[ -f "$target" ]]; then
    echo "==> $target already exists — leaving it untouched"
  elif [[ -f "$example" ]]; then
    cp "$example" "$target"
    echo "==> Created $target from $(basename "$example")"
  else
    # A missing template is a real repo/checkout problem — fail loudly now
    # rather than booting the stack with no env and debugging it later.
    echo "ERROR: missing template $example (cannot create $target)" >&2
    exit 1
  fi
}

copy_env_if_missing "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
copy_env_if_missing "$ROOT_DIR/PythonDataService/.env.example" "$ROOT_DIR/PythonDataService/.env"
# The my-frontend container crash-loops without this file (angular.json's
# development file replacement). primeUiLicense in it is a placeholder.
copy_env_if_missing \
  "$ROOT_DIR/Frontend/src/environments/environment.development.ts.example" \
  "$ROOT_DIR/Frontend/src/environments/environment.development.ts"

# Fresh checkouts receive a random local credential before Compose evaluates
# its required interpolation. Upgrades carrying the retired public default are
# rotated in place; an existing operator-owned value is never overwritten.
node "$ROOT_DIR/Frontend/scripts/data-plane-control-secret.cjs" "$ROOT_DIR/.env"

# Loud warning if the Polygon key is still the placeholder — the stack will
# boot, but no market data will flow until it's set.
if grep -q "POLYGON_API_KEY=your_polygon_api_key_here" "$ROOT_DIR/.env" 2>/dev/null; then
  echo ""
  echo "  ⚠️  POLYGON_API_KEY is still the placeholder in .env."
  echo "      Edit .env and set a real key before fetching data."
  echo "      Get one at https://polygon.io/dashboard/api-keys"
  echo ""
fi

# ---------------------------------------------------------------------------
# 5. Host directories, image build, first-run ceremonies, stack up.
#    First build is slow (~5-10 min): .NET SDK image + Python heavy deps.
# ---------------------------------------------------------------------------
# Rootless Podman does not create a missing bind-mount source, so every host
# path compose.yaml mounts must exist before `up` (windows-onboarding §2).
# LEAN_DATA_VOLUME_HOST_PATH relocates the data-lake root; Compose reads it from
# the shell or .env, so this does too.
LAKE_HOST_DIR="$(project_path "$(compose_setting LEAN_DATA_VOLUME_HOST_PATH data-lake-volume)")"
for host_dir in \
  "$(dirname "$ROOT_DIR")/Lean/Data" \
  "$LAKE_HOST_DIR" \
  "$ROOT_DIR/PythonDataService/cache" \
  "$ROOT_DIR/PythonDataService/lean-cache" \
  "$ROOT_DIR/PythonDataService/artifacts/alpaca_clerk"
do
  mkdir -p "$host_dir"
done

echo "==> Building containers (first build is slow)..."
export COMPOSE_BAKE=false   # match restart.sh: avoid the bake fallback warning
podman compose build

# First-run ceremonies (windows-onboarding §4a, §4b). The data plane refuses to
# start without them (exit 78 and exit 3), which is what a first run used to
# time out on. They run as one-shot python-service containers, after the build
# (they need the image) and before `up`.
#
# (a) The Alpaca Clerk volume. compose.yaml declares it external, so it must be
#     created explicitly, and the data plane needs a `_compose_volume_ready`
#     marker inside it. The marker is written only for a provably first install:
#     already marked is a no-op, and anything in the volume or in the legacy host
#     tree (mounted read-only at /app/alpaca_clerk_legacy) is an existing
#     authority set that only the cutover runbook may adopt (exit 3). A tree
#     that cannot be inspected is not an empty one (exit 4).
CLERK_VOLUME="learn-ai-alpaca-clerk-data"
if ! podman volume exists "$CLERK_VOLUME"; then
  echo "==> Creating the Alpaca Clerk volume ($CLERK_VOLUME)..."
  podman volume create "$CLERK_VOLUME" >/dev/null
fi
SEAL_CLERK_VOLUME='
  dir=/app/artifacts/alpaca_clerk
  legacy=/app/alpaca_clerk_legacy
  [ -d "$dir" ] && [ -d "$legacy" ] || exit 4
  if [ -f "$dir/_compose_volume_ready" ] && [ ! -L "$dir/_compose_volume_ready" ]; then exit 0; fi
  if [ -n "$(ls -A "$dir")" ] || [ -n "$(ls -A "$legacy")" ]; then exit 3; fi
  : > "$dir/_compose_volume_ready"
'
seal_status=0
podman compose run --rm --no-deps -T --entrypoint /bin/sh python-service -c "$SEAL_CLERK_VOLUME" \
  || seal_status=$?
case "$seal_status" in
  0) echo "==> Alpaca Clerk volume is marked ready" ;;
  3)
    echo "ERROR: the Alpaca Clerk volume ($CLERK_VOLUME) or the legacy host tree" >&2
    echo "       (PythonDataService/artifacts/alpaca_clerk) already holds data, so this is" >&2
    echo "       not a first install and the volume was NOT marked ready. Adopt that" >&2
    echo "       data with docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md," >&2
    echo "       then re-run this script." >&2
    exit 1
    ;;
  *)
    echo "ERROR: could not check the Alpaca Clerk volume (podman compose run exited $seal_status)." >&2
    exit 1
    ;;
esac

# (b) The data-lake root identity (#1876). The root claims a UUID once; an id
#     already in PythonDataService/.env is the one to claim, else a new one is
#     minted and recorded there after the claim succeeds. init refuses a
#     populated root (that is `manage_data_root stamp`, a deliberate step).
LAKE_MARKER="$LAKE_HOST_DIR/lake/.data-root.json"
if [[ -f "$LAKE_MARKER" ]]; then
  echo "==> Data-lake root identity already set ($LAKE_MARKER)"
else
  LAKE_ROOT_ID="$(env_file_value "$ROOT_DIR/PythonDataService/.env" DATA_LAKE_ROOT_ID)"
  RECORD_LAKE_ROOT_ID=false
  if [[ -z "$LAKE_ROOT_ID" ]]; then
    LAKE_ROOT_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
    RECORD_LAKE_ROOT_ID=true
  fi
  echo "==> Claiming the data-lake root as $LAKE_ROOT_ID..."
  if ! podman compose run --rm --no-deps -T python-service \
       python -m scripts.manage_data_root init --root-id "$LAKE_ROOT_ID"; then
    echo "ERROR: could not claim the data-lake root at $LAKE_HOST_DIR." >&2
    echo "       See docs/runbooks/windows-onboarding.md section 4b; a root that already" >&2
    echo "       holds data is stamped deliberately with manage_data_root stamp." >&2
    exit 1
  fi
  if [[ "$RECORD_LAKE_ROOT_ID" == "true" ]]; then
    printf '\nDATA_LAKE_ROOT_ID=%s\n' "$LAKE_ROOT_ID" >> "$ROOT_DIR/PythonDataService/.env"
    echo "==> Recorded DATA_LAKE_ROOT_ID in PythonDataService/.env"
  fi
fi

echo "==> Starting containers..."
# Build and up are separated (as restart.sh does): a real build failure must
# abort under `set -e`, but `up` can exit non-zero merely because a
# `depends_on: service_healthy` dependency misses the compose startup window on
# a cold/slow first run. Tolerate that here (`|| true`) so the health-wait loop
# below — not compose's startup race — is the authoritative readiness gate and
# containers aren't left stranded in `Created`.
podman compose up -d || true

# ---------------------------------------------------------------------------
# 6. Wait for health and report.
# ---------------------------------------------------------------------------
echo "==> Waiting for services to become healthy..."
wait_for() {
  local name="$1" url="$2" tries=60
  while (( tries-- > 0 )); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      echo "    ✅ $name is up ($url)"
      return 0
    fi
    sleep 3
  done
  echo "    ⚠️  $name did not respond at $url within timeout — check: podman compose logs $name"
  return 1
}

# Track failures rather than swallowing them with `|| true`: a one-shot setup
# script must not report success while the API is unusable (bad env value, port
# conflict, migration failure, crashed service).
health_failures=0
wait_for "python-service" "http://localhost:8000/health" || health_failures=$((health_failures + 1))
wait_for "backend (GraphQL)" "http://localhost:5000/graphql?sdl" || health_failures=$((health_failures + 1))
wait_for "frontend" "http://localhost:4200" || health_failures=$((health_failures + 1))

echo ""
echo "==> Container status:"
podman compose ps

if (( health_failures > 0 )); then
  echo ""
  echo "  ❌ $health_failures required service(s) never became healthy — the stack is NOT usable."
  echo "      Inspect logs (podman compose logs) and re-run this script after fixing the cause."
  exit 1
fi

# ---------------------------------------------------------------------------
# 7. Report.
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " The stack is up. Open http://localhost:4200"
echo ""
echo " Services:"
echo "   Frontend     http://localhost:4200  (my-frontend container, hot reload)"
echo "   GraphQL      http://localhost:5000/graphql"
echo "   Python API   http://localhost:8000/health"
echo "   Postgres     localhost:5432"
echo "   Redis        localhost:6379"
echo ""
echo " This is the combined posture (one data-plane process). To run the"
echo " broker clerks, set up the fleet lanes (see"
echo " docs/runbooks/first-time-setup.md); from then on use ./restart.sh."
echo ""
echo " The full Python test suite runs from a HOST venv, not the container."
echo " Provision it once with:"
echo "   ./bootstrap-host-venv.sh"
echo "============================================================"
