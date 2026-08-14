"""Broker System v2 read router (transport only).

Resolves the ``{broker}`` path segment via the registry, calls the read port,
and translates broker-contract errors into HTTP responses carrying a what/why
detail. No business logic lives here — the router validates/parses, calls a
port, and shapes the response (router-freeze discipline). Phase 1 registers
only ``alpaca``; unknown brokers resolve to ``404``.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.broker.alpaca.clerk import (
    AlpacaClerk,
    ClerkStatus,
    CustodyConflictResponse,
    CustodyDiagnosis,
    CustodyResolutionOutcomeUnknownError,
    CustodyResolutionReceipt,
    CustodyResolutionRequest,
    CustodySnapshotChangedError,
    InventoryBaselineRefusedError,
    OrderCancelResult,
    OrderSubmitResult,
    get_alpaca_clerk,
)
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    MarketMark,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.manual_order_runtime import ManualPreviewStaleError
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualTicketConflictError
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.errors import (
    BrokerError,
    BrokerRateLimited,
    BrokerSubmissionHeld,
)
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderGroup,
    BrokerOrderRequest,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.broker.contract.ports import BrokerReadPort
from app.broker.contract.registry import get_broker_registry
from app.config import settings
from app.lean_sidecar.trading_calendar import current_trading_session_window
from app.schemas.account_pnl_attribution import (
    AccountPnlAttributionResponse,
    AccountPnlReconciliationResponse,
    PortfolioHistoryProofResponse,
)
from app.schemas.manual_orders import (
    ManualOrderCapabilityResponse,
    ManualOrderPreviewRequest,
    ManualOrderPreviewResponse,
    ManualOrderSubmitRequest,
    ManualOrderTicketResponse,
)
from app.security.data_plane_control import (
    require_data_plane_control_secret,
    require_data_plane_control_secret_always,
)
from app.services.account_pnl_reconciliation import reconcile_broker_curve_to_local_pnl
from app.services.broker_account_snapshot import resolve_broker_account_snapshot
from app.services.broker_order_groups import group_orders_by_symbol
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable
from app.services.sqlite_account_pnl_attribution import sqlite_account_pnl_attribution
from app.services.sqlite_clerk_compat import (
    active_sqlite_facade,
    sqlite_clerk_status,
    sqlite_custody_diagnosis,
    sqlite_projection,
)
from app.utils.timestamps import now_ms_utc

router = APIRouter(prefix="/api/brokers", tags=["brokers-v2"])

_DEFAULT_READ_LIMIT = 100
_MAX_READ_LIMIT = 500
_MAX_ACTIVITY_LIMIT = 100
_MAX_INT64_MS = 2**63 - 1


def _raise_http(error: BrokerError) -> NoReturn:
    """Translate a broker-contract error into an HTTPException (what/why)."""
    headers: dict[str, str] | None = None
    if isinstance(error, BrokerRateLimited) and error.retry_after_ms is not None:
        headers = {"Retry-After": str(max(1, math.ceil(error.retry_after_ms / 1000)))}
    detail: dict[str, str | None] = {
        "broker": error.broker,
        "message": error.message,
        "why": error.detail,
    }
    # A held submit (S6) carries a code-like ``reason_code`` the UI flags and
    # renders through ``receiptLabel`` — surface it so the desk can offer the
    # clear-hold exit rather than a generic 409.
    if isinstance(error, BrokerSubmissionHeld):
        detail["reason_code"] = error.reason_code
    raise HTTPException(
        status_code=error.http_status,
        detail=detail,
        headers=headers,
    )


def _resolve_port(broker: str) -> BrokerReadPort:
    try:
        return get_broker_registry().resolve(broker)
    except BrokerError as error:
        _raise_http(error)


async def _run[T](broker: str, call: Callable[[BrokerReadPort], Awaitable[T]]) -> T:
    """Resolve the port, run one read call, and translate contract errors."""
    port = _resolve_port(broker)
    try:
        return await call(port)
    except BrokerError as error:
        _raise_http(error)


def _require_sqlite_manual_facade(account_id: str) -> SqliteAlpacaClerkFacade:
    """Resolve the only authority permitted to mutate a manual ticket."""
    facade = active_sqlite_facade("alpaca")
    if facade is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "sqlite_authority_inactive",
                "message": "Manual order tickets require the active SQLite Account Clerk.",
            },
        )
    if facade.account_id != account_id:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "sqlite_account_not_selected",
                "message": "This account is not the active SQLite Account Clerk authority.",
            },
        )
    return facade


def _manual_ticket_response(
    facade: SqliteAlpacaClerkFacade,
    ticket_id: str,
) -> ManualOrderTicketResponse:
    ticket = facade.manual_order_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "manual_ticket_not_found",
                "message": "No durable manual order ticket has this identity.",
            },
        )
    return ManualOrderTicketResponse.from_resource(ticket, repo=facade.repository)


@router.get("/{broker}/account", response_model=BrokerAccountSnapshot)
async def get_account(broker: str) -> BrokerAccountSnapshot:
    try:
        return await resolve_broker_account_snapshot(broker)
    except BrokerError as error:
        _raise_http(error)


@router.get("/{broker}/positions", response_model=list[BrokerPosition])
async def list_positions(broker: str) -> list[BrokerPosition]:
    return await _run(broker, lambda port: port.list_positions())


@router.get("/{broker}/orders", response_model=list[BrokerOrder])
async def list_orders(
    broker: str,
    status: Literal["open", "closed", "all"] | None = None,
    limit: int | None = Query(default=None, ge=1, le=_MAX_READ_LIMIT),
    after_ms: int | None = Query(default=None, ge=0, le=_MAX_INT64_MS),
) -> list[BrokerOrder]:
    return await _run(
        broker,
        lambda port: port.list_orders(status=status, limit=limit, after_ms=after_ms),
    )


@router.get("/{broker}/order-groups", response_model=list[BrokerOrderGroup])
async def list_order_groups(
    broker: str,
    status: Literal["open", "closed", "all"] | None = None,
    limit: int | None = Query(default=None, ge=1, le=_MAX_READ_LIMIT),
    after_ms: int | None = Query(default=None, ge=0, le=_MAX_INT64_MS),
) -> list[BrokerOrderGroup]:
    """Return recent orders grouped by symbol with Python-owned quantity totals."""
    orders = await _run(
        broker,
        lambda port: port.list_orders(status=status, limit=limit, after_ms=after_ms),
    )
    return group_orders_by_symbol(orders)


@router.get("/{broker}/activities", response_model=list[BrokerActivity])
async def list_activities(
    broker: str,
    limit: int = Query(default=_DEFAULT_READ_LIMIT, ge=1, le=_MAX_ACTIVITY_LIMIT),
    after_ms: int | None = Query(default=None, ge=0, le=_MAX_INT64_MS),
    current_session: bool = Query(default=False),
) -> list[BrokerActivity]:
    if current_session and after_ms is not None:
        raise HTTPException(
            status_code=422,
            detail="current_session and after_ms are mutually exclusive",
        )
    if current_session:
        session = current_trading_session_window(now_ms_utc())
        if session is None:
            return []
        after_ms = session.open_ms_utc
    return await _run(
        broker,
        lambda port: port.list_activities(after_ms=after_ms, limit=limit),
    )


@router.get("/{broker}/assets", response_model=list[BrokerAsset])
async def list_assets(
    broker: str,
    status: Literal["active", "inactive"] | None = None,
    limit: int = Query(default=_DEFAULT_READ_LIMIT, ge=1, le=_MAX_READ_LIMIT),
) -> list[BrokerAsset]:
    return await _run(broker, lambda port: port.list_assets(status=status, limit=limit))


@router.get("/{broker}/clock", response_model=BrokerClockEvidence)
async def get_clock_evidence(broker: str) -> BrokerClockEvidence:
    # Vendor evidence only — the canonical calendar module remains the sole
    # authority for scheduled session structure (no authority change).
    return await _run(broker, lambda port: port.get_clock_evidence())


@router.get("/{broker}/portfolio-history", response_model=BrokerPortfolioHistory)
async def get_portfolio_history(
    broker: str,
    history_range: PortfolioHistoryRange = Query(alias="range"),
) -> BrokerPortfolioHistory:
    """Return the broker's authoritative account equity curve for one window."""
    return await _run(broker, lambda port: port.get_portfolio_history(history_range))


@router.get(
    "/{broker}/portfolio-history-proof",
    response_model=PortfolioHistoryProofResponse,
)
async def get_portfolio_history_proof(
    broker: str,
    history_range: PortfolioHistoryRange = Query(alias="range"),
) -> PortfolioHistoryProofResponse:
    """Bundle one C1 history snapshot with independent C2/C3 proof when available."""
    history_result, positions_result = await asyncio.gather(
        _run(broker, lambda port: port.get_portfolio_history(history_range)),
        _run(broker, lambda port: port.list_positions()),
        return_exceptions=True,
    )
    if isinstance(history_result, BaseException):
        raise history_result
    history = history_result
    if isinstance(positions_result, BaseException):
        return _history_without_proof(
            history,
            "Current positions were unavailable, so local FIFO proof could not be built.",
        )
    positions = positions_result
    sqlite = active_sqlite_facade(broker)
    if sqlite is None:
        return _history_without_proof(
            history,
            "SQLite Clerk authority is not active for this broker.",
        )
    if not history.timestamps:
        return _history_without_proof(
            history,
            "Broker portfolio history has no timestamps for the requested range.",
        )
    try:
        attribution = await asyncio.to_thread(
            sqlite_account_pnl_attribution,
            account_id=sqlite.account_id,
            from_ms=history.timestamps[0],
            to_ms=history.timestamps[-1],
            marks={
                position.symbol.upper(): MarketMark(
                    price=position.current_price,
                    observed_at_ms=position.observed_at_ms,
                )
                for position in positions
                if position.current_price is not None
            },
            position_quantities={
                position.symbol.upper(): position.quantity for position in positions
            },
        )
    except (ClerkTransactionProjectionUnavailable, EconomicProjectionError):
        return _history_without_proof(
            history,
            "SQLite FIFO attribution is unavailable for this broker.",
        )
    if attribution is None:
        return _history_without_proof(
            history,
            "SQLite Clerk authority changed while loading portfolio history proof.",
        )
    reconciliation = reconcile_broker_curve_to_local_pnl(history, attribution)
    return PortfolioHistoryProofResponse(
        history=history,
        attribution=AccountPnlAttributionResponse.from_projection(attribution),
        reconciliation=AccountPnlReconciliationResponse.from_result(reconciliation),
    )


def _history_without_proof(
    history: BrokerPortfolioHistory,
    reason: str,
) -> PortfolioHistoryProofResponse:
    """Preserve the C1 chart snapshot when independent proof is unavailable."""
    return PortfolioHistoryProofResponse(
        history=history,
        proof_unavailable_reason=reason,
    )


def _require_trade_clerk(broker: str) -> AlpacaClerk:
    """Resolve the account-scoped Alpaca Clerk, or raise the right HTTP error.

    Shared by the write endpoints (submit + cancel). An unknown broker surfaces
    the read path's ``404``; an unconfigured Clerk surfaces a ``503`` with a
    what/why. Only Alpaca has a trade port in phase 2.
    """
    if broker != "alpaca":
        # Preserve the read-path 404 for an unknown broker, then reject a known
        # read-only broker instead of accidentally dispatching through Alpaca.
        _resolve_port(broker)
        raise HTTPException(
            status_code=404,
            detail={
                "broker": broker,
                "message": "Order management is not supported for this broker.",
                "why": "Only Alpaca has a phase-2 trade port.",
            },
        )
    runtime = get_active_clerk_runtime()
    if runtime is not None and runtime.authority_kind == "sqlite":
        raise HTTPException(
            status_code=409,
            detail={
                "broker": broker,
                "message": "Generic manual order mutation is unavailable under SQLite authority.",
                "why": (
                    "Use a policy-presented SQLite Account Clerk action so the "
                    "order remains inside durable command/effect custody."
                ),
            },
        )
    if runtime is not None and runtime.authority_kind == "unavailable":
        raise HTTPException(
            status_code=503,
            detail={
                "broker": broker,
                "message": "Alpaca order management is unavailable.",
                "why": (
                    runtime.startup_failure.recovery
                    if runtime.startup_failure is not None
                    else "Restore the activated Account Clerk authority."
                ),
            },
        )
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise HTTPException(
            status_code=503,
            detail={
                "broker": broker,
                "message": "Alpaca order management is not configured.",
                "why": "Set Alpaca paper credentials in .env and restart the service.",
            },
        )
    if not isinstance(clerk, AlpacaClerk):
        raise HTTPException(
            status_code=503,
            detail={
                "broker": broker,
                "message": "Alpaca manual order management is unavailable.",
                "why": "The selected Clerk does not expose the legacy manual-order capability.",
            },
        )
    return clerk


@router.post(
    "/{broker}/orders",
    response_model=OrderSubmitResult,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def submit_orders(broker: str, request: BrokerOrderRequest) -> OrderSubmitResult:
    """Submit one or more equity market/limit legs (phase-2 write path).

    Transport only: FastAPI validates the body — an inconsistent leg (a limit
    order with no ``limit_price``, a market order carrying one) is a Pydantic
    ``422`` here, never a ``500`` — this resolves the account-scoped Clerk
    facade, and the Clerk owns identity minting, fail-closed journaling, the
    broker call, and per-leg result shaping. A per-leg broker rejection is a
    *failed* leg in a ``200`` response (the request itself succeeded), never a
    ``500``.
    """
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.submit(request)
    except BrokerError as error:
        _raise_http(error)


@router.get(
    "/alpaca/accounts/{account_id}/manual-orders/capability",
    response_model=ManualOrderCapabilityResponse,
    dependencies=[Depends(require_data_plane_control_secret_always)],
)
async def sqlite_manual_order_capability(account_id: str) -> ManualOrderCapabilityResponse:
    """Return the current policy answer before a browser opens a ticket."""
    facade = _require_sqlite_manual_facade(account_id)
    try:
        capability = await facade.manual_order_capability()
    except BrokerError as error:
        _raise_http(error)
    return ManualOrderCapabilityResponse.from_domain(capability)


@router.post(
    "/alpaca/accounts/{account_id}/manual-orders/preview",
    response_model=ManualOrderPreviewResponse,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def preview_sqlite_manual_order(
    account_id: str,
    request: ManualOrderPreviewRequest,
) -> ManualOrderPreviewResponse:
    """Bind one browser-stable ticket leg to fresh server authority facts."""
    facade = _require_sqlite_manual_facade(account_id)
    try:
        preview = await facade.preview_manual_order(
            operator_id=settings.PANEL_OPERATOR_IDENTITY,
            ticket_id=str(request.ticket_id),
            leg_id=str(request.leg.leg_id),
            leg=request.leg.instruction,
        )
    except BrokerError as error:
        _raise_http(error)
    return ManualOrderPreviewResponse.from_domain(preview)


@router.put(
    "/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}",
    response_model=ManualOrderTicketResponse,
    status_code=202,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def submit_sqlite_manual_order(
    account_id: str,
    ticket_id: UUID,
    request: ManualOrderSubmitRequest,
) -> ManualOrderTicketResponse:
    """Durably accept then submit exactly one previewed manual market-order leg."""
    facade = _require_sqlite_manual_facade(account_id)
    ticket = str(ticket_id)
    try:
        await facade.submit_manual_order(
            operator_id=settings.PANEL_OPERATOR_IDENTITY,
            ticket_id=ticket,
            leg_id=str(request.leg.leg_id),
            leg=request.leg.instruction,
            preview_token=request.preview_token,
        )
    except ManualPreviewStaleError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_preview_stale", "message": str(exc)},
        ) from exc
    except (DurableConflictError, ManualTicketConflictError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_ticket_conflict", "message": str(exc)},
        ) from exc
    except BrokerError as error:
        _raise_http(error)
    return _manual_ticket_response(facade, ticket)


@router.get(
    "/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}",
    response_model=ManualOrderTicketResponse,
    dependencies=[Depends(require_data_plane_control_secret_always)],
)
async def get_sqlite_manual_order_ticket(
    account_id: str,
    ticket_id: UUID,
) -> ManualOrderTicketResponse:
    """Restore a durable manual ticket after refresh or a lost submit response."""
    facade = _require_sqlite_manual_facade(account_id)
    return _manual_ticket_response(facade, str(ticket_id))


@router.delete(
    "/{broker}/orders/{order_id}",
    response_model=OrderCancelResult,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def cancel_order(broker: str, order_id: UUID) -> OrderCancelResult:
    """Cancel one working order by its broker-assigned id (phase-2 S3 write path).

    Transport only: resolve the account-scoped Clerk facade and delegate. The
    Clerk owns ownership resolution, fail-closed journaling, the broker call, and
    result shaping. A non-cancelable order is a *failed* result in a ``200``
    response with a typed what/why (never a ``500``). Cancel is intentionally a
    first-class Clerk path, independent of the submit gate, so a future exposure
    hold (S6) that blocks new submission never blocks reducing exposure.
    """
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.cancel(str(order_id))
    except BrokerError as error:
        _raise_http(error)


class ClearHoldRequest(BaseModel):
    """Operator's clear-hold request body (phase-2 S6).

    ``operator`` attributes the HOLD_CLEARED line (who lifted the hold);
    ``reason`` is the operator's what/why the ledger records. Both are optional —
    a clear with no attribution still lifts the hold, journaled with defaults.
    """

    model_config = ConfigDict(extra="forbid")

    operator: str = Field(default="operator", max_length=64)
    reason: str = Field(
        default="Operator cleared the exposure hold.", min_length=1, max_length=512
    )

    @field_validator("reason")
    @classmethod
    def _reason_is_nonblank(cls, value: str) -> str:
        """Normalize operator input and reject blank audit reasons at the boundary."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


@router.get("/{broker}/clerk/status", response_model=ClerkStatus)
async def get_clerk_status(broker: str) -> ClerkStatus:
    """Report the clerk's exposure hold, latest reconciliation, and outstanding intents.

    A protected read (the always-on data-plane secret gates the whole router).
    Transport only: resolve the account-scoped Clerk facade and delegate — the
    Clerk owns the journal-derived hold + verdict + outstanding-intent state.
    """
    sqlite = active_sqlite_facade(broker)
    if sqlite is not None:
        projection = await asyncio.to_thread(
            sqlite_projection,
            account_id=sqlite.account_id,
            strategy_instance_id=None,
        )
        assert projection is not None
        return sqlite_clerk_status(
            projection,
            channel_healths=sqlite.channel_health_snapshot(),
        )
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.status()
    except BrokerError as error:
        _raise_http(error)


@router.get("/{broker}/clerk/custody-diagnosis", response_model=CustodyDiagnosis)
async def get_custody_diagnosis(broker: str) -> CustodyDiagnosis:
    """Diagnose Clerk↔broker custody divergence (read-only).

    Transport only: resolve the account-scoped Clerk and delegate. The Clerk
    reads a fresh broker snapshot and projects the structured, backend-authored
    diagnosis the Accounts page renders verbatim.
    """
    sqlite = active_sqlite_facade(broker)
    if sqlite is not None:
        projection = await asyncio.to_thread(
            sqlite_projection,
            account_id=sqlite.account_id,
            strategy_instance_id=None,
        )
        assert projection is not None
        return sqlite_custody_diagnosis(projection)
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.custody_diagnosis()
    except BrokerError as error:
        _raise_http(error)


@router.post(
    "/{broker}/clerk/clear-hold",
    response_model=ClerkStatus,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def clear_clerk_hold(broker: str, request: ClearHoldRequest) -> ClerkStatus:
    """Clear the account exposure hold (operator exit); return the updated status.

    A control mutation (the control secret gates it). Transport only: resolve the
    Clerk and delegate. The Clerk journals HOLD_CLEARED (idempotent — a clear
    against no active hold is a benign NO-OP) and returns the post-clear status so
    the desk re-renders in one round-trip.
    """
    _reject_generic_sqlite_recovery(broker, action="clear_hold")
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.clear_hold(operator=request.operator, reason=request.reason)
    except InventoryBaselineRefusedError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except BrokerError as error:
        _raise_http(error)


@router.post(
    "/{broker}/clerk/resolve",
    response_model=CustodyResolutionReceipt,
    responses={409: {"model": CustodyConflictResponse}},
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def resolve_custody(
    broker: str, request: CustodyResolutionRequest
) -> CustodyResolutionReceipt:
    """Resolve Clerk↔broker divergence: run the diagnosed plan, journal the reason.

    A control mutation. The typed token is a UI friction gate; the operator
    identity is injected server-side. A stale snapshot is a 409; a blocked
    prerequisite is a 409 with the blocker's what/why.
    """
    _reject_generic_sqlite_recovery(broker, action="resolve_custody")
    if request.confirmation_token != "RESOLVE":
        raise HTTPException(
            status_code=422,
            detail={"message": "Type RESOLVE to confirm.", "why": "Confirmation token mismatch."},
        )
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.resolve_custody(
            operator=settings.PANEL_OPERATOR_IDENTITY,
            reason=request.reason,
            snapshot_version=request.snapshot_version,
            idempotency_key=request.idempotency_key,
        )
    except CustodySnapshotChangedError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except InventoryBaselineRefusedError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except CustodyResolutionOutcomeUnknownError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except BrokerError as error:
        _raise_http(error)


def _reject_generic_sqlite_recovery(broker: str, *, action: str) -> None:
    if active_sqlite_facade(broker) is None:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "reason": "generic_recovery_action_retired",
            "message": (
                f"{action} is not available under SQLite Clerk authority. "
                "Refresh the Clerk projection and use its typed recovery catalog."
            ),
        },
    )
