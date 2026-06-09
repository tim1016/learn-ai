#!/usr/bin/env bash
# One-shot dev bootstrap for macOS (Apple Silicon, Tahoe 26+).
#
# Provisions the Podman VM with generous resources, installs the host
# toolchain, wires up .env files, builds the container stack, and brings
# it up. Idempotent — safe to re-run; it reconfigures rather than
# duplicates.
#
# Usage:
#   ./setup-macos.sh                         # full setup, leaves frontend for you to start
#   ./setup-macos.sh --serve                 # also runs `npm install` + `ng serve` at the end
#   ./setup-macos.sh --with-host-daemon      # also bootstrap the host venv + start the
#                                             live-engine daemon (needed for /broker/* pages
#                                             — kept opt-in because the venv install adds
#                                             ~3 min to a fresh setup). Composable with --serve.
#
# Resource overrides (env vars, optional — defaults are auto-computed):
#   PODMAN_CPUS=8 PODMAN_MEMORY_MB=16384 PODMAN_DISK_GB=120 ./setup-macos.sh
#
# This is the macOS analogue of ./restart.sh (which targets GNU/Linux
# userland and is not portable to BSD tools).

set -euo pipefail

SERVE=false
WITH_HOST_DAEMON=false
for arg in "$@"; do
  case "$arg" in
    --serve)             SERVE=true ;;
    --with-host-daemon)  WITH_HOST_DAEMON=true ;;
    -h|--help)
      sed -n '2,17p' "$0"
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
# node:           the Angular frontend dev server
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
#    things up past the working set: this 3-container stack peaks ~15-20 GB
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
# 5. Build + bring up the container stack (db, python-service, backend).
#    First build is slow (~5-10 min): .NET SDK image + Python heavy deps.
# ---------------------------------------------------------------------------
echo "==> Building and starting containers (first build is slow)..."
export COMPOSE_BAKE=false   # match restart.sh: avoid the bake fallback warning
# Build and up are separated (as restart.sh does): a real build failure must
# abort under `set -e`, but `up` can exit non-zero merely because a
# `depends_on: service_healthy` dependency misses the compose startup window on
# a cold/slow first run. Tolerate that here (`|| true`) so the health-wait loop
# below — not compose's startup race — is the authoritative readiness gate and
# containers aren't left stranded in `Created`.
podman compose build
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
# 7. Host live-engine daemon (opt-in: --with-host-daemon).
#
#    /broker/* UI surfaces poll a HOST process at 127.0.0.1:8765 — it cannot
#    live in a container because IBKR Gateway binds reqRealTimeBars to the
#    login-session source IP (error 420). Without it, /broker/instances
#    renders "Live engine unavailable" with no working Recheck.
#
#    Delegated to ./bootstrap-host-daemon.sh so the venv setup and daemon
#    lifecycle live in one place; this hook just opts a fresh machine in.
# ---------------------------------------------------------------------------
if [[ "$WITH_HOST_DAEMON" == "true" ]]; then
  echo ""
  echo "==> Bootstrapping host venv + starting live-engine daemon..."
  "$ROOT_DIR/bootstrap-host-daemon.sh"
fi

# ---------------------------------------------------------------------------
# 8. Frontend.
# ---------------------------------------------------------------------------
if [[ "$SERVE" == "true" ]]; then
  echo "==> Installing frontend deps + starting ng serve (foreground)..."
  cd "$ROOT_DIR/Frontend"
  npm install
  echo "==> Frontend will be at http://localhost:4200 — Ctrl-C to stop."
  exec npx ng serve
else
  echo ""
  echo "============================================================"
  echo " Backend stack is up. To start the frontend:"
  echo ""
  echo "   cd Frontend && npm install && npx ng serve"
  echo ""
  echo " Then open http://localhost:4200"
  echo ""
  echo " Services:"
  echo "   Frontend     http://localhost:4200  (after ng serve)"
  echo "   GraphQL      http://localhost:5000/graphql"
  echo "   Python API   http://localhost:8000/health"
  echo "   Postgres     localhost:5432"
  if [[ "$WITH_HOST_DAEMON" == "true" ]]; then
    echo "   Host daemon  http://127.0.0.1:8765/health"
  else
    echo ""
    echo " /broker/* pages need the host daemon. Start it with:"
    echo "   ./bootstrap-host-daemon.sh"
  fi
  echo "============================================================"
fi
