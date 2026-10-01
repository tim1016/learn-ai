"""API endpoints for options contract data"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.requests import OptionsExpirationsRequest
from app.models.responses import OptionsExpirationsResponse
from app.services.polygon_client import PolygonClientService

router = APIRouter()
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


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
        logger.error(f"[Options] Error listing expirations: {e!s}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list options expirations: {e!s}"
        )
