"""SQLite authority integration seam for the existing Broker V2 panel."""

from __future__ import annotations

import asyncio

from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    StaleRecoveryTokenError,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotPanelView,
    PanelAction,
    PanelActionRequest,
    PanelActionResult,
)
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    StaleRevisionError,
)
from app.services.sqlite_clerk_compat import (
    active_sqlite_facade,
    sqlite_clerk_status,
    sqlite_projection,
)


class SqlitePanelBotNotFound(ValueError):
    """The process registry has a bot absent from active SQLite custody."""


def sqlite_authority_active(broker: str) -> bool:
    return active_sqlite_facade(broker) is not None


def read_sqlite_roster_statuses(broker: str) -> list[BotStatusView] | None:
    """Read the activated bot roster without consulting runner artifacts.

    SQLite activation makes ``strategy_instances`` and ``runs`` the durable
    roster/lifecycle authority.  The legacy binding tree is disposable process
    evidence and must not be walked by routine catalog refreshes.

    Phase-1 SQLite custody is trade-only and predates persistence of the
    display-only strategy key and quantity. Those fields remain explicitly
    unknown instead of being reconstructed from disposable runner artifacts.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    return [
        _sqlite_roster_status(broker, registration, facade.repository)
        for registration in facade.repository.strategy_instances()
    ]


def _sqlite_roster_status(
    broker: str,
    registration: dict[str, object],
    repository: ClerkSqliteRepository,
) -> BotStatusView:
    strategy_instance_id = str(registration["strategy_instance_id"])
    active_run = repository.active_run(strategy_instance_id)
    retired_at_ms = registration.get("retired_at_ms")
    running = active_run is not None
    return BotStatusView(
        strategy_instance_id=strategy_instance_id,
        strategy_key="unknown",
        broker=broker,
        symbol=str(registration["symbol"]),
        mode="trade",
        quantity=None,
        carryover_policy="FORBID",
        running=running,
        phase=("RETIRED" if retired_at_ms is not None else "ON_DUTY" if running else "OFF_DUTY"),
        desired_state="RUNNING" if running else "STOPPED",
        active_run_id=(str(active_run.lifecycle_run_id) if active_run is not None else None),
        duty_outcome=None,
        binding_created_at_ms=int(registration["created_at_ms"]),
        last_transition_at_ms=(
            int(retired_at_ms)
            if retired_at_ms is not None
            else int(active_run.started_at_ms)
            if active_run is not None
            else None
        ),
    )


async def read_sqlite_clerk_status(broker: str = "alpaca") -> ClerkStatus | None:
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    projection = await asyncio.to_thread(
        sqlite_projection,
        account_id=facade.account_id,
        strategy_instance_id=None,
    )
    if projection is None:
        raise RuntimeError("The active SQLite Clerk projection is unavailable.")
    return sqlite_clerk_status(
        projection,
        channel_healths=facade.channel_health_snapshot(),
    )


async def read_sqlite_bot_projection(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
) -> ClerkProjection | None:
    if not sqlite_authority_active(broker):
        return None
    try:
        return await asyncio.to_thread(
            sqlite_projection,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
        )
    except ValueError as exc:
        raise SqlitePanelBotNotFound(str(exc)) from exc


async def read_sqlite_catalog_projections(
    broker: str,
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, ClerkProjection] | None:
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")

    def read_all() -> dict[str, ClerkProjection]:
        reader = SqliteClerkProjectionReader.from_repository(facade.repository)
        try:
            return {
                strategy_instance_id: projection
                for strategy_instance_id in strategy_instance_ids
                if (
                    projection := reader.bot_snapshot(strategy_instance_id)
                ) is not None
            }
        finally:
            reader.close()

    return await asyncio.to_thread(read_all)


async def execute_sqlite_panel_action(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    request: PanelActionRequest,
    panel: BotPanelView,
    action: PanelAction,
    availability_error: ActionNotAvailableError | None,
) -> PanelActionResult | None:
    """Execute through the same policy that authored the presented action."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if request.revision != panel.revision:
        raise StaleRevisionError(
            "The SQLite Clerk projection changed after this action was presented.",
            detail="Refresh the panel and review the current evidence-bound action.",
        )
    if availability_error is not None:
        raise availability_error
    if request.action_id in {"open_custody_timeline", "prepare_safe_flatten"}:
        raise ActionNotAvailableError(
            "This recovery capability is a view action, not a broker mutation.",
            detail="Use the presented navigation control in the panel.",
        )

    async def current_context():
        def read_context():
            reader = SqliteClerkProjectionReader.from_repository(facade.repository)
            try:
                return reader.recovery_context(
                    strategy_instance_id=strategy_instance_id
                )
            finally:
                reader.close()

        context = await asyncio.to_thread(read_context)
        if context is None:
            raise SqlitePanelBotNotFound(
                f"No SQLite custody projection exists for bot '{strategy_instance_id}'."
            )
        return context

    context = await current_context()
    capability = next(
        (
            candidate
            for candidate in build_recovery_catalog(context)
            if candidate.action_id == request.action_id
        ),
        None,
    )
    try:
        result = await execute_recovery_action(
            facade,
            request=RecoveryExecutionRequest(
                action_id=request.action_id,
                concurrency_token=request.concurrency_token,
                execution_ref=(
                    capability.execution_ref if capability is not None else None
                ),
                reason=request.reason,
            ),
            current_context=current_context,
        )
    except StaleRecoveryTokenError as exc:
        raise StaleRevisionError(
            str(exc),
            detail="Refresh the panel before retrying.",
        ) from exc
    except RecoveryActionUnavailableError as exc:
        raise ActionNotAvailableError(
            str(exc),
            detail=exc.capability.next_step,
        ) from exc
    except RecoveryExecutionError as exc:
        raise ActionNotAvailableError(str(exc)) from exc

    return PanelActionResult(
        action_id=request.action_id,
        receipt_id=result.receipt_id,
        recorded_at_ms=result.recorded_at_ms,
        applied=result.applied,
        revision=panel.revision,
        concurrency_token=action.concurrency_token,
        message=(
            f"{action.label} completed."
            if result.applied
            else f"{action.label} was already durably recorded."
        ),
    )


__all__ = [
    "SqlitePanelBotNotFound",
    "execute_sqlite_panel_action",
    "read_sqlite_bot_projection",
    "read_sqlite_catalog_projections",
    "read_sqlite_clerk_status",
    "read_sqlite_roster_statuses",
    "sqlite_authority_active",
]
