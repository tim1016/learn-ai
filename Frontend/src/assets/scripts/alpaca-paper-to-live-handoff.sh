#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f compose.yaml || ! -d PythonDataService ]]; then
  echo "Run this script from the learn-ai repository root." >&2
  exit 1
fi

# The worker's compose target, matching exactly what the desk authored for
# this lane (PythonDataService/app/broker_configuration/desk_state.py's
# worker_restart_command, fed by runtime.py's worker_restart_target_from):
# the deployment's declared FLEET_WORKER_SERVICE, plus whatever compose
# context its own file does not already resolve. In the fleet posture each
# lane is its own service, so a wrong guess here restarts a process that
# applies no lane's staged profile — exactly the bug #2147 exists to fix.
#
# These are never read from a repo-root .env: FLEET_WORKER_SERVICE and its
# compose-context siblings are declared only inside compose service
# environments (compose.yaml, compose.fleet.dev.yaml, compose.fleet.yaml),
# so a host shell never sees them unless something sets them first. The
# Configuration page's "Copy handoff script" button is that something — it
# fills in this lane's exact values before you paste. Running the raw asset
# without going through that button requires exporting FLEET_WORKER_SERVICE
# yourself; there is no default, because a wrong guess here means silently
# restarting the wrong container.
worker_service="${FLEET_WORKER_SERVICE:?Set FLEET_WORKER_SERVICE to the compose service for this lane, or copy this script from the Configuration page, which fills it in for you.}"
compose_words=(podman compose)
if [[ -n "${FLEET_COMPOSE_PROJECT:-}" ]]; then
  compose_words+=(--project-name "$FLEET_COMPOSE_PROJECT")
fi
if [[ -n "${FLEET_COMPOSE_FILES:-}" ]]; then
  IFS=',' read -r -a compose_files <<< "$FLEET_COMPOSE_FILES"
  for compose_file in "${compose_files[@]}"; do
    # Trim surrounding whitespace per entry, matching config.py's
    # _split_compose_files (entry.strip()) — "a, b" must resolve to the
    # same two file names on both sides, not "a" and " b".
    compose_file="${compose_file#"${compose_file%%[![:space:]]*}"}"
    compose_file="${compose_file%"${compose_file##*[![:space:]]}"}"
    compose_words+=(-f "$compose_file")
  done
fi
if [[ -n "${FLEET_COMPOSE_PROFILE:-}" ]]; then
  compose_words+=(--profile "$FLEET_COMPOSE_PROFILE")
fi

read -r -p "Disposable Alpaca Paper account ID: " PAPER_ACCOUNT_ID
if [[ -z "$PAPER_ACCOUNT_ID" ]]; then
  echo "A Paper account ID is required." >&2
  exit 1
fi

worker_stopped=0
restore_worker() {
  if [[ "$worker_stopped" -eq 1 ]]; then
    "${compose_words[@]}" start "$worker_service" >/dev/null 2>&1 || true
  fi
}
trap restore_worker EXIT

echo "Stopping the Alpaca worker ($worker_service)..."
"${compose_words[@]}" stop "$worker_service"
worker_stopped=1

echo "Resetting the disposable Paper workspace after its own safety checks..."
"${compose_words[@]}" run --rm --no-deps "$worker_service" \
  python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --account-id "$PAPER_ACCOUNT_ID" \
  dev-reset --runner-artifacts-root /app/artifacts

echo "Starting the worker so the Configuration page can record Apply..."
"${compose_words[@]}" start "$worker_service"
worker_stopped=0
trap - EXIT

data_plane_url="http://127.0.0.1:8000"

# Who serves the Configuration surface, and therefore whose control secret
# the ceremony needs, depends on the posture (#2158):
#
# - Combined: the worker behind 127.0.0.1:8000 is also the clerk, answers
#   the direct in-process route, and holds DATA_PLANE_CONTROL_SECRET
#   itself (compose.yaml hands it to python-service).
# - Fleet: the coordinator publishes 8000 but mounts no configuration
#   router of its own (_ROLE_RUNS_CLERK, PythonDataService/app/main.py),
#   the lanes publish nothing on the host, and a lane carries no control
#   secret at all — only the coordinator does. The same surface is reached
#   the way the Configuration page reaches it: the coordinator's
#   clerk-scoped fleet catalog route /api/brokers/{broker}/clerks/{clerk_id}
#   (PythonDataService/app/routers/broker_clerks.py), authenticated with
#   that same control secret.
#
# The lane's own environment names its coordinator (FLEET_COORDINATOR_URL),
# whose host is the coordinator's compose service name inside the
# deployment's network — the same self-declaration FLEET_WORKER_SERVICE
# uses, so no service name is ever guessed here. FLEET_COORDINATOR_URL and
# FLEET_CLERK_ID exist only on enrolled lanes; their absence selects the
# combined posture's worker-direct behavior. All three execs are guarded
# because printenv of an unset variable exits nonzero, and `set -euo
# pipefail` must not read that as a failure of the ceremony itself.
coordinator_url="$( ( "${compose_words[@]}" exec -T "$worker_service" printenv FLEET_COORDINATOR_URL 2>/dev/null || true) | tr -d '\r\n' )"
if [[ -n "$coordinator_url" ]]; then
  coordinator_service="${coordinator_url#*://}"
  coordinator_service="${coordinator_service%%[:/]*}"
  control_secret="$( ( "${compose_words[@]}" exec -T "$coordinator_service" printenv DATA_PLANE_CONTROL_SECRET 2>/dev/null || true) | tr -d '\r\n' )"
else
  control_secret="$( ( "${compose_words[@]}" exec -T "$worker_service" printenv DATA_PLANE_CONTROL_SECRET 2>/dev/null || true) | tr -d '\r\n' )"
fi
if [[ -z "$control_secret" ]]; then
  echo "The data-plane control credential is unavailable. The worker is running; use the Configuration page manually." >&2
  exit 1
fi
fleet_clerk_id="$( ( "${compose_words[@]}" exec -T "$worker_service" printenv FLEET_CLERK_ID 2>/dev/null || true) | tr -d '\r\n' )"
config_route() {
  if [[ -n "$fleet_clerk_id" ]]; then
    printf '%s/api/brokers/alpaca/clerks/%s%s' "$data_plane_url" "$fleet_clerk_id" "$1"
  else
    printf '%s/api/brokers/alpaca%s' "$data_plane_url" "$1"
  fi
}
selection_url="$(config_route /configuration/selection)"
selection_json=""
for _ in {1..30}; do
  if selection_json="$(curl --fail --silent --show-error \
    -H "X-Data-Plane-Control-Secret: $control_secret" "$selection_url" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
if [[ -z "$selection_json" ]]; then
  echo "The Configuration page did not become ready. The worker is running; inspect the page before continuing." >&2
  exit 1
fi

configuration_url="http://localhost:4200/brokers/alpaca/configuration"
if command -v open >/dev/null 2>&1; then
  open "$configuration_url"
fi
echo "Open $configuration_url"
echo "Stage the saved Live profile if needed, then click Apply staged revision."
read -r -p "Press Enter only after the page says Apply is recorded... "

selection_json="$(curl --fail --silent --show-error \
  -H "X-Data-Plane-Control-Secret: $control_secret" "$selection_url")"
read -r apply_requested staged_profile staged_revision < <(
  printf '%s' "$selection_json" | python3 -c \
    'import json, sys; value = json.load(sys.stdin); print(str(bool(value.get("apply_requested"))).lower(), value.get("staged_profile_id") or "", value.get("staged_revision") or "")'
)
if [[ "$apply_requested" != "true" || -z "$staged_profile" || -z "$staged_revision" ]]; then
  echo "Apply is not recorded for a staged profile. Nothing else was restarted." >&2
  exit 1
fi

encoded_profile="$(printf '%s' "$staged_profile" | python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')"
revision_url="$(config_route "/configuration/profiles/$encoded_profile/revisions/$staged_revision")"
endpoint_mode="$(curl --fail --silent --show-error \
  -H "X-Data-Plane-Control-Secret: $control_secret" "$revision_url" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin).get("endpoint_mode", ""))')"
if [[ "$endpoint_mode" != "live" ]]; then
  echo "The staged profile is not Live. The worker was not restarted." >&2
  exit 1
fi

echo "Restarting the worker to make the recorded Live profile effective..."
"${compose_words[@]}" restart "$worker_service"

for _ in {1..30}; do
  if selection_json="$(curl --fail --silent --show-error \
    -H "X-Data-Plane-Control-Secret: $control_secret" "$selection_url" 2>/dev/null)" \
    && printf '%s' "$selection_json" | python3 -c \
      'import json, sys; value = json.load(sys.stdin); ok = not value.get("apply_requested") and value.get("effective_profile_id") == value.get("staged_profile_id") and value.get("effective_revision") == value.get("staged_revision"); raise SystemExit(0 if ok else 1)'; then
    echo "Live profile is effective. Live bot trading remains unarmed."
    exit 0
  fi
  sleep 1
done

echo "The restart finished without proving the Live profile effective. Read Last Apply on the Configuration page." >&2
exit 1
