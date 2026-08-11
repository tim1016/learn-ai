"""SQLite authority integration seam for the existing Broker V2 panel."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    EconomicSnapshot,
    FillWindowProjection,
    SessionEconomicProjection,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.models import (
    ControlMetaSnapshot,
    DecisionReceiptResource,
)
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
from app.lean_sidecar.trading_calendar import current_trading_session_window
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    PanelAction,
    PanelActionRequest,
    PanelActionResult,
)
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    StaleRevisionError,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    SQLITE_PANEL_LIFECYCLE_ACTION_IDS,
    build_sqlite_catalog,
)
from app.services.sqlite_clerk_compat import (
    active_sqlite_facade,
    sqlite_clerk_status,
    sqlite_projection,
)
from app.utils.timestamps import now_ms_utc


class SqlitePanelBotNotFound(ValueError):
    """The process registry has a bot absent from active SQLite custody."""


_CATALOG_COHERENCE_ATTEMPTS = 2


class SqlitePanelEconomicUnavailable(RuntimeError):
    """SQLite cannot provide one coherent custody and economic panel cut."""


class SqlitePanelDecisionUnavailable(RuntimeError):
    """SQLite decision evidence cannot be rendered as the panel contract."""


@dataclass(frozen=True)
class SqlitePanelEvidence:
    """One revision-bound custody projection and S2 economic bundle."""

    status: BotStatusView
    projection: ClerkProjection
    economics: SessionEconomicProjection


@dataclass(frozen=True)
class SqliteChartEvidence:
    """Revision-bound SQLite bot identity and complete HISTORY fill window."""

    status: BotStatusView
    fills: FillWindowProjection


def sqlite_authority_active(broker: str) -> bool:
    return active_sqlite_facade(broker) is not None


def read_sqlite_roster_statuses(broker: str) -> list[BotStatusView] | None:
    """Read the activated bot roster without consulting runner artifacts.

    SQLite activation makes ``strategy_instances`` and ``runs`` the durable
    roster/lifecycle authority.  The legacy binding tree is disposable process
    evidence and must not be walked by routine catalog refreshes.

    Immutable strategy identity comes from the S1 ``bot_config`` fold.  A
    missing config is an unavailable authority projection, never an
    ``unknown`` identity reconstructed from disposable runner artifacts.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    return [
        _sqlite_roster_status(broker, registration, facade.repository)
        for registration in facade.repository.strategy_instances()
    ]


def read_sqlite_bot_status(
    broker: str,
    strategy_instance_id: str,
) -> BotStatusView | None:
    """Read one activated bot's immutable identity and lifecycle from SQLite."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    registration = facade.repository.strategy_instance(strategy_instance_id)
    if registration is None:
        return None
    return _sqlite_roster_status(broker, registration, facade.repository)


def _sqlite_roster_status(
    broker: str,
    registration: dict[str, object],
    repository: ClerkSqliteRepository,
) -> BotStatusView:
    strategy_instance_id = str(registration["strategy_instance_id"])
    config = repository.bot_config(strategy_instance_id)
    if (
        config is None
        or config.strategy_instance_id != strategy_instance_id
        or not config.strategy_key
        or config.strategy_key == "unknown"
        or not config.display_name
    ):
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' has no immutable SQLite configuration."
        )
    active_run = repository.active_run(strategy_instance_id)
    retired_at_ms = registration.get("retired_at_ms")
    running = active_run is not None
    return BotStatusView(
        strategy_instance_id=strategy_instance_id,
        strategy_key=config.strategy_key,
        strategy_label=config.display_name,
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


def _meta_matches(
    meta: ControlMetaSnapshot,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
) -> bool:
    return (
        meta.account_id == account_id
        and meta.authority_generation == authority_generation
        and meta.control_revision == control_revision
    )


def _bound_bot_status(
    facade: object,
    broker: str,
    strategy_instance_id: str,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
) -> BotStatusView | None:
    """Read one lifecycle/config row between equal authority revision fences."""
    repository = facade.repository
    before = repository.control_meta_snapshot()
    if not _meta_matches(
        before,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite bot identity changed before lifecycle projection."
        )
    registration = repository.strategy_instance(strategy_instance_id)
    status = (
        _sqlite_roster_status(broker, registration, repository)
        if registration is not None
        else None
    )
    after = repository.control_meta_snapshot()
    if not _meta_matches(
        after,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite bot identity changed during lifecycle projection."
        )
    return status


def _bound_roster_statuses(
    facade: object,
    broker: str,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
) -> list[BotStatusView]:
    """Read roster identity/lifecycle rows between equal authority fences."""
    repository = facade.repository
    before = repository.control_meta_snapshot()
    if not _meta_matches(
        before,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite roster identity changed before lifecycle projection."
        )
    statuses = [
        _sqlite_roster_status(broker, registration, repository)
        for registration in repository.strategy_instances()
    ]
    after = repository.control_meta_snapshot()
    if not _meta_matches(
        after,
        account_id=account_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
    ):
        raise SqliteCatalogRevisionMismatch(
            "SQLite roster identity changed during lifecycle projection."
        )
    return statuses


async def read_sqlite_clerk_status(
    broker: str = "alpaca",
    *,
    symbol: str | None = None,
) -> ClerkStatus | None:
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
        channel_healths=facade.channel_health_snapshot(symbol),
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


async def read_sqlite_panel_evidence(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    now_ms: int,
) -> SqlitePanelEvidence | None:
    """Read one panel's custody and S2 economics at the same SQLite revision.

    Separate reader classes use their own read transactions. A writer can
    commit between them, so this seam retries a bounded number of times and
    accepts only identical account, generation, and control-revision values.
    It never fills a mismatch from JSONL or a broker endpoint.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    session_window = current_trading_session_window(now_ms)

    def read_once() -> SqlitePanelEvidence | None:
        for _attempt in range(3):
            custody_reader = SqliteClerkProjectionReader.from_repository(facade.repository)
            economic_reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
            try:
                projection = custody_reader.bot_snapshot(strategy_instance_id)
                economics = economic_reader.bot_session_economic_projection(
                    strategy_instance_id,
                    session_window=session_window,
                )
            finally:
                custody_reader.close()
                economic_reader.close()
            if projection is None or economics is None:
                return None
            snapshot = economics.snapshot
            if (
                projection.account_id == snapshot.account_id
                and projection.authority_generation == snapshot.authority_generation
                and projection.control_revision == snapshot.control_revision
                and projection.strategy_instance_id == snapshot.strategy_instance_id
            ):
                try:
                    status = _bound_bot_status(
                        facade,
                        broker,
                        strategy_instance_id,
                        account_id=snapshot.account_id,
                        authority_generation=snapshot.authority_generation,
                        control_revision=snapshot.control_revision,
                    )
                except SqliteCatalogRevisionMismatch:
                    continue
                if status is None:
                    return None
                return SqlitePanelEvidence(
                    status=status,
                    projection=projection,
                    economics=economics,
                )
        raise SqlitePanelEconomicUnavailable(
            "SQLite custody and execution economics changed during panel projection."
        )

    try:
        return await asyncio.to_thread(read_once)
    except (EconomicProjectionError, SqliteCatalogProjectionUnavailable) as exc:
        raise SqlitePanelEconomicUnavailable(str(exc)) from exc


async def read_sqlite_chart_fills(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    from_ms: int,
    to_ms: int,
) -> FillWindowProjection | None:
    """Read every effective SQLite fill for one bounded HISTORY chart window.

    The S2 reader returns all-or-unavailable, rather than a truncated tail, so
    chart markers remain a complete execution account for their window.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")

    def read_window() -> FillWindowProjection | None:
        reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
        try:
            return reader.bot_fill_window(
                strategy_instance_id,
                from_ms=from_ms,
                to_ms=to_ms,
            )
        finally:
            reader.close()

    try:
        return await asyncio.to_thread(read_window)
    except (EconomicProjectionError, SqliteCatalogProjectionUnavailable) as exc:
        raise SqlitePanelEconomicUnavailable(str(exc)) from exc


async def read_sqlite_chart_evidence(
    broker: str,
    account_id: str,
    strategy_instance_id: str,
    *,
    from_ms: int,
    to_ms: int,
) -> SqliteChartEvidence | None:
    """Read one HISTORY marker window and its identity at one revision fence."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")

    def read_once() -> SqliteChartEvidence | None:
        for _attempt in range(3):
            reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
            try:
                fills = reader.bot_fill_window(
                    strategy_instance_id,
                    from_ms=from_ms,
                    to_ms=to_ms,
                )
            finally:
                reader.close()
            if fills is None:
                return None
            try:
                status = _bound_bot_status(
                    facade,
                    broker,
                    strategy_instance_id,
                    account_id=fills.account_id,
                    authority_generation=fills.authority_generation,
                    control_revision=fills.control_revision,
                )
            except SqliteCatalogRevisionMismatch:
                continue
            if status is None:
                return None
            return SqliteChartEvidence(status=status, fills=fills)
        raise SqlitePanelEconomicUnavailable(
            "SQLite history identity and execution folds changed during projection."
        )

    try:
        return await asyncio.to_thread(read_once)
    except (EconomicProjectionError, SqliteCatalogProjectionUnavailable) as exc:
        raise SqlitePanelEconomicUnavailable(str(exc)) from exc


def read_sqlite_decision_receipts(
    broker: str,
    strategy_instance_id: str,
    *,
    limit: int = 8,
) -> list[DecisionReceipt] | None:
    """Read bounded decision evidence from SQLite, never the legacy JSONL tail."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    try:
        resources = facade.repository.decision_receipt_tail(
            strategy_instance_id=strategy_instance_id,
            limit=limit,
        )
    except (TypeError, ValueError) as exc:
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision evidence is unavailable for bot '{strategy_instance_id}'."
        ) from exc
    return [_decision_receipt_from_resource(resource) for resource in resources]


def _decision_receipt_from_resource(resource: DecisionReceiptResource) -> DecisionReceipt:
    """Adapt a durable S1 receipt to the existing panel evidence view type."""
    try:
        facts = json.loads(resource.facts_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision receipt {resource.seq} has invalid facts JSON."
        ) from exc
    if not isinstance(facts, dict):
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision receipt {resource.seq} facts must be an object."
        )
    try:
        return DecisionReceipt.model_validate(
            {
                "seq": resource.seq,
                "ts_ms": resource.observed_at_ms,
                "bar_ref": facts["bar_ref"],
                "outcome": resource.outcome,
                "reason_code": facts["reason_code"],
                "intent_id": resource.intent_id or "",
                "order_ref": resource.order_ref or "",
                "indicator_snapshot": facts.get("indicator_snapshot", {}),
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SqlitePanelDecisionUnavailable(
            f"SQLite decision receipt {resource.seq} does not satisfy the panel evidence contract."
        ) from exc


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


async def read_sqlite_catalog_economic_rollups(
    broker: str,
    account_id: str,
    strategy_instance_ids: list[str],
) -> dict[str, EconomicSnapshot] | None:
    """Read all requested roster economics in one S2 SQLite revision.

    The S2 reader holds a single SQLite read transaction across every requested
    bot.  The active product path therefore never mixes fill/P&L values from
    JSONL, a process registry, or independently timed per-bot reads.
    """
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    session_window = current_trading_session_window(now_ms_utc())

    def read_all() -> dict[str, EconomicSnapshot]:
        reader = SqliteEconomicProjectionReader.from_repository(facade.repository)
        try:
            return reader.catalog_economic_rollup(
                strategy_instance_ids,
                session_window=session_window,
            )
        finally:
            reader.close()

    try:
        return await asyncio.to_thread(read_all)
    except EconomicProjectionError as exc:
        raise SqliteCatalogProjectionUnavailable(str(exc)) from exc


async def read_sqlite_catalog(
    broker: str,
    account_id: str,
) -> list[BotCatalogView] | None:
    """Build the activated roster from SQLite-only lifecycle and economic folds."""
    facade = active_sqlite_facade(broker)
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    for attempt in range(_CATALOG_COHERENCE_ATTEMPTS):
        candidate_statuses = read_sqlite_roster_statuses(broker)
        if candidate_statuses is None:
            return None
        strategy_instance_ids = [
            status.strategy_instance_id for status in candidate_statuses
        ]
        if not strategy_instance_ids:
            return []
        projections = await read_sqlite_catalog_projections(
            broker,
            account_id,
            strategy_instance_ids,
        )
        economic_rollups = await read_sqlite_catalog_economic_rollups(
            broker,
            account_id,
            strategy_instance_ids,
        )
        if projections is None or economic_rollups is None:
            raise SqliteCatalogProjectionUnavailable(
                "The active SQLite authority became unavailable during catalog projection."
            )
        try:
            first_snapshot = next(iter(economic_rollups.values()), None)
            if first_snapshot is None:
                raise SqliteCatalogRevisionMismatch(
                    "SQLite catalog has no economic revision to bind lifecycle identity."
                )
            statuses = _bound_roster_statuses(
                facade,
                broker,
                account_id=first_snapshot.account_id,
                authority_generation=first_snapshot.authority_generation,
                control_revision=first_snapshot.control_revision,
            )
            if [status.strategy_instance_id for status in statuses] != strategy_instance_ids:
                raise SqliteCatalogRevisionMismatch(
                    "SQLite roster membership changed during catalog projection."
                )
            return build_sqlite_catalog(
                statuses,
                projections,
                economic_rollups=economic_rollups,
                account_id=account_id,
            )
        except SqliteCatalogRevisionMismatch:
            if attempt == _CATALOG_COHERENCE_ATTEMPTS - 1:
                raise
    raise AssertionError("catalog coherence retry exhausted without a result")


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
    if request.action_id in SQLITE_PANEL_LIFECYCLE_ACTION_IDS:
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
    "SqliteChartEvidence",
    "SqlitePanelBotNotFound",
    "SqlitePanelDecisionUnavailable",
    "SqlitePanelEconomicUnavailable",
    "execute_sqlite_panel_action",
    "read_sqlite_bot_projection",
    "read_sqlite_bot_status",
    "read_sqlite_catalog",
    "read_sqlite_catalog_economic_rollups",
    "read_sqlite_catalog_projections",
    "read_sqlite_chart_evidence",
    "read_sqlite_chart_fills",
    "read_sqlite_clerk_status",
    "read_sqlite_decision_receipts",
    "read_sqlite_panel_evidence",
    "read_sqlite_roster_statuses",
    "sqlite_authority_active",
]
