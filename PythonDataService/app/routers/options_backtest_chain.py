"""The Options Backtest page's past-chain routes (#3030).

``GET /past-chain/day`` says what the listing held on a past trading date, every expiry with its tag.
``GET /past-chain/expiry`` says one expiry at one instant of that session: the underlying's price, the money, and the
calls and puts around it with the price each last traded at. ``POST /past-chain/download`` captures into the lake
what those two need: the listing, the underlying's bars and the bars of the contracts named.

The trading date and the expiry arrive apart and neither moves the other. A trading date is any instant on its New York
date; an expiry is its 16:00 New York anchor. The reads block, so each runs in a worker thread.

The two reads are open, like the other lake reads: they ask the vendor for nothing. The download spends vendor requests
and writes the lake, as ``POST /api/data-lake/ensure-data`` does, and carries the data plane's control secret as it does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.data_lake.catalog_client import CatalogUnavailableError
from app.options_backtest.chain_download import FetchRefusal, ensure_chain_data, fetch_refusal
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.past_chain import (
    DEFAULT_STRIKES_EACH_SIDE,
    MAX_STRIKES_EACH_SIDE,
    ChainDay,
    ChainExpiry,
    ChainRefusal,
    chain_session,
    contract_from_symbol,
    describe_day,
    describe_expiry,
    expiry_date_of,
)
from app.options_backtest.setup import UNDERLYING_PATTERN
from app.schemas.options_backtest import OptionsBacktestRefusalBody
from app.schemas.options_backtest_chain import (
    PastChainDayResponse,
    PastChainDownloadRequest,
    PastChainDownloadResponse,
    PastChainExpiryResponse,
)
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.session_anchors import MAX_TIMESTAMP_MS

logger = logging.getLogger(__name__)

router = APIRouter()

_REFUSALS = {
    status.HTTP_400_BAD_REQUEST: {"model": OptionsBacktestRefusalBody},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": OptionsBacktestRefusalBody},
}

Underlying = Annotated[
    str, Query(pattern=UNDERLYING_PATTERN, description="The underlying's ticker, one to five letters.")
]
TradingDateMs = Annotated[
    int,
    Query(
        ge=0, le=MAX_TIMESTAMP_MS, description="Any instant on the New York trading date; the page sends its midnight."
    ),
]
ExpiryMs = Annotated[
    int, Query(ge=0, le=MAX_TIMESTAMP_MS, description="The expiry's anchor: 16:00 New York time on its date.")
]
InstantMs = Annotated[
    int | None,
    Query(
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description=(
            "An instant within the session, on a whole minute (a multiple of 60 000 ms); the session's close when "
            "absent. The price shown is the last bar closing at or before it."
        ),
    ),
]
StrikesEachSide = Annotated[
    int, Query(ge=0, le=MAX_STRIKES_EACH_SIDE, description="Strikes shown on each side of the money, for each right.")
]


def _refused(error: ChainRefusal) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": error.code, "message": str(error)})


def _catalog_is_down(error: CatalogUnavailableError) -> HTTPException:
    # The error names the file it was reading by its path on the server's disk: that goes to the log, not the wire.
    logger.warning("the data lake's catalog cannot be reached", extra={"error": str(error)})
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE",
            "message": "The data lake's catalog cannot be reached, so what is stored cannot be read.",
        },
    )


def _read_day(underlying: str, trading_date_ms: int) -> tuple[ChainDay, FetchRefusal | None]:
    session = chain_session(trading_date_ms)
    day = describe_day(LakeRunData(underlying), underlying=underlying, session=session)
    return day, fetch_refusal(underlying, session.trading_date)


def _read_expiry(underlying: str, trading_date_ms: int, expiry_ms: int, at_ms: int | None, strikes: int) -> ChainExpiry:
    session = chain_session(trading_date_ms)
    expiry = expiry_date_of(expiry_ms)
    return describe_expiry(
        LakeRunData(underlying),
        underlying=underlying,
        session=session,
        expiry=expiry,
        at_ms=at_ms,
        strikes_each_side=strikes,
    )


@router.get("/past-chain/day", response_model=PastChainDayResponse, responses=_REFUSALS)
async def get_past_chain_day(underlying: Underlying, trading_date_ms: TradingDateMs) -> PastChainDayResponse:
    """Every expiry the underlying's listing held on the date, tagged, with its calls and puts counted."""
    # ``fetch_refusal`` is whether the lake would fetch anything for the session now, by the rule a download asks, on
    # the clock a download takes.
    try:
        day, refusal = await asyncio.to_thread(_read_day, underlying, trading_date_ms)
    except ChainRefusal as exc:
        raise _refused(exc) from exc
    except CatalogUnavailableError as exc:
        raise _catalog_is_down(exc) from exc
    return PastChainDayResponse.from_day(day, refusal)


@router.get("/past-chain/expiry", response_model=PastChainExpiryResponse, responses=_REFUSALS)
async def get_past_chain_expiry(
    underlying: Underlying,
    trading_date_ms: TradingDateMs,
    expiry_ms: ExpiryMs,
    at_ms: InstantMs = None,
    strikes: StrikesEachSide = DEFAULT_STRIKES_EACH_SIDE,
) -> PastChainExpiryResponse:
    """One expiry at one instant: the underlying's price, the money, and the contracts around it with their prices."""
    try:
        chain = await asyncio.to_thread(_read_expiry, underlying, trading_date_ms, expiry_ms, at_ms, strikes)
    except ChainRefusal as exc:
        raise _refused(exc) from exc
    except CatalogUnavailableError as exc:
        raise _catalog_is_down(exc) from exc
    return PastChainExpiryResponse.from_expiry(chain, expiry_ms)


@router.post(
    "/past-chain/download",
    response_model=PastChainDownloadResponse,
    responses=_REFUSALS,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def post_past_chain_download(request: PastChainDownloadRequest) -> PastChainDownloadResponse:
    """Capture the listing, the underlying's bars and the bars of the contracts named; one item says how each came out.

    Needs the data plane's control secret (the ``X-Data-Plane-Control-Secret`` header), as it spends vendor requests
    and writes the lake. A vendor or lake failure is an item with its typed reason and never a failed request. A
    request the page should not have made (a date with no session, an expiry that is no anchor, a contract that is no
    OCC symbol or not of the expiry, more than the cap) is refused before anything is asked. A contract that another
    request is fetching at the same moment is an item that failed with reason ``lease_timeout``: it is not missing; ask
    again once the other request has finished.
    """
    try:
        session = await asyncio.to_thread(chain_session, request.trading_date_ms)
        expiry = None if request.expiry_ms is None else expiry_date_of(request.expiry_ms)
        items = await ensure_chain_data(
            request.underlying,
            session.trading_date,
            expiry=expiry,
            contracts=[contract_from_symbol(symbol) for symbol in request.contracts],
        )
    except ChainRefusal as exc:
        raise _refused(exc) from exc
    except CatalogUnavailableError as exc:
        raise _catalog_is_down(exc) from exc
    return PastChainDownloadResponse.from_items(request.underlying, session, items)
