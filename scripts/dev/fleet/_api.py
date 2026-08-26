"""Shared data-plane API client for fleet stress tooling.

Stdlib-only (urllib) so it runs on the host without a venv. The control
secret is read from the environment or from ``PythonDataService/.env`` and
is never logged.

Origin: rebuilt from the 2026-08-25 fleet-stress session tooling, which the
audit (docs/audits/bot-fleet-stress-2026-08-25.md §1) flagged as candidates
for ``scripts/dev/``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("FLEET_API_BASE", "http://localhost:8000")
ACCOUNT_ID = os.environ.get("FLEET_ACCOUNT_ID", "PA3KWXU1C4C3")
BROKER = os.environ.get("FLEET_BROKER", "alpaca")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def control_secret() -> str:
    env = os.environ.get("DATA_PLANE_CONTROL_SECRET")
    if env:
        return env
    for env_file in (_REPO_ROOT / "PythonDataService" / ".env", _REPO_ROOT / ".env"):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("DATA_PLANE_CONTROL_SECRET="):
                    return line.split("=", 1)[1].strip()
    raise RuntimeError("DATA_PLANE_CONTROL_SECRET not found in env or .env files")


TRANSPORT_FAILURE_STATUS = 0
"""The status these tools report when no HTTP response was obtained at all."""


def request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> tuple[int, Any, float]:
    """Return (status_code, parsed_json, latency_seconds).

    A transport failure -- refused connection, timeout, dropped socket -- is
    normalized to ``TRANSPORT_FAILURE_STATUS`` with a detail payload rather
    than raised. These tools exist to run *through* the outages they induce
    (SIGKILL, SIGSTOP, container restart); a raising client would abort the
    sweep at the first casualty and lose every later observation, which is
    exactly the evidence the probe was started to collect.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={
            "X-Data-Plane-Control-Secret": control_secret(),
            "Content-Type": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode()), time.monotonic() - started
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"detail": raw[:500]}
        return err.code, payload, time.monotonic() - started
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return (
            TRANSPORT_FAILURE_STATUS,
            {"detail": f"transport failure: {reason}"},
            time.monotonic() - started,
        )


def list_roster() -> list[dict[str, Any]]:
    status, payload, _ = request("GET", f"/api/brokers/{BROKER}/bots")
    if status != 200:
        raise RuntimeError(f"roster read failed: {status} {payload}")
    return payload


def get_catalog() -> list[dict[str, Any]]:
    status, payload, latency = request(
        "GET", f"/api/brokers/{BROKER}/accounts/{ACCOUNT_ID}/bots/catalog"
    )
    if status != 200:
        raise RuntimeError(f"catalog read failed: {status} {payload}")
    logger.debug("catalog read", extra={"rows": len(payload), "latency_s": latency})
    return payload


def get_panel(sid: str) -> dict[str, Any]:
    status, payload, _ = request(
        "GET", f"/api/brokers/{BROKER}/accounts/{ACCOUNT_ID}/bots/{sid}/panel"
    )
    if status != 200:
        raise RuntimeError(f"panel read failed for {sid}: {status} {payload}")
    return payload


def post_action(
    sid: str,
    action_id: str,
    *,
    reason: str | None = None,
    idempotency_key: str | None = None,
    panel: dict[str, Any] | None = None,
    allow_disabled: bool = False,
) -> tuple[int, Any, float]:
    """Rebind revision + concurrency_token from a fresh panel read, then POST.

    2026-08-25 lore: every action POST must carry the per-action revision and
    concurrency_token from the panel GET's ``actions[]`` — tokens are
    action-scoped and stable across reads; the display revision is not.
    """
    panel = panel if panel is not None else get_panel(sid)
    action = next((a for a in panel["actions"] if a["action_id"] == action_id), None)
    if action is None:
        raise RuntimeError(f"action '{action_id}' not presented for {sid}")
    if not action["enabled"] and not allow_disabled:
        blockers = [b.get("headline") or b.get("reason_code") for b in action.get("blockers", [])]
        raise RuntimeError(f"action '{action_id}' disabled for {sid}: {blockers}")
    body = {
        "action_id": action_id,
        "revision": action["revision"],
        "concurrency_token": action["concurrency_token"],
        "idempotency_key": idempotency_key or f"fleet-{uuid.uuid4().hex}",
    }
    if reason is not None:
        body["reason"] = reason
    return request(
        "POST",
        f"/api/brokers/{BROKER}/accounts/{ACCOUNT_ID}/bots/{sid}/actions",
        body,
    )


def deploy(
    sid: str,
    strategy_key: str,
    symbol: str,
    *,
    evidence_override_reason: str | None = None,
    quantity: int = 1,
) -> tuple[int, Any, float]:
    body: dict[str, Any] = {
        "strategy_instance_id": sid,
        "strategy_key": strategy_key,
        "symbol": symbol,
        "sizing": {"preset": "safe_canary", "quantity": quantity},
        "execution_mode": "paper",
        "carryover_policy": "FORBID",
        "parameters": {},
    }
    if evidence_override_reason is not None:
        body["evidence_override"] = {
            "acknowledgement": "I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK",
            "reason": evidence_override_reason,
        }
    return request(
        "POST", f"/api/brokers/{BROKER}/accounts/{ACCOUNT_ID}/bots", body
    )


def jsonl_append(path: str | Path, record: dict[str, Any]) -> None:
    record.setdefault("ts_ms", int(time.time() * 1000))
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def emit(record: dict[str, Any]) -> None:
    """Write one machine-readable result line to the tools' stdout contract.

    These are host CLIs whose stdout *is* the result -- the operator pipes it
    into the campaign JSONL. Diagnostics and progress go to ``logger``; only
    results come through here. Routing every result line through this one
    seam keeps the contract single-sourced and greppable instead of
    scattering writes across six tools.
    """
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def emit_verdict(text: str) -> None:
    """Write one human-readable verdict line to the same result stream."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
