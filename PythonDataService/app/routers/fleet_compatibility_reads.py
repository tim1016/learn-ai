"""Narrow coordinator alias for the unscoped panel-profile read during migration.

The browser's fleet ingress targets the coordinator, while the normal broker
routers intentionally live only on clerk processes.  This legacy read remains
available for the bounded compatibility window without mounting the broader
broker, panel, or mutation routers on the coordinator.  It delegates to its
canonical read implementation; no browser request is forwarded to an agent and
no clerk endpoint becomes publicly reachable.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.broker_v2_panel import PanelProfile
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for

router = APIRouter(prefix="/api/brokers", tags=["fleet-compatibility-reads"])


@router.get("/{broker}/panel-profile", summary="Legacy panel profile compatibility read")
async def get_legacy_panel_profile(broker: str) -> PanelProfile:
    """Delegate the closed profile read without mounting the panel router."""
    profile = panel_profile_for(broker)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Broker '{broker}' has no panel capability profile.",
                "why": "Only Alpaca exposes the broker-v2 panel in phase 1.",
            },
        )
    return profile


__all__ = ["router"]
