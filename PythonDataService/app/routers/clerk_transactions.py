"""Read-only, bounded Clerk-native transaction history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.broker.alpaca.clerk.sqlite.repository_external_order_api import (
    ExternalOrderNotFoundError,
)
from app.schemas.clerk_transaction_projection import (
    ClerkCustodyWindowSummary,
    ClerkTransactionHistoryResponse,
    ClerkTransactionRow,
    ExternalOrderAcknowledgementRequest,
    ExternalOrderAcknowledgementResponse,
    TransactionOrigin,
)
from app.services.clerk_transaction_projection import (
    ClerkTransactionProjectionStore,
    ClerkTransactionProjectionUnavailable,
    transaction_detail,
    transaction_history,
)
from app.services.clerk_transaction_projection_store import PostgresClerkTransactionProjectionStore
from app.services.sqlite_clerk_transaction_projection import (
    sqlite_acknowledge_external_order,
    sqlite_transaction_detail,
    sqlite_transaction_history,
)

router = APIRouter(prefix="/api/accounts", tags=["clerk-transactions"])


def get_clerk_transaction_store() -> ClerkTransactionProjectionStore:
    return PostgresClerkTransactionProjectionStore()


@router.post(
    "/{account_id}/transactions/external-orders/{external_order_id}/acknowledge",
    response_model=ExternalOrderAcknowledgementResponse,
)
async def acknowledge_external_order_endpoint(
    account_id: str,
    external_order_id: str = Path(min_length=1, max_length=256),
    request: ExternalOrderAcknowledgementRequest = ...,
) -> ExternalOrderAcknowledgementResponse:
    """Durably record operator review of one external broker-order observation."""
    try:
        acknowledged = await asyncio.to_thread(
            sqlite_acknowledge_external_order,
            account_id=account_id,
            external_order_id=external_order_id,
            operator=request.operator,
        )
    except ExternalOrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="External order observation was not found for this account.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if acknowledged is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SQLite Clerk authority is not active for this account.",
        )
    return ExternalOrderAcknowledgementResponse.from_external_order(acknowledged)


@router.get("/{account_id}/transactions", response_model=ClerkTransactionHistoryResponse)
async def get_clerk_transaction_history(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    origin: TransactionOrigin | None = Query(default=None),
    lifecycle_state: str | None = Query(default=None, min_length=1, max_length=64),
    strategy_instance_id: str | None = Query(default=None, min_length=1, max_length=128),
    run_id: str | None = Query(default=None, min_length=1, max_length=128),
    store: ClerkTransactionProjectionStore = Depends(get_clerk_transaction_store),
) -> ClerkTransactionHistoryResponse:
    """Read one indexed keyset page without broker, Account Truth, or journal I/O."""

    try:
        sqlite_page = await asyncio.to_thread(
            sqlite_transaction_history,
            account_id=account_id,
            limit=limit,
            cursor=cursor,
            origin=origin,
            lifecycle_state=lifecycle_state,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
        )
        if sqlite_page is not None:
            return sqlite_page
        return await transaction_history(
            account_id=account_id,
            limit=limit,
            cursor=cursor,
            origin=origin,
            lifecycle_state=lifecycle_state,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            store=store,
        )
    except ClerkTransactionProjectionUnavailable:
        # Availability is a backend-authored UI state, not a browser-side
        # inference from an HTTP failure. The canonical Clerk evidence remains
        # durable even while this downstream read model is unavailable.
        return ClerkTransactionHistoryResponse(
            projection_available=False,
            canonical_fallback_required=True,
            feed_state="projection_unavailable",
            feed_headline="Projection unavailable",
            feed_detail="Clerk transaction projection unavailable; canonical acknowledgement remains durable.",
            high_water_journal_seq=None,
            lag_records=None,
            lag_is_lower_bound=False,
            custody_summary=ClerkCustodyWindowSummary(
                record_count=0,
                a0_custody_accepted_count=0,
                a1_broker_write_started_count=0,
                a2_broker_known_count=0,
                a3_economic_terminal_count=0,
                uncertain_count=0,
            ),
            rows=[],
            next_cursor=None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get(
    "/{account_id}/transactions/{transaction_id}",
    response_model=ClerkTransactionRow,
)
async def get_clerk_transaction_detail(
    account_id: str,
    transaction_id: str,
    store: ClerkTransactionProjectionStore = Depends(get_clerk_transaction_store),
) -> ClerkTransactionRow:
    """Read exactly one selected projected receipt; never rescan Clerk or IBKR."""

    try:
        sqlite_active, sqlite_row = await asyncio.to_thread(
            sqlite_transaction_detail,
            account_id=account_id,
            transaction_id=transaction_id,
        )
        if sqlite_active:
            if sqlite_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="SQLite Clerk operation was not found for this account.",
                )
            return sqlite_row
        row = await transaction_detail(
            account_id=account_id, transaction_id=transaction_id, store=store
        )
    except ClerkTransactionProjectionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clerk transaction projection unavailable.",
        ) from exc
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projected transaction was not found for this account.",
        )
    return row
