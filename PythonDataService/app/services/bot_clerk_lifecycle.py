"""One boundary between process-owned bot tasks and Clerk-owned run custody."""

from __future__ import annotations

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.active_protocol import (
    ClerkAdmissionSnapshotStaleError,
    RevisionBoundRunRegistrar,
)
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.services.bot_binding_repository import BrokerBotBinding


class ActiveClerkUnavailableError(RuntimeError):
    """A trade run cannot cross a lifecycle boundary without its Clerk."""


class ClerkAdmissionTokenStaleError(RuntimeError):
    """SQLite Clerk refused activation because the prepared admission went stale."""


def _requires_clerk(binding: BrokerBotBinding) -> bool:
    return binding.broker == "alpaca" and binding.mode == "trade"


async def register_order_capable_run(
    binding: BrokerBotBinding,
    *,
    admission_snapshot: ClerkCustodySnapshot | None = None,
) -> None:
    """Persist Clerk strategy/run identity before a trade task can exist."""
    if not _requires_clerk(binding):
        return
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise ActiveClerkUnavailableError("The Alpaca Clerk is not installed.")
    if admission_snapshot is None or not isinstance(clerk, RevisionBoundRunRegistrar):
        await clerk.register_strategy_run(binding)
        return
    try:
        await clerk.register_strategy_run(binding, admission_snapshot=admission_snapshot)
    except ClerkAdmissionSnapshotStaleError as exc:
        raise ClerkAdmissionTokenStaleError(str(exc)) from exc


async def commit_stop_before_task_cancel(
    binding: BrokerBotBinding,
    *,
    reason: str,
) -> None:
    """Persist the active authority's STOP before process cancellation."""
    if not _requires_clerk(binding):
        return
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise ActiveClerkUnavailableError("The Alpaca Clerk is not installed.")
    await clerk.stop_strategy_run(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        reason=reason,
    )


__all__ = [
    "ActiveClerkUnavailableError",
    "ClerkAdmissionTokenStaleError",
    "commit_stop_before_task_cancel",
    "register_order_capable_run",
]
