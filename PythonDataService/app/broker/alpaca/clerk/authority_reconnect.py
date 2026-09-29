"""Reconnect an account authority whose startup could not reach Alpaca (#2582).

2026-09-29: Alpaca was unreachable for about three seconds while the Live
clerk selected its authority. The selection failed, nothing ever retried it,
and every account read answered 503 until someone restarted the container.

A selection whose only failure is an unreachable Alpaca now ends in the
``BROKER_UNREACHABLE_RECONNECTING`` refusal, and the composition root keeps
serving with it installed while this loop re-runs the same selection on a
bounded backoff. The loop returns the first answer that is not "Alpaca is
unreachable": an installed authority, or a terminal failure whose copy says
it will not retry. Every attempt is logged with its number, never silently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from app.broker.alpaca.clerk.active_runtime import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
    terminal_startup_recovery,
    unavailable_runtime,
)

logger = logging.getLogger(__name__)

#: The first wait after the failed boot selection. Doubled after every
#: attempt that still cannot reach Alpaca, up to ``RECONNECT_MAX_DELAY_S``:
#: a blip of seconds recovers within seconds, and an outage of hours costs one
#: account read a minute.
RECONNECT_FIRST_DELAY_S: Final = 2.0
RECONNECT_MAX_DELAY_S: Final = 60.0
#: A reconnect that ended on an error nothing expected. Final, like every
#: startup failure but the unreachable broker, and its copy says so.
RECONNECT_FAILED: Final = "CLERK_RECONNECT_FAILED"


async def reconnect_authority(
    unreachable: ActiveClerkRuntime,
    *,
    select: Callable[[], Awaitable[ActiveClerkRuntime]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    first_delay_s: float = RECONNECT_FIRST_DELAY_S,
    max_delay_s: float = RECONNECT_MAX_DELAY_S,
) -> ActiveClerkRuntime:
    """Re-select until the answer is no longer an unreachable Alpaca, and return it.

    ``select`` is the composition root's own authority selection, unchanged:
    a reconnect installs exactly what a boot that found Alpaca reachable would
    have. A failed attempt's handles are closed inside the selection, as on
    every failed boot.
    """
    if first_delay_s <= 0 or max_delay_s < first_delay_s:
        raise ValueError("reconnect backoff must start positive and cap at or above its start")
    runtime = unreachable
    delay_s = first_delay_s
    attempt = 0
    while (failure := _reconnecting_failure(runtime)) is not None:
        attempt += 1
        logger.warning(
            "Alpaca was unreachable for this Clerk's account; reconnect attempt %d in %.0f s: %s",
            attempt,
            delay_s,
            failure.recovery,
            extra={
                "action": "clerk_authority_reconnect_scheduled",
                "attempt": attempt,
                "delay_s": delay_s,
                "account_id": failure.account_id,
                "reason_code": failure.reason_code,
            },
        )
        await sleep(delay_s)
        runtime = await select()
        delay_s = min(delay_s * 2, max_delay_s)
    if runtime.startup_failure is None:
        logger.warning(
            "Alpaca answered; this Clerk's account authority installed after %d reconnect attempt(s)",
            attempt,
            extra={
                "action": "clerk_authority_reconnected",
                "attempts": attempt,
                "account_id": runtime.selected_account_id,
            },
        )
    else:
        logger.error(
            "This Clerk stopped reconnecting after %d attempt(s); its startup failure is final: %s",
            attempt,
            runtime.startup_failure.recovery,
            extra={
                "action": "clerk_authority_reconnect_ended",
                "attempts": attempt,
                "account_id": runtime.startup_failure.account_id,
                "reason_code": runtime.startup_failure.reason_code,
            },
        )
    return runtime


async def run_authority_reconnect(
    unreachable: ActiveClerkRuntime,
    *,
    select: Callable[[], Awaitable[ActiveClerkRuntime]],
    acknowledge: Callable[[ActiveClerkRuntime], Awaitable[ActiveClerkRuntime]],
    install: Callable[[ActiveClerkRuntime], None],
    boot: Callable[[ActiveClerkRuntime], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The composition root's reconnect: re-select, then acknowledge, install and boot the answer.

    The callables are the boot's own steps, so a reconnected authority is
    acknowledged, installed and boot-recovered exactly as a booted one. The
    task never dies silently: an unexpected error is logged, and a lane still
    showing "reconnecting" is given a final refusal instead -- its copy must
    stop promising a reconnect that is no longer running.
    """
    installed: ActiveClerkRuntime | None = None
    try:
        installed = await acknowledge(await reconnect_authority(unreachable, select=select, sleep=sleep))
        install(installed)
        if installed.clerk is not None:
            await boot(installed)
    except Exception as exc:
        logger.exception(
            "This Clerk's reconnect ended on an unexpected error; Start stays refused "
            "until the Clerk restarts: %s",
            exc,
            extra={"action": "clerk_authority_reconnect_failed", "error": str(exc)},
        )
        if installed is None:
            failure = unreachable.startup_failure
            install(
                unavailable_runtime(
                    RECONNECT_FAILED,
                    account_id=None if failure is None else failure.account_id,
                    recovery=terminal_startup_recovery(f"its reconnect to Alpaca failed ({exc})"),
                )
            )


def _reconnecting_failure(runtime: ActiveClerkRuntime) -> ClerkStartupFailure | None:
    """The failure a reconnect retries, or ``None`` once there is nothing to retry."""
    return runtime.startup_failure if runtime.reconnecting else None


__all__ = [
    "RECONNECT_FAILED",
    "RECONNECT_FIRST_DELAY_S",
    "RECONNECT_MAX_DELAY_S",
    "reconnect_authority",
    "run_authority_reconnect",
]
