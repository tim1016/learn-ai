"""Reconnect an account authority whose startup Alpaca did not answer (#2582).

2026-09-29: Alpaca was unreachable for about three seconds while the Live
clerk selected its authority. The selection failed, nothing ever retried it,
and every account read answered 503 until someone restarted the container.

A startup whose only failure is Alpaca not answering yet now ends in the
``BROKER_UNREACHABLE_RECONNECTING`` refusal, and the composition root keeps
serving with it installed while this loop retries on a capped backoff, for as
long as it takes (owner decision 2026-09-29: no give-up). One attempt is the
boot's own steps in the boot's own order -- select, acknowledge, install, boot
recovery -- so an attempt either leaves a booted authority serving or leaves
nothing of itself behind: an installed authority whose boot recovery fails is
retired, and the lane goes back to reconnecting (Alpaca again) or to a final
refusal that says restart (anything else). Every attempt is logged and
counted, never silently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from app.broker.alpaca.clerk.active_runtime import (
    ActiveClerkRuntime,
    compose_failure_refusal,
    terminal_startup_recovery,
    unavailable_runtime,
)

logger = logging.getLogger(__name__)

#: The first wait after the failed boot selection. Doubled after every
#: attempt that still cannot reach Alpaca, up to ``RECONNECT_MAX_DELAY_S``:
#: a blip of seconds recovers within seconds, and an outage of hours costs one
#: account read a minute. A rate limit's own Retry-After is waited out whole.
RECONNECT_FIRST_DELAY_S: Final = 2.0
RECONNECT_MAX_DELAY_S: Final = 60.0
#: A reconnect that ended on an error nothing expected. Final, like every
#: startup failure but Alpaca not answering, and its copy says so.
RECONNECT_FAILED: Final = "CLERK_RECONNECT_FAILED"


@dataclass
class ReconnectCounters:
    """Observable counters for this process's reconnects (surface, never silence).

    - ``attempts`` — attempts run, each a full select-to-boot pass.
    - ``still_unreachable`` — attempts Alpaca still did not answer, in the
      selection or in the boot recovery after it.
    - ``installed`` — attempts that left an authority booted and serving.
    - ``final`` — reconnects that ended in a final refusal.
    """

    attempts: int = 0
    still_unreachable: int = 0
    installed: int = 0
    final: int = 0


#: This process's reconnect counters; one lane runs at most one reconnect.
RECONNECT_COUNTERS = ReconnectCounters()


@dataclass(frozen=True)
class AuthoritySteps:
    """The composition root's own boot steps, which every attempt runs in order.

    ``retire`` undoes ``install`` for an authority whose boot recovery failed:
    it stops what the install started and closes the authority, releasing its
    execution lease. ``boot`` runs boot recovery against whatever was
    installed -- a Clerk-less refusal included, so Start reads a finished
    report whichever way an attempt ends.
    """

    select: Callable[[], Awaitable[ActiveClerkRuntime]]
    acknowledge: Callable[[ActiveClerkRuntime], Awaitable[ActiveClerkRuntime]]
    install: Callable[[ActiveClerkRuntime], None]
    retire: Callable[[ActiveClerkRuntime], Awaitable[None]]
    boot: Callable[[ActiveClerkRuntime], Awaitable[None]]


async def run_authority_reconnect(
    unreachable: ActiveClerkRuntime,
    *,
    steps: AuthoritySteps,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    first_delay_s: float = RECONNECT_FIRST_DELAY_S,
    max_delay_s: float = RECONNECT_MAX_DELAY_S,
) -> ActiveClerkRuntime:
    """Retry the boot's authority until it serves or its failure is final; return what ends it.

    The task never dies silently: an error no step expected is logged and
    installs a final refusal, whose copy stops promising a reconnect that is
    no longer running. It keeps the activation evidence of the refusal the
    attempt started from, so the account's Home line and panels keep naming
    the failed authority, and it is booted like every refusal.
    """
    if first_delay_s <= 0 or max_delay_s < first_delay_s:
        raise ValueError("reconnect backoff must start positive and cap at or above its start")
    runtime = unreachable
    delay_s = first_delay_s
    attempt = 0
    try:
        while (failure := runtime.startup_failure) is not None and runtime.reconnecting:
            attempt += 1
            RECONNECT_COUNTERS.attempts += 1
            wait_s = max(delay_s, (failure.retry_after_ms or 0) / 1000)
            logger.warning(
                "Alpaca did not answer this Clerk's startup; reconnect attempt %d in %.0f s: %s",
                attempt,
                wait_s,
                failure.recovery,
                extra={
                    "action": "clerk_authority_reconnect_scheduled",
                    "attempt": attempt,
                    "delay_s": wait_s,
                    "attempts_total": RECONNECT_COUNTERS.attempts,
                    "account_id": failure.account_id,
                    "reason_code": failure.reason_code,
                },
            )
            await sleep(wait_s)
            runtime = await _attempt(steps)
            if runtime.reconnecting:
                RECONNECT_COUNTERS.still_unreachable += 1
            delay_s = min(delay_s * 2, max_delay_s)
    except Exception as exc:
        RECONNECT_COUNTERS.final += 1
        logger.exception(
            "This Clerk's reconnect ended on an unexpected error; it will not retry "
            "until the Clerk restarts: %s",
            exc,
            extra={"action": "clerk_authority_reconnect_failed", "attempts": attempt, "error": str(exc)},
        )
        # The loop binds ``failure`` before any step runs: never None here,
        # it is the failure of the refusal this attempt started from.
        final = unavailable_runtime(
            RECONNECT_FAILED,
            account_id=failure.account_id,
            recovery=terminal_startup_recovery(f"its reconnect to Alpaca failed ({exc})"),
            activation_detected=failure.activation_detected,
            authority_generation=failure.authority_generation,
            db_identity_token=failure.db_identity_token,
        )
        # Acknowledged like every other refusal an attempt installs (#2620):
        # the binding receipt is where a pending Apply is recorded as refused.
        # A second failure here must not leave the lane serving nothing.
        try:
            acknowledged = await steps.acknowledge(final)
        except Exception:
            logger.exception(
                "Acknowledging this Clerk's final reconnect refusal failed too; it is "
                "installed unacknowledged",
                extra={"action": "clerk_authority_reconnect_final_ack_failed", "account_id": failure.account_id},
            )
            acknowledged = final
        steps.install(acknowledged)
        try:
            await steps.boot(acknowledged)
        except Exception:
            # Raised out of the task, this would surface only when shutdown
            # awaits it, and abort custody's teardown there. Start stays
            # refused either way -- no sweep report means no Start -- and
            # says a restart is needed, since no sweep follows (#2620).
            logger.exception(
                "Boot recovery failed for this Clerk's final refusal too; Start stays refused "
                "until the Clerk restarts",
                extra={"action": "clerk_authority_reconnect_final_boot_failed", "account_id": failure.account_id},
            )
        # Acknowledgement can replace the refusal it was handed; the caller
        # must receive the runtime the lane serves, not the one composed here.
        return acknowledged
    if runtime.clerk is not None:
        RECONNECT_COUNTERS.installed += 1
        logger.info(
            "Alpaca answered; this Clerk's account authority is serving after %d reconnect attempt(s)",
            attempt,
            extra={
                "action": "clerk_authority_reconnected",
                "attempts": attempt,
                "account_id": runtime.selected_account_id,
            },
        )
    else:
        RECONNECT_COUNTERS.final += 1
        failure = runtime.startup_failure
        logger.error(
            "This Clerk stopped reconnecting after %d attempt(s); its startup failure is final: %s",
            attempt,
            None if failure is None else failure.recovery,
            extra={
                "action": "clerk_authority_reconnect_ended",
                "attempts": attempt,
                "account_id": None if failure is None else failure.account_id,
                "reason_code": None if failure is None else failure.reason_code,
            },
        )
    return runtime


async def _attempt(steps: AuthoritySteps) -> ActiveClerkRuntime:
    """One select-to-boot pass; what it returns is what the lane now serves, or still awaits.

    Alpaca not answering the selection installs nothing: the reconnecting
    refusal already installed keeps serving. Anything else is acknowledged,
    installed and boot-recovered. An installed authority whose boot recovery
    fails is retired before its refusal replaces it, so no half-booted
    authority -- sweepless, Start refused for good -- is ever left serving.
    That refusal is the one a failed boot composition installs
    (``compose_failure_refusal``): reconnecting when Alpaca was the cause,
    else final, and naming the activation the retired authority served, read
    before retirement closes its repository.
    """
    selected = await steps.select()
    if selected.reconnecting:
        return selected
    acknowledged = await steps.acknowledge(selected)
    steps.install(acknowledged)
    # A refusal holds no repository, so no lease to release: it is booted as
    # it stands.
    repository = acknowledged.sqlite_repository
    if repository is None:
        await steps.boot(acknowledged)
        return acknowledged
    # The activation identity is read inside the guard too (#2620): a
    # control-meta read that raises is a failed composition of this attempt,
    # not an escapee to the catch-all — which would install the final
    # refusal without retiring the authority it just installed, leaving its
    # lease and consumer open.
    activation = None
    try:
        activation = repository.control_meta_snapshot()
        await steps.boot(acknowledged)
    except Exception as exc:
        refusal = compose_failure_refusal(
            exc,
            account_id=repository.account_id,
            authority_generation=None if activation is None else activation.authority_generation,
            db_identity_token=None if activation is None else activation.db_identity_token,
        )
        logger.error(
            "This Clerk's account authority installed, but its boot recovery failed; it is "
            "retired: %s",
            exc,
            extra={
                "action": "clerk_authority_reconnect_boot_failed",
                "account_id": repository.account_id,
                "error": str(exc),
                "reconnecting": refusal.reconnecting,
                "identity_read": activation is not None,
            },
            exc_info=True,
        )
        steps.install(refusal)
        await steps.retire(acknowledged)
        await steps.boot(refusal)
        return refusal
    return acknowledged


__all__ = [
    "RECONNECT_COUNTERS",
    "RECONNECT_FAILED",
    "RECONNECT_FIRST_DELAY_S",
    "RECONNECT_MAX_DELAY_S",
    "AuthoritySteps",
    "ReconnectCounters",
    "run_authority_reconnect",
]
