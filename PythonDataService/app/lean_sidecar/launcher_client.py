"""HTTP client for the LEAN Sidecar launcher service.

The data-plane (``polygon-data-service`` container) reaches the
launcher over HTTP. The launcher is a separate process — by design,
per ``docs/architecture/lean-sidecar-lab.md`` §"Launcher topology" —
so the data plane cannot escalate by exploiting the FastAPI handlers.

This module is the single seam between the data plane and the
launcher. It turns the launcher's structured 400 responses
(``{reason, message}``) into typed Python exceptions so the service
layer can branch on them without parsing free-text.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse

logger = logging.getLogger(__name__)

# Default URL the launcher binds to when run as a host process. The
# data plane reads ``LEAN_LAUNCHER_URL`` to override; the default is
# only useful in dev when both processes share localhost.
DEFAULT_LAUNCHER_URL = "http://127.0.0.1:8090"

# Per-launch HTTP timeout — outer bound on how long the launcher can
# take to *respond*. The launcher's own ``wall_clock_timeout_s``
# bounds how long the LEAN container can run; this is the network
# timeout for the round-trip including queueing + container startup.
# Generous default so the first cold run does not time out.
_LAUNCH_HTTP_TIMEOUT_S = 300.0
_OTHER_HTTP_TIMEOUT_S = 15.0


class LauncherClientError(RuntimeError):
    """Base class for launcher-side failures the data plane should
    surface as a meaningful HTTP error to its own caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LauncherRejected(LauncherClientError):
    """The launcher returned a structured 400 with a stable ``reason``.

    ``reason`` mirrors :class:`app.lean_sidecar.launcher.service.LaunchRejectedError`
    labels (``workspace_not_staged``, ``runner_configuration_error``,
    ``invalid_run_id_or_path``, ``workspace_max_mb_exceeded``).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class LauncherUnreachable(LauncherClientError):
    """Network-level failure reaching the launcher (connect / DNS / timeout)."""


class LauncherProtocolError(LauncherClientError):
    """Launcher returned an unexpected status or body shape."""


def _launcher_url() -> str:
    """The launcher base URL the data plane uses, env-overridable."""
    return os.environ.get("LEAN_LAUNCHER_URL", DEFAULT_LAUNCHER_URL).rstrip("/")


def _auth_headers() -> dict[str, str]:
    """Attach ``X-Launcher-Token`` if the launcher was configured with one.

    The launcher refuses requests without the token when
    ``LEAN_LAUNCHER_TOKEN`` is set in its environment. The data plane
    uses the same env var name; both sides read it independently.
    """
    token = os.environ.get("LEAN_LAUNCHER_TOKEN")
    return {"X-Launcher-Token": token} if token else {}


async def post_launch(request: LaunchRequest) -> LaunchResponse:
    """Send a launch request to the launcher and return its response.

    Raises:
        LauncherRejected: launcher returned 400 with the documented
            ``{reason, message}`` envelope.
        LauncherUnreachable: network failure (connect/timeout/DNS).
        LauncherProtocolError: launcher returned an unexpected status
            or a body that does not parse as ``LaunchResponse``.
    """
    url = f"{_launcher_url()}/launch"
    payload = request.model_dump(mode="json")
    timeout = httpx.Timeout(_LAUNCH_HTTP_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=_auth_headers())
    # ``httpx.TimeoutException`` covers Connect/Read/Write/Pool timeouts;
    # ``httpx.NetworkError`` covers ConnectError + general transport errors.
    # Listing only the leaves (ConnectError, ReadTimeout, ...) misses
    # WriteTimeout and ConnectTimeout — bubble those as unreachable too.
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise LauncherUnreachable(f"launcher at {url} unreachable: {e}") from e

    if response.status_code == 400:
        body = _parse_error_envelope(response)
        raise LauncherRejected(body["reason"], body["message"])
    if response.status_code != 200:
        raise LauncherProtocolError(f"launcher returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        return LaunchResponse.model_validate(response.json())
    except (ValueError, TypeError) as e:
        # ValueError covers json-decode + Pydantic validation; TypeError
        # covers ``Response.model_validate`` receiving a non-mapping.
        # Anything else (httpx internals, MemoryError, etc.) is a real
        # bug and should propagate.
        raise LauncherProtocolError(f"launcher body did not parse as LaunchResponse: {e}") from e


def _parse_error_envelope(response: httpx.Response) -> dict[str, str]:
    """Decode the launcher's 400 ``{detail: {reason, message}}`` shape.

    Falls back to ``{"reason": "unknown", "message": <body>}`` so a
    misbehaving launcher does not crash the data plane's error
    surfacing — operators still see *something* in the response.
    """
    try:
        body = response.json()
        detail = body.get("detail", {})
        if isinstance(detail, dict):
            return {
                "reason": detail.get("reason", "unknown"),
                "message": detail.get("message", str(detail)),
            }
    except (ValueError, TypeError, AttributeError) as e:
        # JSON decode errors / non-dict bodies / .get on a non-mapping
        # are all expected misbehaviour. Log instead of silently
        # swallowing per the repo's "no silent exception handlers"
        # rule.
        logger.warning("launcher returned malformed 400 envelope: %s", e)
    return {"reason": "unknown", "message": response.text[:500]}


def post_extract_metadata_sync(run_id: str, image_digest: str) -> dict[str, str]:
    """Sync HTTP call to the launcher's ``/extract-metadata`` endpoint.

    Sync rather than async because the only caller
    (:func:`app.lean_sidecar.staging.stage_lean_metadata_from_image`) is
    itself sync, invoked from the data plane's pre-launch staging path.
    Using ``httpx.Client`` instead of ``AsyncClient`` keeps the call
    sites unchanged; the brief event-loop block during the metadata
    pull is acceptable because launch itself blocks the event loop for
    much longer than this.

    Raises the same ``LauncherRejected`` / ``LauncherUnreachable`` /
    ``LauncherProtocolError`` exceptions as ``post_launch`` so the
    error contract is consistent across endpoints.
    """
    url = f"{_launcher_url()}/extract-metadata"
    payload = {"run_id": run_id, "image_digest": image_digest}
    # The podman ``create + cp + rm`` round-trip is typically <5s on a
    # hot pull; allow 90s as a generous outer bound matching the worst-
    # case subprocess timeouts inside ``stage_lean_metadata_from_image``
    # (30s create + 30s cp + 15s rm worst case, plus margin).
    timeout = httpx.Timeout(90.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=_auth_headers())
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise LauncherUnreachable(f"launcher at {url} unreachable: {e}") from e

    if response.status_code == 400:
        body = _parse_error_envelope(response)
        raise LauncherRejected(body["reason"], body["message"])
    if response.status_code != 200:
        raise LauncherProtocolError(
            f"launcher /extract-metadata returned HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        return response.json()
    except ValueError as e:
        raise LauncherProtocolError(
            f"launcher /extract-metadata returned a non-JSON body: {e}"
        ) from e


async def get_healthz() -> dict[str, Any]:
    """Read the launcher's ``/healthz``. Used by the data plane's
    own ``/healthz`` to surface launcher reachability."""
    url = f"{_launcher_url()}/healthz"
    timeout = httpx.Timeout(_OTHER_HTTP_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=_auth_headers())
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise LauncherUnreachable(f"launcher at {url} unreachable: {e}") from e
    if response.status_code != 200:
        raise LauncherProtocolError(f"launcher /healthz returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as e:
        raise LauncherProtocolError("launcher /healthz returned a non-JSON body") from e
