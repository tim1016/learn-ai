"""Broker System v2 read router (transport only).

Resolves the ``{broker}`` path segment via the registry, calls the read port,
and translates broker-contract errors into HTTP responses carrying a what/why
detail. No business logic lives here — the router validates/parses, calls a
port, and shapes the response (router-freeze discipline). Phase 1 registers
only ``alpaca``; unknown brokers resolve to ``404``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    MarketMark,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancelConflictError,
    ManualOrderCancelOwnershipError,
    ManualOrderCancelTerminalError,
    ManualTicketCancelError,
)
from app.broker.alpaca.clerk.sqlite.manual_order_runtime import ManualPreviewStaleError
from app.broker.alpaca.clerk.sqlite.manual_orders import (
    ManualTicketConflictError,
    ManualTicketContinuationError,
    ManualTicketLeg,
)
from app.broker.alpaca.clerk.sqlite.projection_errors import ProjectionReadError
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection
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
from app.schemas.clerk_custody import CustodyDiagnosis
from app.schemas.manual_orders import (
    ManualOrderCancellationResponse,
    ManualOrderCancelRequest,
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
logger = logging.getLogger(__name__)

# Matches app/broker/alpaca/client.py's own request timeout. That client
# already turns a stuck request into a BrokerError within this bound, but
# this router should not implicitly trust a transitive dependency to
# enforce it — a defense-in-depth bound so /clerk/status can never block
# UI polling indefinitely regardless of how the account read is wired.
_ACCOUNT_SNAPSHOT_TIMEOUT_S = 15.0

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


def _sqlite_projection_unavailable() -> HTTPException:
    """Build the stable fail-closed response shared by retained broker reads."""
    return HTTPException(
        status_code=503,
        detail={
            "reason": "sqlite_projection_unavailable",
            "message": "The Account Clerk order record could not be read safely.",
            "next_step": ("Keep broker actions blocked and retry after the Clerk projection is repaired."),
        },
    )


async def _read_sqlite_account_projection(
    sqlite: SqliteAlpacaClerkFacade,
) -> ClerkProjection:
    """Map a fail-closed SQLite read to one stable Broker Desk response."""
    try:
        projection = await asyncio.to_thread(
            sqlite_projection,
            account_id=sqlite.account_id,
            strategy_instance_id=None,
        )
    except ProjectionReadError as exc:
        raise _sqlite_projection_unavailable() from exc
    if projection is None:
        raise _sqlite_projection_unavailable()
    return projection


def _require_sqlite_facade(broker: str) -> SqliteAlpacaClerkFacade:
    """Resolve the sole Alpaca custody authority or fail unavailable."""
    if broker != "alpaca":
        _resolve_port(broker)
        raise HTTPException(
            status_code=404,
            detail={
                "broker": broker,
                "message": "Account Clerk custody is not supported for this broker.",
            },
        )
    facade = active_sqlite_facade(broker)
    if facade is not None:
        return facade
    runtime = get_active_clerk_runtime()
    failure = runtime.startup_failure if runtime is not None else None
    raise HTTPException(
        status_code=503,
        detail={
            "broker": broker,
            "reason": (
                failure.reason_code.lower()
                if failure is not None
                else "sqlite_authority_unavailable"
            ),
            "message": (
                failure.recovery
                if failure is not None
                else "No activated SQLite Account Clerk is installed."
            ),
        },
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
    facade = _require_sqlite_facade("alpaca")
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
            position_quantities={position.symbol.upper(): position.quantity for position in positions},
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
            legs=tuple(ManualTicketLeg(leg_id=str(leg.leg_id), instruction=leg.instruction) for leg in request.legs),
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
            legs=tuple(ManualTicketLeg(leg_id=str(leg.leg_id), instruction=leg.instruction) for leg in request.legs),
            preview_token=request.preview_token,
        )
    except ManualPreviewStaleError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_preview_stale", "message": str(exc)},
        ) from exc
    except (DurableConflictError, ManualTicketConflictError, ManualTicketContinuationError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_ticket_conflict", "message": str(exc)},
        ) from exc
    except BrokerError as error:
        _raise_http(error)
    return _manual_ticket_response(facade, ticket)


@router.post(
    "/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}/continue",
    response_model=ManualOrderTicketResponse,
    status_code=202,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def continue_sqlite_manual_order_ticket(
    account_id: str,
    ticket_id: UUID,
    request: ManualOrderSubmitRequest,
) -> ManualOrderTicketResponse:
    """Activate only the next acknowledged ticket leg after a fresh review."""
    facade = _require_sqlite_manual_facade(account_id)
    ticket = str(ticket_id)
    try:
        await facade.submit_manual_order(
            operator_id=settings.PANEL_OPERATOR_IDENTITY,
            ticket_id=ticket,
            legs=tuple(ManualTicketLeg(leg_id=str(leg.leg_id), instruction=leg.instruction) for leg in request.legs),
            preview_token=request.preview_token,
            continuation=True,
        )
    except ManualPreviewStaleError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_preview_stale", "message": str(exc)},
        ) from exc
    except (DurableConflictError, ManualTicketConflictError, ManualTicketContinuationError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_ticket_continuation_refused", "message": str(exc)},
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


@router.post(
    "/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}/cancel",
    response_model=ManualOrderTicketResponse,
    status_code=202,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def cancel_sqlite_manual_ticket(
    account_id: str,
    ticket_id: UUID,
    request: ManualOrderCancelRequest,
) -> ManualOrderTicketResponse:
    """Cancel every verified working order owned by one manual ticket."""
    facade = _require_sqlite_manual_facade(account_id)
    try:
        ticket = await facade.cancel_manual_ticket(
            operator_id=settings.PANEL_OPERATOR_IDENTITY,
            ticket_id=str(ticket_id),
            cancel_request_id=str(request.cancel_request_id),
        )
    except (
        DurableConflictError,
        ManualOrderCancelConflictError,
        ManualOrderCancelOwnershipError,
        ManualOrderCancelTerminalError,
        ManualTicketCancelError,
    ) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_ticket_cancel_refused", "message": str(exc)},
        ) from exc
    return ManualOrderTicketResponse.from_resource(ticket, repo=facade.repository)


@router.post(
    "/alpaca/accounts/{account_id}/manual-orders/{order_ref:path}/cancel",
    response_model=ManualOrderCancellationResponse,
    status_code=202,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def cancel_sqlite_manual_order(
    account_id: str,
    order_ref: str,
    request: ManualOrderCancelRequest,
) -> ManualOrderCancellationResponse:
    """Durably cancel one SQLite-owned manual order by Clerk reference only."""
    facade = _require_sqlite_manual_facade(account_id)
    try:
        submission = await facade.cancel_manual_order(
            operator_id=settings.PANEL_OPERATOR_IDENTITY,
            order_ref=order_ref,
            cancel_request_id=str(request.cancel_request_id),
        )
    except (
        DurableConflictError,
        ManualOrderCancelConflictError,
        ManualOrderCancelOwnershipError,
        ManualOrderCancelTerminalError,
    ) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "manual_order_cancel_refused", "message": str(exc)},
        ) from exc
    return ManualOrderCancellationResponse.from_domain(submission)


@router.get("/{broker}/clerk/status", response_model=ClerkStatus)
async def get_clerk_status(broker: str) -> ClerkStatus:
    """Report the clerk's exposure hold, latest reconciliation, and outstanding intents.

    A protected read (the always-on data-plane secret gates the whole router).
    Transport only: resolve the account-scoped Clerk facade and delegate — the
    Clerk owns the journal-derived hold + verdict + outstanding-intent state,
    plus (#1664) the one canonical account operator posture, authored from
    this same custody projection and a same-request account observation.

    The account read degrades gracefully: a broker-side failure does not 503
    this endpoint (custody state is independently available) — it instead
    surfaces as an explicit ``alpaca_account_evidence_unavailable`` operator
    condition so the account/paper-mode checks fail closed rather than
    silently passing.
    """
    sqlite = _require_sqlite_facade(broker)
    projection = await _read_sqlite_account_projection(sqlite)
    try:
        account = await asyncio.wait_for(
            # Shielded: a timeout here must cancel only this request's wait,
            # never the account-snapshot cache's shared in-flight task —
            # otherwise this request's bound would cut off a read that a
            # concurrent /clerk/status caller is still legitimately waiting on.
            asyncio.shield(resolve_broker_account_snapshot(broker)),
            timeout=_ACCOUNT_SNAPSHOT_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning(
            "Clerk status account read timed out; reporting evidence-unavailable posture",
            extra={"broker": broker, "timeout_s": _ACCOUNT_SNAPSHOT_TIMEOUT_S},
        )
        account = None
    except BrokerError as error:
        logger.warning(
            "Clerk status account read failed; reporting evidence-unavailable posture",
            extra={"broker": broker, "reason": error.message},
        )
        account = None
    return sqlite_clerk_status(
        projection,
        channel_healths=sqlite.channel_health_snapshot(),
        account=account,
    )


@router.get("/{broker}/clerk/custody-diagnosis", response_model=CustodyDiagnosis)
async def get_custody_diagnosis(broker: str) -> CustodyDiagnosis:
    """Diagnose Clerk↔broker custody divergence (read-only).

    Transport only: resolve the account-scoped Clerk and delegate. The Clerk
    reads a fresh broker snapshot and projects the structured, backend-authored
    diagnosis the Accounts page renders verbatim.
    """
    sqlite = _require_sqlite_facade(broker)
    projection = await _read_sqlite_account_projection(sqlite)
    return sqlite_custody_diagnosis(projection)
