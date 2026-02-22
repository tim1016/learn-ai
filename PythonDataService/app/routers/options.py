"""API endpoints for options contract data"""
from fastapi import APIRouter, HTTPException, status
import logging

from app.services.polygon_client import PolygonClientService
from app.models.requests import OptionsContractsRequest, OptionsExpirationsRequest
from app.models.responses import OptionsContractsResponse, OptionsContractItem, OptionsExpirationsResponse

router = APIRouter()
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


@router.post("/contracts", response_model=OptionsContractsResponse)
async def list_options_contracts(request: OptionsContractsRequest):
    """
    List options contracts for a given underlying ticker.

    - **underlying_ticker**: Underlying stock symbol (e.g., GLD, SPY)
    - **as_of_date**: Date to check contracts as of (YYYY-MM-DD)
    - **contract_type**: Filter by call or put
    - **strike_price_gte/lte**: Filter by strike price range
    - **expiration_date**: Exact or range filter for expiration
    - **limit**: Maximum number of results (default 100)
    """
    try:
        logger.info(f"[Options] Request: underlying={request.underlying_ticker}, "
                     f"as_of={request.as_of_date}, type={request.contract_type}, "
                     f"strike=[{request.strike_price_gte}, {request.strike_price_lte}]")

        raw_contracts = polygon_client.list_options_contracts(
            underlying_ticker=request.underlying_ticker,
            as_of_date=request.as_of_date,
            contract_type=request.contract_type,
            strike_price_gte=request.strike_price_gte,
            strike_price_lte=request.strike_price_lte,
            expiration_date=request.expiration_date,
            expiration_date_gte=request.expiration_date_gte,
            expiration_date_lte=request.expiration_date_lte,
            expired=request.expired,
            limit=request.limit,
        )

        contracts = [OptionsContractItem(**c) for c in raw_contracts]

        logger.info(f"[Options] Returning {len(contracts)} contracts for {request.underlying_ticker}")

        return OptionsContractsResponse(
            success=True,
            contracts=contracts,
            count=len(contracts),
        )

    except Exception as e:
        logger.error(f"[Options] Error listing contracts: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list options contracts: {str(e)}"
        )


@router.post("/expirations", response_model=OptionsExpirationsResponse)
async def list_options_expirations(request: OptionsExpirationsRequest):
    """
    List unique expiration dates for options on an underlying ticker.
    Much faster than fetching full contracts — sorts by expiration_date
    and extracts unique dates without loading full contract payloads.
    """
    try:
        logger.info(
            f"[Options] Expirations request: underlying={request.underlying_ticker}, "
            f"type={request.contract_type}, "
            f"range=[{request.expiration_date_gte}, {request.expiration_date_lte}]"
        )

        expirations = polygon_client.list_options_expirations(
            underlying_ticker=request.underlying_ticker,
            contract_type=request.contract_type,
            expiration_date_gte=request.expiration_date_gte,
            expiration_date_lte=request.expiration_date_lte,
        )

        logger.info(f"[Options] Returning {len(expirations)} expirations for {request.underlying_ticker}")

        return OptionsExpirationsResponse(
            success=True,
            expirations=expirations,
            count=len(expirations),
        )

    except Exception as e:
        logger.error(f"[Options] Error listing expirations: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list options expirations: {str(e)}"
        )
