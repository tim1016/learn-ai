"""API endpoints for snapshots (options chain + stock snapshots)"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.requests import (
    OptionsChainSnapshotRequest,
    StockSnapshotRequest,
)
from app.models.responses import (
    DaySnapshot,
    GreeksSnapshot,
    LastQuoteSnapshot,
    LastTradeSnapshot,
    MinuteBar,
    OptionsChainSnapshotResponse,
    OptionsContractSnapshotItem,
    SnapshotBar,
    StockSnapshotResponse,
    StockTickerSnapshot,
    UnderlyingSnapshot,
)
from app.services.fred_service import get_risk_free_rate
from app.services.polygon_client import PolygonClientService
from app.services.rate_dividend_service import get_rate_and_dividend

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

        underlying = UnderlyingSnapshot(**result["underlying"])

        contracts = []
        for c in result["contracts"]:
            greeks = GreeksSnapshot(**c["greeks"]) if c.get("greeks") else None
            day = DaySnapshot(**c["day"]) if c.get("day") else None
            last_trade = LastTradeSnapshot(**c["last_trade"]) if c.get("last_trade") else None
            last_quote = LastQuoteSnapshot(**c["last_quote"]) if c.get("last_quote") else None
            contracts.append(
                OptionsContractSnapshotItem(
                    ticker=c.get("ticker"),
                    contract_type=c.get("contract_type"),
                    strike_price=c.get("strike_price"),
                    expiration_date=c.get("expiration_date"),
                    break_even_price=c.get("break_even_price"),
                    implied_volatility=c.get("implied_volatility"),
                    open_interest=c.get("open_interest"),
                    greeks=greeks,
                    day=day,
                    last_trade=last_trade,
                    last_quote=last_quote,
                )
            )

        logger.info(f"[Snapshot] Returning {len(contracts)} contracts for {request.underlying_ticker}")

        # Source live r and q for callers (pricing-lab, strategy-builder, etc.).
        # The dividend lookup is best-effort and may leave q unset. The rate
        # needs no spot and never fails (FRED falls back to the one Python
        # default), so every snapshot carries it and the pricing pages never
        # invent their own (#2764).
        risk_free_rate: float | None = None
        dividend_yield: float | None = None
        rate_source: str | None = None
        dividend_source: str | None = None
        spot = underlying.price if underlying and underlying.price else None
        if spot and spot > 0:
            try:
                rd = get_rate_and_dividend(
                    ticker=request.underlying_ticker,
                    spot_price=spot,
                    polygon=polygon_client,
                    dte_days=30,
                )
                risk_free_rate = rd.rate
                dividend_yield = rd.dividend_yield
                rate_source = rd.source_rate
                dividend_source = rd.source_dividend
            except Exception as exc:
                logger.warning("[Snapshot] rate/dividend lookup failed: %s", exc)
        if risk_free_rate is None:
            risk_free_rate = get_risk_free_rate(dte_days=30)
            rate_source = "FRED"

        return OptionsChainSnapshotResponse(
            success=True,
            underlying=underlying,
            contracts=contracts,
            count=len(contracts),
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            rate_source=rate_source,
            dividend_source=dividend_source,
        )

    except Exception as e:
        logger.error(f"[Snapshot] Error fetching chain snapshot: {e!s}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to fetch options chain snapshot: {e!s}"
        )


def _build_ticker_snapshot(data: dict) -> StockTickerSnapshot:
    """Convert a raw polygon_client snapshot dict into a StockTickerSnapshot model."""
    day = data.get("day")
    prev_day = data.get("prev_day")
    minute = data.get("min")
    return StockTickerSnapshot(
        ticker=data.get("ticker"),
        day=SnapshotBar(**day) if day else None,
        prev_day=SnapshotBar(**prev_day) if prev_day else None,
        min=MinuteBar(**minute) if minute else None,
        todays_change=data.get("todays_change"),
        todays_change_percent=data.get("todays_change_percent"),
        updated=data.get("updated"),
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
        logger.error(f"[Snapshot] Error fetching ticker snapshot: {e!s}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to fetch stock snapshot: {e!s}"
        )
