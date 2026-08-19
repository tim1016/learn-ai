"""One boundary between process-owned bot tasks and Clerk-owned run custody."""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.active_protocol import (
    ClerkAdmissionSnapshotStaleError,
    RevisionBoundRunRegistrar,
)
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_control_plane import (
    BotControlPlane,
    BotControlPlaneDiscriminator,
)
from app.services.bot_lifecycle_projection import ActiveSqliteAlpacaLifecycleAuthority


class ActiveClerkUnavailableError(RuntimeError):
    """An Alpaca run cannot cross a duty boundary without its Clerk."""


class ClerkAdmissionTokenStaleError(RuntimeError):
    """SQLite Clerk refused activation because the prepared admission went stale."""


class ClerkRunAuthorityChangedError(RuntimeError):
    """The run is no longer SQLite-active at a boot mutation boundary."""


def _requires_duty_authority(binding: BrokerBotBinding) -> bool:
    """All Alpaca modes need duty authority; only trade may execute effects."""

    return binding.broker == "alpaca"


async def register_alpaca_duty_run(
    binding: BrokerBotBinding,
    *,
    admission_snapshot: ClerkCustodySnapshot | None = None,
) -> None:
    """Persist SQLite duty identity before any Alpaca task can exist."""
    if not _requires_duty_authority(binding):
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
    if not _requires_duty_authority(binding):
        return
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise ActiveClerkUnavailableError("The Alpaca Clerk is not installed.")
    await clerk.stop_strategy_run(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        reason=reason,
    )


async def stop_interrupted_alpaca_duty_run(
    artifacts_root: Path,
    *,
    strategy_instance_id: str,
    run_id: str,
) -> None:
    """Revalidate SQLite/plane identity, then stop one boot-orphaned run."""

    authority = ActiveSqliteAlpacaLifecycleAuthority()
    sqlite_claim = (strategy_instance_id, run_id) in authority.recovery_candidates()
    BotControlPlaneDiscriminator(artifacts_root).require(
        strategy_instance_id,
        BotControlPlane.ALPACA_RUNNER,
        sqlite_alpaca_claim=sqlite_claim,
    )
    if not sqlite_claim:
        raise ClerkRunAuthorityChangedError(
            f"Run {run_id!r} is no longer SQLite-active for {strategy_instance_id!r}"
        )
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise ActiveClerkUnavailableError("The Alpaca Clerk is not installed.")
    await clerk.stop_strategy_run(
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        reason="INTERRUPTED_BY_RESTART",
    )


__all__ = [
    "ActiveClerkUnavailableError",
    "ClerkAdmissionTokenStaleError",
    "ClerkRunAuthorityChangedError",
    "commit_stop_before_task_cancel",
    "register_alpaca_duty_run",
    "stop_interrupted_alpaca_duty_run",
]
