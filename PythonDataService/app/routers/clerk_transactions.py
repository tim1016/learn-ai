"""Read-only, bounded Clerk-native transaction history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.schemas.clerk_transaction_projection import ClerkTransactionHistoryResponse
from app.services.clerk_transaction_projection import (
    ClerkTransactionProjectionStore,
    ClerkTransactionProjectionUnavailable,
    PostgresClerkTransactionProjectionStore,
    transaction_history,
)

router = APIRouter(prefix="/api/accounts", tags=["clerk-transactions"])


def get_clerk_transaction_store() -> ClerkTransactionProjectionStore:
    return PostgresClerkTransactionProjectionStore()


@router.get("/{account_id}/transactions", response_model=ClerkTransactionHistoryResponse)
async def get_clerk_transaction_history(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    store: ClerkTransactionProjectionStore = Depends(get_clerk_transaction_store),
) -> ClerkTransactionHistoryResponse:
    """Read one indexed keyset page without broker, Account Truth, or journal I/O."""

    try:
        return await transaction_history(account_id=account_id, limit=limit, cursor=cursor, store=store)
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
            rows=[],
            next_cursor=None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
