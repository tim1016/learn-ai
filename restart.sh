#!/usr/bin/env bash
# Restart all services with fresh containers.
# Usage:
#   ./restart.sh              # Rebuild only changed layers (fast)
#   ./restart.sh --no-cache   # Full rebuild from scratch (slow, ~5min)
#   ./restart.sh --help       # Show usage and exit. Touches no container.

set -euo pipefail

# Compose v2.32+ auto-opts into Buildx Bake when multiple services have build:
# contexts. Under the podman socket BuildKit isn't wired up, so the warning
# fires and Compose falls back to the classic builder. Disable bake to silence.
export COMPOSE_BAKE=false

usage() {
  cat <<'USAGE'
Usage: ./restart.sh [--no-cache] [--help]

  --no-cache    Rebuild every image from scratch (no layer cache), slow (~5 min).
  --help, -h    Show this help and exit. Touches no container.

No other argument is recognised. This script's very first destructive action
is `podman compose down`, which tears down every service including both
broker clerks -- an unrecognised argument must be refused before that ever
runs, not fall through to it.
USAGE
}

# Argument parsing happens before anything else in this script, deliberately
# ahead of the COMPOSE_ARGS setup and the teardown below. An unrecognised
# flag (a typo, `--help` before this existed) must never reach
# `podman compose down` -- it did once, and only an unrelated SIGPIPE from a
# piped consumer aborted the script before the teardown executed.
NO_CACHE=""
case "${1:-}" in
  "") ;;
  --no-cache)
    NO_CACHE="--no-cache"
    echo "==> Full rebuild (no cache) requested"
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  *)
    echo "Unrecognised argument: $1" >&2
    usage >&2
    exit 1
    ;;
esac
if [[ $# -gt 1 ]]; then
  echo "Unrecognised extra argument(s): ${*:2}" >&2
  usage >&2
  exit 1
fi

# Compose auto-loads only compose.yaml and compose.override.yaml. The
# committed fleet topology (compose.fleet.dev.yaml) is a third file, so it
# must be named explicitly or the stack silently reverts to the unfenced
# `combined` posture (fleet_boot.py:108-116) on every restart. Order matters:
# later files win, so the dev-only override still applies last. An explicit
# COMPOSE_FILE in the environment takes over entirely, as Compose intends.
COMPOSE_ARGS=()
if [[ -z "${COMPOSE_FILE:-}" ]]; then
  COMPOSE_ARGS+=(--file compose.yaml)
  [[ -f compose.fleet.dev.yaml ]] && COMPOSE_ARGS+=(--file compose.fleet.dev.yaml)
  [[ -f compose.override.yaml ]] && COMPOSE_ARGS+=(--file compose.override.yaml)
fi
echo "==> Compose files: ${COMPOSE_ARGS[*]:-\$COMPOSE_FILE=$COMPOSE_FILE}"

echo "==> Tearing down all containers..."
podman compose ${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"} down

# Compose ownership is decided by the label Compose itself stamps, never by a
# hardcoded service list. A hardcoded list silently misclassifies every service
# added after it was written: the broker clerk agents (`alpaca-live-clerk`,
# `alpaca-paper-clerk`) were absent from the original five names, so a clerk
# left in Created was reaped as an "orphan" below and never recovered above —
# destroying a live execution lane on a routine `./restart.sh`.
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"
COMPOSE_LABEL="com.docker.compose.project=${COMPOSE_PROJECT}"

# Reap orphaned non-compose containers stuck in Created (typically `sleep 1`
# probes from lean-sidecar metadata staging that the host process abandons).
# `podman compose down` only touches compose-managed names, so these pile up
# in `podman ps -a` over time. Skip if none — `xargs --no-run-if-empty` isn't
# portable, so guard explicitly.
#
# The destructive query deliberately does NOT mention ${COMPOSE_PROJECT}. Asking
# for "created containers not in *our* project" would make a wrong project name
# catastrophic: the label would match nothing and every core service sitting in
# Created would be reaped. Asking for "created containers carrying no compose
# project label at all" is positive evidence of non-ownership, so a wrong
# project name can only ever under-reap. A container belonging to some other
# Compose project is left alone either way, which is also correct.
ORPHANS=$(podman ps -a --filter status=created \
  --filter "label!=com.docker.compose.project" --format "{{.Names}}" || true)
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
  podman compose ${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"} build --no-cache --pull
else
  echo "==> Building images..."
  podman compose ${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"} build
fi
echo "==> Starting all services..."
podman compose ${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"} up -d --force-recreate || true

# Recover services left in Created. When a `depends_on: service_healthy`
# target takes longer than expected to flip healthy, compose abandons the
# dependent in Created and never retries — even after the dep recovers.
# Observed on cold restarts where postgres takes >25s for WAL fsync
# recovery. Try starting any compose container still in Created; if its
# deps are now healthy, it'll come up.
# Re-read: the reap above may have removed non-compose entries.
STUCK=$(podman ps -a --filter status=created \
  --filter "label=${COMPOSE_LABEL}" --format "{{.Names}}" || true)
if [[ -n "$STUCK" ]]; then
  echo "==> Starting compose services left in Created:"
  echo "$STUCK" | sed 's/^/    /'
  echo "$STUCK" | xargs -r podman start >/dev/null 2>&1 || true
fi

# What Compose says should exist. The health verdict below compares against
# this, not against however many containers happen to be running: `podman ps`
# lists running containers only, so a service that crashed on boot is absent
# from BOTH the healthy count and the total, the ratio stays balanced, and the
# script reports "All N services healthy!" while a live execution lane is
# simply gone. Counting what should be there is what makes a missing clerk
# visible.
EXPECTED_SERVICES=$(podman compose ${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"} config --services 2>/dev/null | sort || true)

# Wait budget: the longest healthcheck start_period in compose.yaml is the
# frontend at 120s; backend cold compile pushes 60–90s on top. Poll for
# 240s (80 x 3s) so the script's verdict matches reality on cold builds.
# Poll budget is injectable so a test can drive the verdict without waiting
# the full production budget; unset, it is the 80 x 3s described above.
RESTART_HEALTH_ATTEMPTS="${RESTART_HEALTH_ATTEMPTS:-80}"
RESTART_HEALTH_INTERVAL="${RESTART_HEALTH_INTERVAL:-3}"
echo "==> Waiting for services to become healthy..."
for i in $(seq 1 "$RESTART_HEALTH_ATTEMPTS"); do
  HEALTHY=$(podman ps --filter "label=${COMPOSE_LABEL}" \
    --filter health=healthy --format "{{.Names}}" | wc -l)
  TOTAL=$(podman ps --filter "label=${COMPOSE_LABEL}" --format "{{.Names}}" | wc -l)
  RUNNING_SERVICES=$(podman ps --filter "label=${COMPOSE_LABEL}" \
    --format '{{index .Labels "com.docker.compose.service"}}' | sort -u || true)
  MISSING=$(comm -23 <(printf '%s\n' "$EXPECTED_SERVICES") \
    <(printf '%s\n' "$RUNNING_SERVICES") | grep -v '^$' || true)
  if [[ -n "$MISSING" ]]; then
    echo "    [$i] $HEALTHY/$TOTAL healthy; not running: $(echo $MISSING | tr '\n' ' ')"
  else
    echo "    [$i] $HEALTHY/$TOTAL healthy"
  fi
  if [[ -z "$MISSING" && "$HEALTHY" -ge "$TOTAL" && "$TOTAL" -gt 0 ]]; then
    echo "==> All $TOTAL services healthy!"
    podman ps --filter "label=${COMPOSE_LABEL}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
  fi
  # Mid-loop rescue: if a compose service drifted back into Created
  # because its dep flapped, recover it without waiting for the next
  # restart. Cheap to retry.
  STUCK=$(podman ps -a --filter status=created \
    --filter "label=${COMPOSE_LABEL}" --format "{{.Names}}" || true)
  if [[ -n "$STUCK" ]]; then
    echo "$STUCK" | xargs -r podman start >/dev/null 2>&1 || true
  fi
  sleep "$RESTART_HEALTH_INTERVAL"
done

echo "==> WARNING: Not all services healthy after $((RESTART_HEALTH_ATTEMPTS * RESTART_HEALTH_INTERVAL))s"
if [[ -n "${MISSING:-}" ]]; then
  echo "==> Declared by compose but NOT RUNNING: $(echo $MISSING | tr '\n' ' ')"
fi
podman ps --filter "label=${COMPOSE_LABEL}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
exit 1
