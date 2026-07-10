"""Route-owned guard for mutating broker-control data-plane routes."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from app.config import settings

CONTROL_SECRET_ENV_VAR = "DATA_PLANE_CONTROL_SECRET"
CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR = "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL"
CONTROL_SECRET_HEADER = "X-Data-Plane-Control-Secret"
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def require_data_plane_control_secret(
    request: Request,
    supplied: str | None = Header(default=None, alias=CONTROL_SECRET_HEADER),
) -> None:
    """Require the local shared secret for mutating data-plane control routes."""

    if request.method.upper() not in UNSAFE_HTTP_METHODS:
        return

    _require_configured_control_secret(
        supplied=supplied,
        missing_detail=f"{CONTROL_SECRET_ENV_VAR} is required for data-plane control mutations",
    )


async def require_data_plane_control_secret_always(
    supplied: str | None = Header(default=None, alias=CONTROL_SECRET_HEADER),
) -> None:
    """Require the local shared secret for sensitive read-only observability routes."""

    _require_configured_control_secret(
        supplied=supplied,
        missing_detail=f"{CONTROL_SECRET_ENV_VAR} is required for protected data-plane reads",
    )


def _require_configured_control_secret(*, supplied: str | None, missing_detail: str) -> None:
    expected = settings.DATA_PLANE_CONTROL_SECRET.strip()
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
    "UNSAFE_HTTP_METHODS",
    "require_data_plane_control_secret",
    "require_data_plane_control_secret_always",
]
