"""Narrow coordinator aliases for unscoped broker reads during migration.

The browser's fleet ingress targets the coordinator, while the normal broker
routers intentionally live only on clerk processes.  These two legacy reads
remain available for the bounded compatibility window without mounting the
broader broker, panel, or mutation routers on the coordinator.  Panel-profile
delegates to its canonical read implementation; no browser request is
forwarded to an agent and no clerk endpoint becomes publicly reachable.

Live-verdict does not delegate (#2140).  This router mounts only under
``FLEET_ROLE=fleet_coordinator`` (never ``combined``, see ``app/main.py``),
and a standalone coordinator installs no clerk runtime of its own --
``app/main.py``'s ``set_active_clerk_runtime`` runs only on a lane.  Calling
``brokers.get_live_verdict`` from here used to read that permanently-empty
runtime and report it as the installation's verdict: ``configured_mode:
"unconfigured"`` at HTTP 200 while a real-money lane was up and answering
correctly on its own process.  There is no topology where this route could
ever answer correctly, so it always refuses, retired exactly like the
compatibility-retirement machinery retires any other unscoped read once its
lane-scoped replacement exists (``compatibility_route_family`` in
``app/broker/fleet/lane_runtime.py`` already classifies ``live-verdict``
under ``brokers_lane_extras``) -- except here the refusal is unconditional
rather than evidence-gated, because unlike a lane-serving process this route
never had a legitimate answer to measure in the first place.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from app.broker.fleet.errors import flat_refusal_body
from app.schemas.broker_v2_panel import PanelProfile
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for

router = APIRouter(prefix="/api/brokers", tags=["fleet-compatibility-reads"])

#: Same reason code ``lane_runtime.py``'s ``FleetLaneRuntimeMiddleware`` mints
#: for a retired compatibility-read alias (#2067's refusal vocabulary pins it
#: at 410) -- this route reuses the vocabulary, not a second one, while
#: naming the lane-scoped replacement this specific alias has now that A1
#: exists, which the generic middleware's raw-ASGI refusal cannot do.
_LIVE_VERDICT_RETIRED_NEXT_STEP = (
    "List this broker's lanes at GET /api/brokers/{broker}/clerks, then read "
    "GET /api/brokers/{broker}/clerks/{clerk_id}/live-verdict for the lane "
    "you want."
)


@router.get(
    "/{broker}/live-verdict",
    status_code=410,
    summary="Legacy broker verdict compatibility read",
)
async def get_legacy_live_verdict(broker: str) -> Response:
    """Refuse: a standalone coordinator has no clerk runtime to answer from."""
    del broker
    return JSONResponse(
        status_code=410,
        content=flat_refusal_body(
            "compatibility_read_retired",
            "This compatibility read has retired; use its canonical broker and clerk route.",
            next_step=_LIVE_VERDICT_RETIRED_NEXT_STEP,
        ),
    )


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
