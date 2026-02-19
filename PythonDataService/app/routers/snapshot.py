"""API endpoints for snapshots (options chain + stock snapshots)"""
from fastapi import APIRouter, HTTPException, status
import logging

from app.services.polygon_client import PolygonClientService
from app.models.requests import (
    OptionsChainSnapshotRequest,
    StockSnapshotRequest,
    StockSnapshotsRequest,
    MarketMoversRequest,
    UnifiedSnapshotRequest,
)
from app.models.responses import (
    OptionsChainSnapshotResponse,
    OptionsContractSnapshotItem,
    UnderlyingSnapshot,
    GreeksSnapshot,
    DaySnapshot,
    StockSnapshotResponse,
    StockSnapshotsResponse,
    StockTickerSnapshot,
    SnapshotBar,
    MinuteBar,
    MarketMoversResponse,
    UnifiedSnapshotResponse,
    UnifiedSnapshotItem,
    UnifiedSnapshotSession,
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


def _build_ticker_snapshot(data: dict) -> StockTickerSnapshot:
    """Convert a raw polygon_client snapshot dict into a StockTickerSnapshot model."""
    day = data.get('day')
    prev_day = data.get('prev_day')
    minute = data.get('min')
    return StockTickerSnapshot(
        ticker=data.get('ticker'),
        day=SnapshotBar(**day) if day else None,
        prev_day=SnapshotBar(**prev_day) if prev_day else None,
        min=MinuteBar(**minute) if minute else None,
        todays_change=data.get('todays_change'),
        todays_change_percent=data.get('todays_change_percent'),
        updated=data.get('updated'),
    )


@router.post("/ticker", response_model=StockSnapshotResponse)
async def get_stock_snapshot(request: StockSnapshotRequest):
    """Fetch a snapshot for a single stock ticker (price, day/prevDay OHLCV, change)."""
    try:
        logger.info(f"[Snapshot] Single ticker request: {request.ticker}")

        result = polygon_client.get_stock_snapshot(request.ticker)
        snapshot = _build_ticker_snapshot(result)

        logger.info(f"[Snapshot] Returning snapshot for {request.ticker}")
        return StockSnapshotResponse(success=True, snapshot=snapshot)

    except Exception as e:
        logger.error(f"[Snapshot] Error fetching ticker snapshot: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stock snapshot: {str(e)}"
        )


@router.post("/market", response_model=StockSnapshotsResponse)
async def get_stock_snapshots(request: StockSnapshotsRequest):
    """Fetch snapshots for multiple stock tickers (or all tickers if none specified)."""
    try:
        ticker_label = ",".join(request.tickers) if request.tickers else "all"
        logger.info(f"[Snapshot] Market snapshot request: {ticker_label}")

        results = polygon_client.get_stock_snapshots(request.tickers)
        snapshots = [_build_ticker_snapshot(r) for r in results]

        logger.info(f"[Snapshot] Returning {len(snapshots)} market snapshots")
        return StockSnapshotsResponse(success=True, snapshots=snapshots, count=len(snapshots))

    except Exception as e:
        logger.error(f"[Snapshot] Error fetching market snapshots: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch market snapshots: {str(e)}"
        )


@router.post("/movers", response_model=MarketMoversResponse)
async def get_market_movers(request: MarketMoversRequest):
    """Fetch top market movers — gainers or losers."""
    try:
        logger.info(f"[Snapshot] Market movers request: {request.direction}")

        results = polygon_client.get_market_movers(request.direction)
        tickers = [_build_ticker_snapshot(r) for r in results]

        logger.info(f"[Snapshot] Returning {len(tickers)} {request.direction}")
        return MarketMoversResponse(success=True, tickers=tickers, count=len(tickers))

    except Exception as e:
        logger.error(f"[Snapshot] Error fetching market movers: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch market movers: {str(e)}"
        )


@router.post("/unified", response_model=UnifiedSnapshotResponse)
async def get_unified_snapshots(request: UnifiedSnapshotRequest):
    """Fetch unified v3 snapshots with flexible ticker filtering."""
    try:
        logger.info(f"[Snapshot] Unified snapshot request: tickers={request.tickers}, limit={request.limit}")

        results = polygon_client.get_unified_snapshots(
            tickers=request.tickers,
            limit=request.limit,
        )

        items = []
        for r in results:
            session_data = r.get('session')
            items.append(UnifiedSnapshotItem(
                ticker=r.get('ticker'),
                type=r.get('type'),
                market_status=r.get('market_status'),
                name=r.get('name'),
                session=UnifiedSnapshotSession(**session_data) if session_data else None,
            ))

        logger.info(f"[Snapshot] Returning {len(items)} unified snapshots")
        return UnifiedSnapshotResponse(success=True, results=items, count=len(items))

    except Exception as e:
        logger.error(f"[Snapshot] Error fetching unified snapshots: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch unified snapshots: {str(e)}"
        )
