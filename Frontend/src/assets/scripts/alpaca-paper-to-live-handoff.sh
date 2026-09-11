#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f compose.yaml || ! -d PythonDataService ]]; then
  echo "Run this script from the learn-ai repository root." >&2
  exit 1
fi

read -r -p "Disposable Alpaca Paper account ID: " PAPER_ACCOUNT_ID
if [[ -z "$PAPER_ACCOUNT_ID" ]]; then
  echo "A Paper account ID is required." >&2
  exit 1
fi

worker_stopped=0
restore_worker() {
  if [[ "$worker_stopped" -eq 1 ]]; then
    podman compose start python-service >/dev/null 2>&1 || true
  fi
}
trap restore_worker EXIT

echo "Stopping the Alpaca worker..."
podman compose stop python-service
worker_stopped=1

echo "Resetting the disposable Paper workspace after its own safety checks..."
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --account-id "$PAPER_ACCOUNT_ID" \
  dev-reset --runner-artifacts-root /app/artifacts

echo "Starting the worker so the Configuration page can record Apply..."
podman compose start python-service
worker_stopped=0
trap - EXIT

data_plane_url="http://127.0.0.1:8000"
selection_url="$data_plane_url/api/brokers/alpaca/configuration/selection"
control_secret="$(podman compose exec -T python-service printenv DATA_PLANE_CONTROL_SECRET 2>/dev/null | tr -d '\r\n')"
if [[ -z "$control_secret" ]]; then
  echo "The data-plane control credential is unavailable. The worker is running; use the Configuration page manually." >&2
  exit 1
fi
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
revision_url="$data_plane_url/api/brokers/alpaca/configuration/profiles/$encoded_profile/revisions/$staged_revision"
endpoint_mode="$(curl --fail --silent --show-error \
  -H "X-Data-Plane-Control-Secret: $control_secret" "$revision_url" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin).get("endpoint_mode", ""))')"
if [[ "$endpoint_mode" != "live" ]]; then
  echo "The staged profile is not Live. The worker was not restarted." >&2
  exit 1
fi

echo "Restarting the worker to make the recorded Live profile effective..."
podman compose restart python-service

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
