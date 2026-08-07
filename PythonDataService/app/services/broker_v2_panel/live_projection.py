"""Producer-owned versioned snapshot hubs for the broker V2 bot panel."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app.schemas.broker_v2_panel import BotPanelLiveSnapshot
from app.services.broker_v2_panel import panel_data_source
from app.services.surface_hub import SurfaceHub, SurfaceHubRegistry

_HUBS: SurfaceHubRegistry[BotPanelLiveSnapshot] = SurfaceHubRegistry()
_HUB_RETIREMENT_TASKS: dict[str, asyncio.Task[None]] = {}
_HUB_PROMPT_TASKS: set[asyncio.Task[None]] = set()
_HUB_IDLE_SECONDS = 30.0
logger = logging.getLogger(__name__)


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
        panel, chart = await panel_data_source.get_live_snapshot_parts(
            broker,
            account_id,
            sid,
            resolution=resolution,
        )
        return BotPanelLiveSnapshot(
            stream_epoch="",
            surface_version=0,
            panel=panel,
            live_chart=chart,
        )

    hub = _HUBS.get_or_create(
        key,
        assemble=assemble,
        refresh_interval_seconds=5.0,
    )
    await hub.start()
    release_live_projection_hub(
        broker,
        account_id,
        sid,
        resolution=resolution,
        hub=hub,
    )
    return hub


def retain_live_projection_hub(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
) -> None:
    """Cancel idle retirement while an SSE client owns the hub."""
    key = _hub_key(broker, account_id, sid, resolution)
    task = _HUB_RETIREMENT_TASKS.pop(key, None)
    if task is not None:
        task.cancel()


def release_live_projection_hub(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
    hub: SurfaceHub[BotPanelLiveSnapshot],
) -> None:
    """Retire an unobserved on-demand hub after a short reconnect window."""
    key = _hub_key(broker, account_id, sid, resolution)
    previous = _HUB_RETIREMENT_TASKS.pop(key, None)
    if previous is not None:
        previous.cancel()
    _HUB_RETIREMENT_TASKS[key] = asyncio.create_task(
        _retire_hub_when_idle(key, hub),
        name=f"live-panel-hub-retirement:{key}",
    )


async def _retire_hub_when_idle(
    key: str,
    hub: SurfaceHub[BotPanelLiveSnapshot],
) -> None:
    task = asyncio.current_task()
    try:
        await asyncio.sleep(_HUB_IDLE_SECONDS)
        if _HUBS.get(key) is hub and hub.resource_limits.watcher_count == 0:
            await _HUBS.remove(key)
    except asyncio.CancelledError:
        raise
    finally:
        if _HUB_RETIREMENT_TASKS.get(key) is task:
            _HUB_RETIREMENT_TASKS.pop(key, None)


async def stop_live_projection_hubs() -> None:
    """Drain every on-demand V2 projection producer during app shutdown."""
    prompt_tasks = tuple(_HUB_PROMPT_TASKS)
    for task in prompt_tasks:
        task.cancel()
    if prompt_tasks:
        await asyncio.gather(*prompt_tasks, return_exceptions=True)
    retirement_tasks = tuple(_HUB_RETIREMENT_TASKS.values())
    _HUB_RETIREMENT_TASKS.clear()
    for task in retirement_tasks:
        task.cancel()
    if retirement_tasks:
        await asyncio.gather(*retirement_tasks, return_exceptions=True)
    await _HUBS.stop_all()


def schedule_live_projection_refresh(
    broker: str,
    account_id: str,
    sid: str,
) -> None:
    """Prompt live views without extending a committed action response.

    The action result is already durable before this function is called. Hub
    refresh is presentation work with a five-second producer fallback, so it
    is owned by a tracked background task and drained during service shutdown.
    """
    task = asyncio.create_task(
        refresh_live_projection_hubs(broker, account_id, sid),
        name=f"live-panel-hub-prompt:{broker}:{account_id}:{sid}",
    )
    _HUB_PROMPT_TASKS.add(task)
    task.add_done_callback(_finish_prompt_refresh)


def _finish_prompt_refresh(task: asyncio.Task[None]) -> None:
    _HUB_PROMPT_TASKS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "live panel prompt task failed after a committed control action",
            extra={"exception": repr(error)},
        )


async def refresh_live_projection_hubs(
    broker: str,
    account_id: str,
    sid: str,
) -> None:
    """Prompt every existing bot hub after a committed control mutation.

    No hub is created here.  REST/SSE demand still owns producer lifecycle;
    the normal five-second producer cadence remains the bounded fallback.
    """
    hubs = tuple(
        hub
        for resolution in ("5s", "1m")
        if (hub := _HUBS.get(_hub_key(broker, account_id, sid, resolution)))
        is not None
    )
    if not hubs:
        return
    results = await asyncio.gather(
        *(hub.refresh() for hub in hubs),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(
                "live panel prompt refresh failed after a committed control action",
                extra={
                    "broker": broker,
                    "account_id": account_id,
                    "strategy_instance_id": sid,
                    "exception": repr(result),
                },
            )


def reset_live_projection_hubs_for_testing() -> None:
    """Install an empty registry; callers must first stop any running hubs."""
    global _HUBS
    _HUBS = SurfaceHubRegistry()
    _HUB_RETIREMENT_TASKS.clear()
    _HUB_PROMPT_TASKS.clear()
