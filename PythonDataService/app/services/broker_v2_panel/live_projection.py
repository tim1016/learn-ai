"""Producer-owned versioned snapshot hubs for the broker V2 bot panel."""

from __future__ import annotations

from typing import Literal

from app.schemas.broker_v2_panel import BotPanelLiveSnapshot
from app.services.broker_v2_panel import panel_data_source
from app.services.surface_hub import SurfaceHub, SurfaceHubRegistry

_HUBS: SurfaceHubRegistry[BotPanelLiveSnapshot] = SurfaceHubRegistry()


def _hub_key(
    broker: str,
    account_id: str,
    sid: str,
    resolution: Literal["5s", "1m"],
) -> str:
    return ":".join((broker, account_id, sid, resolution))


async def get_or_start_live_projection_hub(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
) -> SurfaceHub[BotPanelLiveSnapshot]:
    """Return the one producer for a complete panel/chart state document."""
    key = _hub_key(broker, account_id, sid, resolution)

    async def assemble() -> BotPanelLiveSnapshot:
        panel = await panel_data_source.get_panel(
            broker,
            account_id,
            sid,
        )
        chart = await panel_data_source.get_live_chart(
            broker,
            account_id,
            sid,
            resolution=resolution,
        )
        return BotPanelLiveSnapshot(panel=panel, live_chart=chart)

    hub = _HUBS.get_or_create(
        key,
        assemble=assemble,
        refresh_interval_seconds=5.0,
    )
    await hub.start()
    return hub


async def stop_live_projection_hubs() -> None:
    """Drain every on-demand V2 projection producer during app shutdown."""
    await _HUBS.stop_all()


def reset_live_projection_hubs_for_testing() -> None:
    """Install an empty registry; callers must first stop any running hubs."""
    global _HUBS
    _HUBS = SurfaceHubRegistry()
