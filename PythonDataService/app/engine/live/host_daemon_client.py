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


async def fetch_instances(base_url: str) -> dict | None:
    """GET /instances from the daemon. Returns the parsed body or None."""
    return await _get_json(f"{base_url.rstrip('/')}/instances")


async def fetch_instance_process(base_url: str, strategy_instance_id: str) -> dict | None:
    """GET /instances/{id}/process from the daemon. Returns the body or None."""
    return await _get_json(f"{base_url.rstrip('/')}/instances/{strategy_instance_id}/process")


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
