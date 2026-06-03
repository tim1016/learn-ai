#!/usr/bin/env bash
# Restart all services with fresh containers.
# Usage:
#   ./restart.sh              # Rebuild only changed layers (fast)
#   ./restart.sh --no-cache   # Full rebuild from scratch (slow, ~5min)

set -euo pipefail

# Compose v2.32+ auto-opts into Buildx Bake when multiple services have build:
# contexts. Under the podman socket BuildKit isn't wired up, so the warning
# fires and Compose falls back to the classic builder. Disable bake to silence.
export COMPOSE_BAKE=false

NO_CACHE=""
if [[ "${1:-}" == "--no-cache" ]]; then
  NO_CACHE="--no-cache"
  echo "==> Full rebuild (no cache) requested"
fi

echo "==> Tearing down all containers..."
podman compose down

# Reap orphaned non-compose containers stuck in Created (typically `sleep 1`
# probes from lean-sidecar metadata staging that the host process abandons).
# `podman compose down` only touches compose-managed names, so these pile up
# in `podman ps -a` over time. Skip if none — `xargs --no-run-if-empty`
# isn't portable, so guard explicitly.
ORPHANS=$(podman ps -a --filter status=created --format "{{.Names}}" | grep -Ev '^(my-postgres|my-redis|my-backend|my-frontend|polygon-data-service)$' || true)
if [[ -n "$ORPHANS" ]]; then
  echo "==> Reaping orphaned Created containers:"
  echo "$ORPHANS" | sed 's/^/    /'
  echo "$ORPHANS" | xargs -r podman rm -f >/dev/null
fi

# `compose up --no-cache` is not supported by the docker-compose plugin
# (the flag is rejected at parse time), so both paths build as a separate
# step. Keeping `build` separate is also what preserves build-failure
# visibility: a genuine image-build failure aborts the script under `set -e`
# instead of being swallowed by the `|| true` that the *up* step needs (see
# below). Folding `--build` into `up` would hide the build's exit status.
# NOTE: `podman compose up` exits non-zero when a `depends_on:
# service_healthy` gate doesn't flip in time — it abandons the dependent in
# Created (see the recovery block below). Under `set -e` that non-zero exit
# would kill the script before the rescue logic runs, stranding the
# dependents permanently. Guard *only the up* with `|| true`: the
# Created-recovery blocks and the 240s health-wait loop below are the
# authoritative verdict and will exit 1 if anything is genuinely unhealthy.
if [[ -n "$NO_CACHE" ]]; then
  echo "==> Building images from scratch..."
  podman compose build --no-cache --pull
else
  echo "==> Building images..."
  podman compose build
fi
echo "==> Starting all services..."
podman compose up -d --force-recreate || true

# Recover services left in Created. When a `depends_on: service_healthy`
# target takes longer than expected to flip healthy, compose abandons the
# dependent in Created and never retries — even after the dep recovers.
# Observed on cold restarts where postgres takes >25s for WAL fsync
# recovery. Try starting any compose container still in Created; if its
# deps are now healthy, it'll come up.
STUCK=$(podman ps -a --filter status=created --format "{{.Names}}" \
  | grep -E '^(my-postgres|my-redis|my-backend|my-frontend|polygon-data-service)$' || true)
if [[ -n "$STUCK" ]]; then
  echo "==> Starting compose services left in Created:"
  echo "$STUCK" | sed 's/^/    /'
  echo "$STUCK" | xargs -r podman start >/dev/null 2>&1 || true
fi

# Wait budget: the longest healthcheck start_period in compose.yaml is the
# frontend at 120s; backend cold compile pushes 60–90s on top. Poll for
# 240s (80 x 3s) so the script's verdict matches reality on cold builds.
echo "==> Waiting for services to become healthy..."
for i in {1..80}; do
  HEALTHY=$(podman ps --filter health=healthy --format "{{.Names}}" | wc -l)
  TOTAL=$(podman ps --format "{{.Names}}" | wc -l)
  echo "    [$i] $HEALTHY/$TOTAL healthy"
  if [[ "$HEALTHY" -ge "$TOTAL" && "$TOTAL" -gt 0 ]]; then
    echo "==> All $TOTAL services healthy!"
    podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
  fi
  # Mid-loop rescue: if a compose service drifted back into Created
  # because its dep flapped, recover it without waiting for the next
  # restart. Cheap to retry.
  STUCK=$(podman ps -a --filter status=created --format "{{.Names}}" \
    | grep -E '^(my-postgres|my-redis|my-backend|my-frontend|polygon-data-service)$' || true)
  if [[ -n "$STUCK" ]]; then
    echo "$STUCK" | xargs -r podman start >/dev/null 2>&1 || true
  fi
  sleep 3
done

echo "==> WARNING: Not all services healthy after 240s"
podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
exit 1
