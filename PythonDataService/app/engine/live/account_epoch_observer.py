"""Clerk-owned observation of broker conditions that invalidate account proof."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from app.engine.live.account_artifacts import append_account_event
from app.engine.live.account_clerk_journal import (
    AccountClerkJournalEntry,
    is_economic_terminal_broker_event,
)
from app.engine.live.account_epoch import EpochInvalidationTrigger

logger = logging.getLogger(__name__)


class AccountEpochObserverClerk(Protocol):
    """The narrow Clerk surface the broker-signal observer may use."""

    _account_id: str

    def reconciliation_snapshot(self) -> Awaitable[list[AccountClerkJournalEntry]]: ...

    def observe_epoch_invalidation(self, trigger: EpochInvalidationTrigger) -> Awaitable[None]: ...

    def observe_critical_stream_silence(
        self,
        *,
        now_ms: int,
        has_nonterminal_work: bool,
    ) -> Awaitable[None]: ...


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def supervise_account_epoch_signals(
    *,
    client: object,
    clerk: AccountEpochObserverClerk,
    stop: asyncio.Event,
    signal_poll_s: float = 1.0,
    active_poll_interval_s: float = 30.0,
    clock: Callable[[], int] = now_ms,
) -> None:
    """Convert Clerk-client observations into durable, shadow-only receipts."""

    last_code: int | None = None
    last_connectivity_lost_count = 0
    was_connected = client_connection_state(client)
    last_active_poll_ms = clock()
    code_to_trigger: dict[int, EpochInvalidationTrigger] = {
        1100: "IBKR_1100",
        1101: "IBKR_1101",
        1102: "IBKR_1102",
        504: "SOCKET_LOSS",
    }
    while not stop.is_set():
        code = getattr(client, "last_ibkr_code", None)
        if code != last_code:
            last_code = code if isinstance(code, int) else None
            trigger = code_to_trigger.get(last_code) if last_code is not None else None
            if trigger is not None:
                await clerk.observe_epoch_invalidation(trigger)
                if stop.is_set():
                    return
        connected = client_connection_state(client)
        if was_connected is True and connected is False:
            await clerk.observe_epoch_invalidation("SOCKET_LOSS")
            if stop.is_set():
                return
        if connected is not None:
            was_connected = connected

        lost_count = getattr(client, "connectivity_lost_count", None)
        if isinstance(lost_count, int) and lost_count > last_connectivity_lost_count:
            last_connectivity_lost_count = lost_count
            await clerk.observe_epoch_invalidation("SOCKET_LOSS")
            if stop.is_set():
                return

        current_ms = clock()
        entries = await clerk.reconciliation_snapshot()
        has_nonterminal_work = has_nonterminal_epoch_work(entries)
        await clerk.observe_critical_stream_silence(
            now_ms=current_ms,
            has_nonterminal_work=has_nonterminal_work,
        )
        if current_ms - last_active_poll_ms >= int(active_poll_interval_s * 1_000):
            last_active_poll_ms = current_ms
            if has_nonterminal_work:
                probe = getattr(client, "probe", None)
                if callable(probe):
                    try:
                        await probe(timeout_s=4.0)
                    except Exception:
                        logger.warning(
                            "Clerk active broker poll failed while nonterminal work exists",
                            exc_info=True,
                            extra={"account_id": clerk._account_id},
                        )
                        await clerk.observe_epoch_invalidation("ACTIVE_POLL_FAILED")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=signal_poll_s)


def has_nonterminal_epoch_work(entries: list[AccountClerkJournalEntry]) -> bool:
    """Whether a failed active poll can invalidate live broker proof."""

    by_intent: dict[str, list[AccountClerkJournalEntry]] = {}
    for entry in entries:
        if entry.intent is not None:
            by_intent.setdefault(entry.intent.intent_id, []).append(entry)
    return any(
        any(entry.entry_kind == "recorded" for entry in intent_entries)
        and not any(
            entry.entry_kind == "custody_expired_before_submit"
            or (
                entry.entry_kind == "broker_event"
                and is_economic_terminal_broker_event(entry.broker_event)
            )
            for entry in intent_entries
        )
        for intent_entries in by_intent.values()
    )


def client_connection_state(client: object) -> bool | None:
    """Read hard and soft connection loss from a concrete Clerk client."""

    is_connected = getattr(client, "is_connected", None)
    if not callable(is_connected):
        return None
    return bool(is_connected()) and not bool(getattr(client, "connection_lost", False))


def handle_epoch_signal_supervisor_completion(
    task: asyncio.Task[None],
    *,
    artifacts_root: Path,
    account_id: str,
    stop: asyncio.Event,
    stream_failed: asyncio.Event,
    clock: Callable[[], int] = now_ms,
) -> None:
    """Make observer failure visible and relinquish the Clerk lease."""

    if task.cancelled() or stop.is_set():
        return
    failure = task.exception()
    if failure is None:
        failure = RuntimeError("ACCOUNT_EPOCH_SIGNAL_SUPERVISOR_EXITED")
    logger.exception(
        "account epoch signal supervisor stopped unexpectedly",
        exc_info=failure,
        extra={"account_id": account_id},
    )
    try:
        append_account_event(
            artifacts_root,
            account_id,
            {
                "event_type": "account_epoch_signal_supervisor_unhealthy",
                "ts_ms": clock(),
                "reason": "ACCOUNT_EPOCH_SIGNAL_SUPERVISOR_UNHEALTHY",
                "failure_type": type(failure).__name__,
            },
        )
    except Exception:
        logger.exception(
            "could not record account epoch signal supervisor failure",
            extra={"account_id": account_id},
        )
    stream_failed.set()
    stop.set()


__all__ = [
    "AccountEpochObserverClerk",
    "client_connection_state",
    "handle_epoch_signal_supervisor_completion",
    "has_nonterminal_epoch_work",
    "supervise_account_epoch_signals",
]
