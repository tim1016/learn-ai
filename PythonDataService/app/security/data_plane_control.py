"""Route-owned guard for mutating broker-control data-plane routes.

Two caller families share these routes (delivery B, composed auth):

- the browser/operator caller presents the local shared control secret;
- the fleet coordinator forwards routed operations over the internal lane and
  presents the per-clerk coordinator service token instead.

The browser secret terminates at the coordinator (ADR 0062 addendum): a
forwarded request never carries it, and an internal request never needs it.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from app.broker.fleet.delivery import COORDINATOR_TOKEN_HEADER
from app.config import fleet_settings, settings

CONTROL_SECRET_ENV_VAR = "DATA_PLANE_CONTROL_SECRET"
CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR = "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL"
CONTROL_SECRET_HEADER = "X-Data-Plane-Control-Secret"
RETIRED_DATA_PLANE_CONTROL_SECRET = "local-dev-control-secret"
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _lane_forward_is_authorized(request: Request) -> bool:
    """Whether this request is an authorized fleet-forwarded operation.

    Two conditions, both required: the request presents the coordinator
    token (compared against the env-only value this agent was provisioned
    with, in constant time, never logged) **and** it carries the pinned
    fleet identity a coordinator dispatch always attaches — a token alone
    must never widen into a general-purpose bypass of the control secret on
    routes the forwarding allowlist never names. An absent or wrong token
    returns ``False`` and the ordinary control-secret checks decide.
    """
    supplied = request.headers.get(COORDINATOR_TOKEN_HEADER, "")
    expected = (fleet_settings.COORDINATOR_SERVICE_TOKEN or "").strip()
    if not expected or not supplied:
        return False
    if (
        "x-fleet-clerk-id" not in request.headers
        or "x-fleet-broker" not in request.headers
    ):
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def require_data_plane_control_secret(
    request: Request,
    supplied: str | None = Header(default=None, alias=CONTROL_SECRET_HEADER),
) -> None:
    """Require the local shared secret for mutating data-plane control routes."""

    if request.method.upper() not in UNSAFE_HTTP_METHODS:
        return
    if _lane_forward_is_authorized(request):
        return

    _require_configured_control_secret(
        supplied=supplied,
        missing_detail=f"{CONTROL_SECRET_ENV_VAR} is required for data-plane control mutations",
    )


async def require_data_plane_control_secret_always(
    request: Request,
    supplied: str | None = Header(default=None, alias=CONTROL_SECRET_HEADER),
) -> None:
    """Require the local shared secret for sensitive read-only observability routes."""

    if _lane_forward_is_authorized(request):
        return
    _require_configured_control_secret(
        supplied=supplied,
        missing_detail=f"{CONTROL_SECRET_ENV_VAR} is required for protected data-plane reads",
    )


def _require_configured_control_secret(*, supplied: str | None, missing_detail: str) -> None:
    expected = settings.DATA_PLANE_CONTROL_SECRET.strip()
    if expected == RETIRED_DATA_PLANE_CONTROL_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{CONTROL_SECRET_ENV_VAR} uses a retired public value and must be rotated",
        )
    if not expected:
        if settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL:
            return
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=missing_detail,
        )

    if not hmac.compare_digest((supplied or "").encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"missing or wrong {CONTROL_SECRET_HEADER}",
        )


__all__ = [
    "CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR",
    "CONTROL_SECRET_ENV_VAR",
    "CONTROL_SECRET_HEADER",
    "RETIRED_DATA_PLANE_CONTROL_SECRET",
    "UNSAFE_HTTP_METHODS",
    "require_data_plane_control_secret",
    "require_data_plane_control_secret_always",
]
