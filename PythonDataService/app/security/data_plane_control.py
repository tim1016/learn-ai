"""Route-owned guard for broker-control data-plane routes: mutating routes and
sensitive read-only observability routes alike.

Two caller families share these routes (delivery B, composed auth):

- the browser/operator caller presents the local shared control secret;
- the fleet coordinator forwards routed operations over the internal lane and
  presents the per-clerk coordinator service token instead, proven by
  ``app.broker.fleet.delivery.lane_forward_is_authorized`` — this module
  consumes that proof, it does not define it.

The browser secret terminates at the coordinator (ADR 0062 addendum): a
forwarded request never carries it, and an internal request never needs it.
"""

from __future__ import annotations

import hmac

from fastapi import Header, Request

from app.broker.fleet.delivery import lane_forward_is_authorized
from app.broker.fleet.errors import (
    DataPlaneControlSecretRefused,
    FleetControlPlaneNotInstalled,
)
from app.config import settings

CONTROL_SECRET_ENV_VAR = "DATA_PLANE_CONTROL_SECRET"
CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR = "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL"
CONTROL_SECRET_HEADER = "X-Data-Plane-Control-Secret"
RETIRED_DATA_PLANE_CONTROL_SECRET = "local-dev-control-secret"
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def require_data_plane_control_secret(
    request: Request,
    supplied: str | None = Header(default=None, alias=CONTROL_SECRET_HEADER),
) -> None:
    """Require the local shared secret for mutating data-plane control routes."""

    if request.method.upper() not in UNSAFE_HTTP_METHODS:
        return
    if lane_forward_is_authorized(request.headers):
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

    if lane_forward_is_authorized(request.headers):
        return
    _require_configured_control_secret(
        supplied=supplied,
        missing_detail=f"{CONTROL_SECRET_ENV_VAR} is required for protected data-plane reads",
    )


def _require_configured_control_secret(*, supplied: str | None, missing_detail: str) -> None:
    expected = settings.DATA_PLANE_CONTROL_SECRET.strip()
    if expected == RETIRED_DATA_PLANE_CONTROL_SECRET:
        raise FleetControlPlaneNotInstalled(
            f"{CONTROL_SECRET_ENV_VAR} uses a retired public value and must be rotated",
            next_step=f"Set {CONTROL_SECRET_ENV_VAR} to a freshly generated secret; "
            "the retired default is never valid in any deployment.",
        )
    if not expected:
        if settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL:
            return
        raise FleetControlPlaneNotInstalled(
            missing_detail,
            next_step=f"Configure {CONTROL_SECRET_ENV_VAR} (or explicitly opt into "
            f"{CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR} for local development) before "
            "calling this route.",
        )

    if not hmac.compare_digest((supplied or "").encode("utf-8"), expected.encode("utf-8")):
        raise DataPlaneControlSecretRefused(
            f"missing or wrong {CONTROL_SECRET_HEADER}",
            next_step=f"Present the {CONTROL_SECRET_HEADER} header with the value "
            "configured for this deployment.",
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
