"""Polygon option-contract listing fetcher (#3030).

``/v3/reference/options/contracts`` with ``as_of`` and no ``expired`` flag
answers the contracts alive on that date, the ones expiring that day included
(probed 2026-10-12). ``as_of`` with ``expired=true`` answers only contracts
already expired by then, which is the wrong query for a chain.

A listing is admitted to the lake only if it is whole, so this fetcher returns
only after the vendor has said there is no next page. Every other end of a
call (an HTTP error, a status that is not OK, a cursor that loops, a cursor
that leaves Polygon, a page count past any real listing) raises one of the
lake's typed Polygon errors. It never hands back what it had read so far.
The capped helper in ``app/services/polygon_client.py`` stops early on
purpose and is not used here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.data_lake.polygon_fetcher import (
    _POLYGON_BASE,
    _TIMEOUT_S,
    PolygonFetchError,
    _raise_for_payload_status,
    _raise_for_status,
)

logger = logging.getLogger(__name__)

_CONTRACTS_PATH = "/v3/reference/options/contracts"
#: The vendor's largest page for this endpoint (probed 2026-10-12).
_PAGE_LIMIT = 1000
#: A ceiling no real listing reaches (an underlying's whole chain is tens of
#: pages); past it the paging is not converging and the fetch is refused.
_MAX_PAGES = 2_000


@dataclass(frozen=True)
class PolygonOptionContract:
    """One contract from the listing, as the vendor wrote it.

    ``strike`` is the exact decimal in the JSON text. ``exercise_style`` and
    ``shares_per_contract`` are ``None`` when the vendor left them out, which
    keeps the contract from being called standard further on.
    """

    ticker: str
    contract_type: str
    strike: Decimal
    expiration_date: str
    exercise_style: str | None
    shares_per_contract: Decimal | None
    has_additional_underlyings: bool


async def fetch_option_contracts(underlying: str, as_of: date, api_key: str) -> list[PolygonOptionContract]:
    """Every contract listed for ``underlying`` as of ``as_of``, in the order the vendor gave them.

    Raises the ``Polygon*`` errors of ``polygon_fetcher`` and never returns a
    partial listing.
    """
    url = f"{_POLYGON_BASE}{_CONTRACTS_PATH}"
    params = {
        "underlying_ticker": underlying.upper(),
        "as_of": as_of.strftime("%Y-%m-%d"),
        "limit": _PAGE_LIMIT,
        "apiKey": api_key,
    }
    out: list[PolygonOptionContract] = []
    read: set[str] = set()
    next_url: str | None = url

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        while next_url is not None:
            if next_url in read:
                raise PolygonFetchError(f"Polygon contract paging looped back to a page already read for {underlying}")
            if len(read) >= _MAX_PAGES:
                raise PolygonFetchError(f"Polygon contract paging passed {_MAX_PAGES} pages for {underlying}")
            if next_url != url and not next_url.startswith(f"{_POLYGON_BASE}/"):
                # The key rides on every later request, so a cursor that leaves Polygon is never followed.
                raise PolygonFetchError(f"Polygon's next page for {underlying} is not on api.polygon.io")
            read.add(next_url)
            try:
                if next_url == url:
                    resp = await client.get(url, params=params)
                else:
                    # httpx replaces a URL's query when ``params`` is given, which would drop the
                    # vendor's cursor and re-read page one; the key is set on the URL itself.
                    resp = await client.get(httpx.URL(next_url).copy_set_param("apiKey", api_key))
            except httpx.TimeoutException as exc:
                raise PolygonFetchError(f"Polygon request timed out for {underlying}: {exc}", status_code=None) from exc
            except httpx.RequestError as exc:
                raise PolygonFetchError(f"Polygon transport error for {underlying}: {exc}", status_code=None) from exc
            _raise_for_status(resp, underlying)
            try:
                # Floats parse as Decimal so a strike keeps the vendor's exact digits.
                payload = resp.json(parse_float=Decimal)
            except ValueError as exc:
                raise PolygonFetchError(f"Polygon contract page for {underlying} is not JSON: {exc}", resp.status_code) from exc
            if not isinstance(payload, dict):
                raise PolygonFetchError(f"Polygon contract page for {underlying} is not a JSON object", resp.status_code)
            _raise_for_payload_status(payload, underlying, resp.status_code)
            if payload.get("status") not in ("OK", "DELAYED"):
                raise PolygonFetchError(
                    f"Polygon contract page for {underlying} did not confirm status OK", resp.status_code
                )
            results = payload.get("results") or []
            if not isinstance(results, list):
                raise PolygonFetchError(f"Polygon contract results for {underlying} are not a list", resp.status_code)
            out.extend(_contract_from_row(row, underlying) for row in results)
            cursor = payload.get("next_url") or None
            if cursor is not None and not isinstance(cursor, str):
                raise PolygonFetchError(f"Polygon next page for {underlying} is not a URL", resp.status_code)
            next_url = cursor
    logger.info(
        "data_lake.polygon_option_contracts: listing fetched",
        extra={"action": "option_listing_fetched", "underlying": underlying.upper(), "contracts": len(out), "pages": len(read)},
    )
    return out


def _contract_from_row(row: Any, underlying: str) -> PolygonOptionContract:
    if not isinstance(row, dict):
        raise PolygonFetchError(f"Polygon contract row for {underlying} is not a JSON object")
    ticker = row.get("ticker")
    contract_type = row.get("contract_type")
    expiration = row.get("expiration_date")
    if not isinstance(ticker, str) or not ticker:
        raise PolygonFetchError(f"Polygon contract row for {underlying} has no ticker")
    if not isinstance(contract_type, str) or not contract_type:
        raise PolygonFetchError(f"Polygon contract row {ticker} has no contract_type")
    if not isinstance(expiration, str) or not expiration:
        raise PolygonFetchError(f"Polygon contract row {ticker} has no expiration_date")
    exercise_style = row.get("exercise_style")
    return PolygonOptionContract(
        ticker=ticker,
        contract_type=contract_type,
        strike=_decimal(row.get("strike_price"), what="strike_price", ticker=ticker),
        expiration_date=expiration,
        exercise_style=exercise_style if isinstance(exercise_style, str) and exercise_style else None,
        shares_per_contract=(
            None
            if row.get("shares_per_contract") is None
            else _decimal(row["shares_per_contract"], what="shares_per_contract", ticker=ticker)
        ),
        has_additional_underlyings=bool(row.get("additional_underlyings")),
    )


def _decimal(value: Any, *, what: str, ticker: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PolygonFetchError(f"Polygon contract row {ticker} has no {what}")
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise PolygonFetchError(f"Polygon contract row {ticker} has a {what} that is not a number: {value!r}") from exc
