#!/usr/bin/env bash
# Restart all services with fresh containers.
# Usage:
#   ./restart.sh              # Rebuild only changed layers (fast)
#   ./restart.sh --no-cache   # Full rebuild from scratch (slow, ~5min)

set -euo pipefail

NO_CACHE=""
if [[ "${1:-}" == "--no-cache" ]]; then
  NO_CACHE="--no-cache"
  echo "==> Full rebuild (no cache) requested"
fi

echo "==> Tearing down all containers..."
podman compose down

echo "==> Building and starting all services..."
podman compose up -d --build --force-recreate $NO_CACHE

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
