"""HTTP client: main service -> host live-run daemon (ADR 0004).

The host daemon owns the subprocesses and is therefore the sole authority for
the live ``strategy_instance_id -> run_id`` binding. The instance-status
endpoint in ``polygon-data-service`` cannot prove liveness from artifacts, so it
queries the daemon here. Every call fails *closed*: an unreachable daemon yields
``None``, and the endpoint renders the instance with no live binding (process
state ``unreachable``) rather than guessing one from disk.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(2.0)
# Deploy runs git + file hashing on the host; allow more headroom than the
# liveness GETs, but still bounded so a wedged daemon surfaces as 503.
_DEPLOY_TIMEOUT = httpx.Timeout(15.0)


class HostDaemonError(Exception):
    """A daemon call that must surface its status to the caller.

    Unlike the liveness GETs (which fail *closed* to ``None``), a deploy POST
    carries an outcome the operator needs: the daemon's HTTP status and detail
    are propagated so the data-plane endpoint can re-raise them verbatim
    (dirty-tree 409, missing-input 400, git 503), and a connection failure maps
    to 503 "daemon unreachable".
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def deploy(base_url: str, payload: dict) -> dict:
    """POST /deploy to the daemon and return the parsed body.

    Raises :class:`HostDaemonError` on any non-2xx response (status + detail
    propagated) or on connection failure (503).
    """
    url = f"{base_url.rstrip('/')}/deploy"
    try:
        async with httpx.AsyncClient(timeout=_DEPLOY_TIMEOUT) as client:
            response = await client.post(url, json=payload)
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


async def fetch_instances(base_url: str) -> dict | None:
    """GET /instances from the daemon. Returns the parsed body or None."""
    return await _get_json(f"{base_url.rstrip('/')}/instances")


async def fetch_instance_process(base_url: str, strategy_instance_id: str) -> dict | None:
    """GET /instances/{id}/process from the daemon. Returns the body or None."""
    return await _get_json(f"{base_url.rstrip('/')}/instances/{strategy_instance_id}/process")


async def fetch_qc_audit_copies(base_url: str) -> dict | None:
    """GET /qc-audit-copies from the daemon. Returns the body or None."""
    return await _get_json(f"{base_url.rstrip('/')}/qc-audit-copies")


async def _get_json(url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Fail closed: the caller treats None as "liveness unknown / unreachable".
        logger.warning("host daemon unreachable at %s: %s", url, exc)
        return None
