"""Controllable virtual clock for historical market replay."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

from app.schemas.offline_replay import OfflineReplaySpeed


class ReplayTickPermission(StrEnum):
    RUN = "run"
    STEP = "step"
    STOP = "stop"


class OfflineReplayClock:
    """Coordinate pause/resume/step/speed without changing market timestamps.

    The clock controls wall-clock pacing only. The engine continues to receive
    the historical bar's canonical exchange timestamp, so replay speed cannot
    leak into strategy decisions, fills, or persisted evidence.
    """

    def __init__(
        self,
        speed: OfflineReplaySpeed,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._speed = speed
        self._sleep = sleep
        self._paused = False
        self._step_budget = 0
        self._stop_requested = False
        self._condition = asyncio.Condition()

    @property
    def speed(self) -> OfflineReplaySpeed:
        return self._speed

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    async def pause(self) -> None:
        async with self._condition:
            self._paused = True
            self._condition.notify_all()

    async def resume(self) -> None:
        async with self._condition:
            self._paused = False
            self._step_budget = 0
            self._condition.notify_all()

    async def step(self) -> None:
        async with self._condition:
            self._paused = True
            self._step_budget += 1
            self._condition.notify_all()

    async def set_speed(self, speed: OfflineReplaySpeed) -> None:
        async with self._condition:
            self._speed = speed
            self._condition.notify_all()

    async def stop(self) -> None:
        async with self._condition:
            self._stop_requested = True
            self._condition.notify_all()

    async def before_tick(self, *, warmup: bool) -> ReplayTickPermission:
        """Wait until the next bar may be emitted.

        Warm-up never sleeps or honors pause: it is an initialization phase,
        not part of the user-visible playback timeline.
        """
        if warmup:
            return ReplayTickPermission.STOP if self._stop_requested else ReplayTickPermission.RUN

        async with self._condition:
            while self._paused and self._step_budget == 0 and not self._stop_requested:
                await self._condition.wait()
            if self._stop_requested:
                return ReplayTickPermission.STOP
            if self._step_budget > 0:
                self._step_budget -= 1
                return ReplayTickPermission.STEP
            return ReplayTickPermission.RUN

    async def pace(self, permission: ReplayTickPermission) -> None:
        """Delay one source minute according to playback speed.

        The wait is condition-aware so pause, stop, and speed changes take
        effect immediately even at 1x, whose nominal interval is 60 seconds.
        A custom sleeper remains available for deterministic unit tests.
        """
        if permission is not ReplayTickPermission.RUN:
            return
        if self._sleep is not asyncio.sleep:
            await self._sleep(60.0 / float(self._speed))
            return

        async with self._condition:
            delay_seconds = 60.0 / float(self._speed)
            try:
                await asyncio.wait_for(self._condition.wait(), timeout=delay_seconds)
            except TimeoutError:
                return
