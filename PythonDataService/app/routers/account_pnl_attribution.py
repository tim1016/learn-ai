"""Account-scoped SQLite FIFO attribution transport boundary."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, status

from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError
from app.schemas.account_pnl_attribution import AccountPnlAttributionResponse
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable
from app.services.sqlite_account_pnl_attribution import sqlite_account_pnl_attribution

router = APIRouter(prefix="/api/accounts", tags=["account-pnl-attribution"])


@router.get(
    "/{account_id}/pnl-attribution",
    response_model=AccountPnlAttributionResponse,
)
async def get_account_pnl_attribution(
    account_id: str,
    from_ms: int = Query(ge=0),
    to_ms: int = Query(ge=0),
) -> AccountPnlAttributionResponse:
    """Return C2's inclusive account FIFO attribution from SQLite folds."""
    if to_ms < from_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_ms must be greater than or equal to from_ms",
        )
    try:
        projection = await asyncio.to_thread(
            sqlite_account_pnl_attribution,
            account_id=account_id,
            from_ms=from_ms,
            to_ms=to_ms,
        )
    except (ClerkTransactionProjectionUnavailable, EconomicProjectionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SQLite FIFO attribution is unavailable for this account.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if projection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SQLite Clerk authority is not active for this account.",
        )
    return AccountPnlAttributionResponse.from_projection(projection)
