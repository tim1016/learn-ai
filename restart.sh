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

# `compose up --no-cache` is not supported by the docker-compose plugin
# (the flag is rejected at parse time). Build first when --no-cache is
# requested, then up. The cached path can fold build into up as before.
if [[ -n "$NO_CACHE" ]]; then
  echo "==> Building images from scratch..."
  podman compose build --no-cache --pull
  echo "==> Starting all services..."
  podman compose up -d --force-recreate
else
  echo "==> Building and starting all services..."
  podman compose up -d --build --force-recreate
fi

echo "==> Waiting for services to become healthy..."
for i in {1..30}; do
  HEALTHY=$(podman ps --filter health=healthy --format "{{.Names}}" | wc -l)
  TOTAL=$(podman ps --format "{{.Names}}" | wc -l)
  echo "    [$i] $HEALTHY/$TOTAL healthy"
  if [[ "$HEALTHY" -ge "$TOTAL" && "$TOTAL" -gt 0 ]]; then
    echo "==> All $TOTAL services healthy!"
    podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
  fi
  sleep 3
done

echo "==> WARNING: Not all services healthy after 90s"
podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
exit 1
