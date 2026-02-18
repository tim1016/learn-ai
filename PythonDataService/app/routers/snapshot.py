"""API endpoints for options chain snapshots"""
from fastapi import APIRouter, HTTPException, status
import logging

from app.services.polygon_client import PolygonClientService
from app.models.requests import OptionsChainSnapshotRequest
from app.models.responses import (
    OptionsChainSnapshotResponse,
    OptionsContractSnapshotItem,
    UnderlyingSnapshot,
    GreeksSnapshot,
    DaySnapshot,
)

router = APIRouter()
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


@router.post("/options-chain", response_model=OptionsChainSnapshotResponse)
async def get_options_chain_snapshot(request: OptionsChainSnapshotRequest):
    """
    Fetch a snapshot of the options chain for an underlying ticker.

    Returns current greeks, IV, open interest, and day OHLCV for each contract,
    plus the underlying asset's current price.

    - **underlying_ticker**: Underlying stock symbol (e.g., AAPL, SPY, GLD)
    """
    try:
        logger.info(f"[Snapshot] Request: underlying={request.underlying_ticker}")

        result = polygon_client.list_snapshot_options_chain(
            underlying_asset=request.underlying_ticker,
            expiration_date=request.expiration_date,
        )

        underlying = UnderlyingSnapshot(**result['underlying'])

        contracts = []
        for c in result['contracts']:
            greeks = GreeksSnapshot(**c['greeks']) if c.get('greeks') else None
            day = DaySnapshot(**c['day']) if c.get('day') else None
            contracts.append(OptionsContractSnapshotItem(
                ticker=c.get('ticker'),
                contract_type=c.get('contract_type'),
                strike_price=c.get('strike_price'),
                expiration_date=c.get('expiration_date'),
                break_even_price=c.get('break_even_price'),
                implied_volatility=c.get('implied_volatility'),
                open_interest=c.get('open_interest'),
                greeks=greeks,
                day=day,
            ))

        logger.info(f"[Snapshot] Returning {len(contracts)} contracts for {request.underlying_ticker}")

        return OptionsChainSnapshotResponse(
            success=True,
            underlying=underlying,
            contracts=contracts,
            count=len(contracts),
        )

    except Exception as e:
        logger.error(f"[Snapshot] Error fetching chain snapshot: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch options chain snapshot: {str(e)}"
        )
