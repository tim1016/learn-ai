"""Coordinator-side qualification-only mode control (issue #2206).

Mirrors ``app.routers.fleet_qualification``'s clerk-side hooks, but on the
opposite role: the recorded history provider
(``app.services.broker_v2_panel.qualification_recorded_history``) lives on
the fleet-coordinator process, so its mode toggle must too. Gated by the
same random Compose ceremony namespace prefix
(``app.routers.fleet_qualification.is_qualification_coordinator_lane``),
this process's own ``fleet_coordinator`` role, and the ceremony's minted
per-run probe secret -- reusing the identical gate shape (role + namespace +
secret) rather than inventing a second one. Absent that gate this router is
never constructed, so there is no route at all for dev or production to
reach, not merely an inert one.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict

from app.routers.fleet_qualification import (
    is_qualification_coordinator_lane,
    require_qualification_secret,
)
from app.services.broker_v2_panel.qualification_recorded_history import (
    RecordedHistoryMode,
    recorded_history_mode,
    set_recorded_history_mode,
)

_PREFIX = "/internal/fleet-qualification-history"


class RecordedHistoryModeRequest(BaseModel):
    """The recorded provider's requested mode (issue #2206).

    ``RecordedHistoryMode`` is the one place the three-mode vocabulary is
    spelled; this field reuses it rather than repeating the literal.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    mode: RecordedHistoryMode


class RecordedHistoryModeResponse(BaseModel):
    """The recorded provider's current mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: RecordedHistoryMode


def qualification_history_router(*, role: str, namespace: str, secret: str) -> APIRouter | None:
    """Build the coordinator-only recorded-history mode-control surface, or ``None``."""
    if not is_qualification_coordinator_lane(role=role, namespace=namespace, secret=secret):
        return None
    router = APIRouter(prefix=_PREFIX, include_in_schema=False)

    @router.get("/mode", response_model=RecordedHistoryModeResponse)
    async def read_mode(
        x_fleet_qualification_secret: str | None = Header(default=None),
    ) -> RecordedHistoryModeResponse:
        require_qualification_secret(x_fleet_qualification_secret, secret)
        return RecordedHistoryModeResponse(mode=recorded_history_mode())

    @router.post("/mode", response_model=RecordedHistoryModeResponse)
    async def write_mode(
        payload: RecordedHistoryModeRequest,
        x_fleet_qualification_secret: str | None = Header(default=None),
    ) -> RecordedHistoryModeResponse:
        require_qualification_secret(x_fleet_qualification_secret, secret)
        set_recorded_history_mode(payload.mode)
        return RecordedHistoryModeResponse(mode=recorded_history_mode())

    return router


def qualification_history_router_from_environment(role: str, namespace: str) -> APIRouter | None:
    """Resolve the gated coordinator router from environment, like its clerk-side sibling."""
    return qualification_history_router(
        role=role,
        namespace=namespace,
        secret=os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", ""),
    )


__all__ = [
    "RecordedHistoryModeRequest",
    "RecordedHistoryModeResponse",
    "qualification_history_router",
    "qualification_history_router_from_environment",
]
