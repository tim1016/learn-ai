"""Read-only, bounded Clerk-native transaction history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.schemas.clerk_transaction_projection import (
    ClerkCustodyWindowSummary,
    ClerkTransactionHistoryResponse,
    ClerkTransactionRow,
    TransactionOrigin,
)
from app.services.clerk_transaction_projection import (
    ClerkTransactionProjectionStore,
    ClerkTransactionProjectionUnavailable,
    transaction_detail,
    transaction_history,
)
from app.services.clerk_transaction_projection_store import PostgresClerkTransactionProjectionStore

router = APIRouter(prefix="/api/accounts", tags=["clerk-transactions"])


def get_clerk_transaction_store() -> ClerkTransactionProjectionStore:
    return PostgresClerkTransactionProjectionStore()


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
