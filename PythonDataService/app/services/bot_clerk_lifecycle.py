"""One boundary between process-owned bot tasks and Clerk-owned run custody."""

from __future__ import annotations

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.services.bot_binding_repository import BrokerBotBinding


class ActiveClerkUnavailableError(RuntimeError):
    """A trade run cannot cross a lifecycle boundary without its Clerk."""


def _requires_clerk(binding: BrokerBotBinding) -> bool:
    return binding.broker == "alpaca" and binding.mode == "trade"


async def register_order_capable_run(binding: BrokerBotBinding) -> None:
    """Persist Clerk strategy/run identity before a trade task can exist."""
    if not _requires_clerk(binding):
        return
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise ActiveClerkUnavailableError("The Alpaca Clerk is not installed.")
    await clerk.register_strategy_run(binding)


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
    "commit_stop_before_task_cancel",
    "register_order_capable_run",
]
