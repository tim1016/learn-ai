"""HTTP client: main service -> host live-run daemon (ADR 0004).

The host daemon owns the subprocesses and is therefore the sole authority for
the live ``strategy_instance_id -> run_id`` binding. The instance-status
endpoint in ``polygon-data-service`` cannot prove liveness from artifacts, so it
queries the daemon here.

Read path (PRD #619-C2): every GET returns ``(DaemonResult, dict | None)``.
The ``DaemonResult`` classifies the transport outcome (CONNECTED / UNREACHABLE
/ AUTH_FAILED / PROTOCOL_ERROR / INCOMPATIBLE_CONTRACT); the dict carries the
parsed body iff ``result.kind == "CONNECTED"``. Callers that only need
fail-closed semantics keep checking ``payload is None`` — the typed result is
additive context for log/UX surfacing and for the connectivity monitor
(``probe_daemon_health``) to fold into a running state.

Write path: POST helpers continue to raise ``HostDaemonError`` for now. The
typed mutation classification (619-C5) is a separate refactor that ties
``outcome_ambiguous=True`` to operator-surface ``OUTCOME_UNKNOWN``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import httpx
from pydantic import ValidationError

from app.engine.live.daemon_auth import TOKEN_HEADER, read_daemon_token
from app.engine.live.daemon_transport import DaemonResult
from app.schemas.live_runs import HostRunnerHealth

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(2.0)
# Deploy runs git + file hashing on the host; allow more headroom than the
# liveness GETs, but still bounded so a wedged daemon surfaces as 503.
_DEPLOY_TIMEOUT = httpx.Timeout(15.0)
# Emergency flatten round-trips to the broker synchronously (the daemon caps the
# CLI at 120s); give the HTTP hop a little more so the daemon's own timeout wins.
_FLATTEN_TIMEOUT = httpx.Timeout(130.0)


def _auth_headers() -> dict[str, str]:
    """Attach ``X-Live-Runner-Token`` to every daemon request (ADR 0007).

    Resolves the token from ``LIVE_RUNNER_DAEMON_TOKEN`` env (operator override)
    or, when env is unset, from the daemon's token file shared via the artifacts
    bind mount. If no token is resolvable (daemon not started yet, env unset, no
    file on the mount) we send no header — the daemon then 401s and the caller
    surfaces that as it would any other error (deploy: re-raised; GETs:
    ``DaemonResult.auth_failed`` + ``payload=None``).
    """
    # Lazy import keeps the engine/live client from pulling broker config into
    # module import order and keeps test monkeypatching of settings effective.
    from app.broker.ibkr.config import get_settings

    artifacts_root = Path(get_settings().live_runs_root).parent
    token = read_daemon_token(artifacts_root)
    return {TOKEN_HEADER: token} if token else {}


class HostDaemonError(Exception):
    """A daemon call that must surface its status to the caller.

    Unlike the typed GETs (which fold every transport failure into a
    ``DaemonResult``), a deploy/start/stop/flatten POST carries an outcome the
    operator needs to see verbatim: the daemon's HTTP status and detail are
    propagated so the data-plane endpoint can re-raise them (dirty-tree 409,
    missing-input 400, git 503), and a connection failure maps to 503 "daemon
    unreachable". The typed mutation classification (PRD #619-C5) replaces
    this shape with ``DaemonResult`` for POSTs.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Write path (HostDaemonError — to be typed in 619-C5).
# ---------------------------------------------------------------------------


async def deploy(base_url: str, payload: dict) -> dict:
    """POST /deploy to the daemon and return the parsed body.

    Raises :class:`HostDaemonError` on any non-2xx response (status + detail
    propagated) or on connection failure (503).
    """
    url = f"{base_url.rstrip('/')}/deploy"
    try:
        async with httpx.AsyncClient(timeout=_DEPLOY_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_auth_headers())
    except httpx.HTTPError as exc:
        logger.warning("host daemon unreachable at %s: %s", url, exc)
        raise HostDaemonError(503, f"host daemon unreachable: {exc}") from exc
    if response.status_code >= 400:
        raise HostDaemonError(response.status_code, _detail_of(response))
    try:
        return response.json()
    except ValueError as exc:
        raise HostDaemonError(502, f"host daemon returned a non-JSON body: {exc}") from exc


async def start_run(base_url: str, run_id: str, payload: dict) -> dict:
    """POST /runs/{run_id}/start to the daemon and return the parsed body.

    Mirrors :func:`deploy`: the daemon's auth, precondition, and unreachable
    statuses are propagated via :class:`HostDaemonError` so the data-plane
    endpoint can re-raise them verbatim. Browsers must never hold the daemon's
    shared secret, so the UI routes Start through the data plane (which forwards
    the token from the artifacts bind mount) rather than calling the daemon
    directly (ADR 0007).
    """
    return await _post_action(f"{base_url.rstrip('/')}/runs/{run_id}/start", payload)


async def stop_run(base_url: str, run_id: str, payload: dict) -> dict:
    """POST /runs/{run_id}/stop to the daemon and return the parsed body.

    Same forwarding contract as :func:`start_run`.
    """
    return await _post_action(f"{base_url.rstrip('/')}/runs/{run_id}/stop", payload)


async def emergency_flatten_run(base_url: str, run_id: str, payload: dict) -> dict:
    """POST /runs/{run_id}/emergency-flatten to the daemon.

    Same forwarding contract as :func:`start_run`, but with a longer timeout: the
    daemon runs the one-shot flatten CLI synchronously, which round-trips to the
    broker (connect, fetch positions, place liquidating orders, disconnect).
    """
    return await _post_action(
        f"{base_url.rstrip('/')}/runs/{run_id}/emergency-flatten",
        payload,
        timeout=_FLATTEN_TIMEOUT,
    )


async def _post_action(url: str, payload: dict, *, timeout: httpx.Timeout = _TIMEOUT) -> dict:
    """Shared body for the start/stop/flatten forwards (same contract as :func:`deploy`)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=_auth_headers())
    except httpx.HTTPError as exc:
        logger.warning("host daemon unreachable at %s: %s", url, exc)
        raise HostDaemonError(503, f"host daemon unreachable: {exc}") from exc
    if response.status_code >= 400:
        raise HostDaemonError(response.status_code, _detail_of(response))
    try:
        return response.json()
    except ValueError as exc:
        raise HostDaemonError(502, f"host daemon returned a non-JSON body: {exc}") from exc


def _detail_of(response: httpx.Response) -> str:
    """Extract a human-readable detail from a daemon error response."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"host daemon returned {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return f"host daemon returned {response.status_code}"


# ---------------------------------------------------------------------------
# Read path — typed DaemonResult (PRD #619-C2).
# ---------------------------------------------------------------------------


async def fetch_instances(base_url: str) -> tuple[DaemonResult, dict | None]:
    """GET /instances. Returns ``(DaemonResult, dict | None)``.

    The dict is the parsed body iff ``result.kind == "CONNECTED"``. Existing
    fail-closed callers can keep checking ``payload is None``; the result is
    additive context for typed-failure surfacing.
    """
    return await _typed_get_json(f"{base_url.rstrip('/')}/instances")


async def fetch_instance_process(
    base_url: str, strategy_instance_id: str
) -> tuple[DaemonResult, dict | None]:
    """GET /instances/{id}/process. Returns ``(DaemonResult, dict | None)``."""
    return await _typed_get_json(
        f"{base_url.rstrip('/')}/instances/{strategy_instance_id}/process"
    )


async def fetch_qc_audit_copies(base_url: str) -> tuple[DaemonResult, dict | None]:
    """GET /qc-audit-copies. Returns ``(DaemonResult, dict | None)``."""
    return await _typed_get_json(f"{base_url.rstrip('/')}/qc-audit-copies")


async def fetch_audit_copy_sizing_lookup(
    base_url: str,
    audit_copy_path: str,
    proposed_sizing: dict | None = None,
) -> tuple[DaemonResult, dict | None]:
    """GET /audit-copy-sizing-lookup. Returns ``(DaemonResult, dict | None)``.

    ``proposed_sizing`` is JSON-encoded into the query string.
    """
    import json as _json
    from urllib.parse import quote

    params = f"audit_copy_path={quote(audit_copy_path, safe='/')}"
    if proposed_sizing is not None:
        params += f"&proposed_sizing={quote(_json.dumps(proposed_sizing, sort_keys=True))}"
    return await _typed_get_json(
        f"{base_url.rstrip('/')}/audit-copy-sizing-lookup?{params}"
    )


async def probe_daemon_health(base_url: str) -> DaemonResult:
    """GET /health. Parsed against ``HostRunnerHealth`` for the typed result.

    Used by ``DaemonConnectivityMonitor`` (619-C2). Returns only the
    ``DaemonResult`` — the monitor cares about the connectivity classification
    + the daemon's declared ``daemon_boot_id``, not the full health envelope.

    Parse failures classify via the typed signal:

    - ``pydantic.ValidationError`` (response shape doesn't match the contract)
      → ``DaemonResult.incompatible_contract(...)``
    - ``ValueError`` from JSON decode → ``DaemonResult.malformed_body(...)``
    """
    result, response = await _classify_http(
        f"{base_url.rstrip('/')}/health", method="GET"
    )
    if response is None:
        return result
    try:
        health = HostRunnerHealth.model_validate_json(response.content)
    except ValidationError as exc:
        return DaemonResult.incompatible_contract(
            status=response.status_code, detail=str(exc)
        )
    except ValueError as exc:
        return DaemonResult.malformed_body(
            status=response.status_code, detail=str(exc)
        )
    # ``HostRunnerHealth`` doesn't carry an ``api_version`` field yet; when it
    # does (forward-compat tracking), pass it through here.
    return DaemonResult.connected(
        status=response.status_code,
        daemon_boot_id=health.daemon_boot_id,
        daemon_api_version=None,
    )


async def _typed_get_json(
    url: str, *, timeout: httpx.Timeout = _TIMEOUT
) -> tuple[DaemonResult, dict | None]:
    """Shared body for the typed read path. Returns ``(result, payload)``.

    The payload is a parsed JSON object iff ``result.kind == "CONNECTED"``.
    Non-2xx, transport failures, malformed JSON, and non-object payloads all
    classify deterministically and return ``payload=None``.
    """
    result, response = await _classify_http(url, method="GET", timeout=timeout)
    return _parse_json_body(result, response)


async def _classify_http(
    url: str,
    *,
    method: Literal["GET", "POST"],
    payload: dict | None = None,
    timeout: httpx.Timeout = _TIMEOUT,
) -> tuple[DaemonResult, httpx.Response | None]:
    """Classify the transport outcome of one daemon HTTP exchange.

    Shared chokepoint for ``_typed_get_json`` / ``_typed_post_json`` /
    ``probe_daemon_health``. Returns:

    - ``(connected, response)`` when the daemon answered with 2xx — the
      raw ``httpx.Response`` is passed back so the caller can parse the
      body (untyped JSON, ``HostRunnerHealth``, …).
    - ``(failure_result, None)`` for every transport / auth / 4xx-5xx
      outcome: the closed-kind ``DaemonResult`` already carries the
      detail and the caller does not need to inspect the response.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                response = await client.get(url, headers=_auth_headers())
            else:
                response = await client.post(
                    url, json=payload, headers=_auth_headers()
                )
    except Exception as exc:
        logger.warning("host daemon unreachable at %s: %s", url, exc)
        return DaemonResult.from_httpx_exception(exc), None

    if response.status_code in (401, 403):
        return (
            DaemonResult.auth_failed(
                status=response.status_code, detail=_detail_of(response)
            ),
            None,
        )
    if response.status_code >= 400:
        return (
            DaemonResult.protocol_error(
                status=response.status_code, detail=_detail_of(response)
            ),
            None,
        )
    # 2xx — body parsing belongs to the caller.
    return DaemonResult.connected(status=response.status_code), response


def _parse_json_body(
    result: DaemonResult, response: httpx.Response | None
) -> tuple[DaemonResult, dict | None]:
    """Parse the 2xx body as an untyped JSON object.

    No-op pass-through when ``response is None`` (transport already
    classified). Otherwise: ``ValueError`` → ``malformed_body``;
    non-dict → ``incompatible_contract``; dict → return as-is.
    """
    if response is None:
        return result, None
    try:
        body = response.json()
    except ValueError as exc:
        return (
            DaemonResult.malformed_body(
                status=response.status_code, detail=str(exc)
            ),
            None,
        )
    if not isinstance(body, dict):
        return (
            DaemonResult.incompatible_contract(
                status=response.status_code,
                detail=f"expected JSON object, got {type(body).__name__}",
            ),
            None,
        )
    return DaemonResult.connected(status=response.status_code), body
