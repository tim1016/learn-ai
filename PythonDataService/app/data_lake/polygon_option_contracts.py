"""Polygon option-contract listing fetcher (#3030).

``/v3/reference/options/contracts`` with ``as_of`` and no ``expired`` flag
answers the contracts alive on that date, the ones expiring that day included
(probed 2026-10-12). ``as_of`` with ``expired=true`` answers only contracts
already expired by then, which is the wrong query for a chain.

A listing is admitted to the lake only if it is whole, so this fetcher returns
only after the vendor has said there is no next page: a ``next_url`` that is absent
or ``null``. Every other end of a call (an HTTP error, a status that is not OK, a
cursor that is present but not a non-empty text, a cursor that loops, a cursor
that leaves Polygon, a page count past any real listing, a paging that runs out
of the time it was given) raises one of the lake's typed Polygon errors. It never
hands back what it had read so far.
The capped helper in ``app/services/polygon_client.py`` stops early on
purpose and is not used here.

The key rides in an ``Authorization: Bearer`` header, on the first page and on
every later one, and in no URL (``polygon_option_http``). A cursor that leaves
Polygon is refused before any request, so the header goes nowhere else.

A row the code cannot fully read is kept: the fetcher gives back what it could
read of it and ``None`` for the rest, and the lake marks the row non-standard.
Only a row that is not a JSON object, or has no usable ticker (blank, not text, or
not ASCII), is an error (:class:`PolygonContractRowError`), because there is nothing to
keep. Every page must say its rows as a list, and an empty page may not point to a next
one: the listing is whole or it is an error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.data_lake.polygon_fetcher import _POLYGON_BASE, _TIMEOUT_S, PolygonFetchError
from app.data_lake.polygon_option_http import get_json_object, require_confirmed_status

logger = logging.getLogger(__name__)

_CONTRACTS_PATH = "/v3/reference/options/contracts"
#: The vendor's largest page for this endpoint (probed 2026-10-12).
_PAGE_LIMIT = 1000
#: A ceiling no real listing reaches: an underlying's whole chain is tens of pages.
#: It bounds the memory a listing can take; past it the paging is not converging and the
#: fetch is refused. The time a listing may take is the caller's ``budget_s``.
_MAX_PAGES = 200


class PolygonContractRowError(PolygonFetchError):
    """A listed row that is not a JSON object, or has no usable ticker: there is nothing of it to keep."""


class PolygonListingTimeoutError(PolygonFetchError):
    """The paging used up the time it was given, so the pages read so far are not a listing."""


def _now_s() -> float:
    """The clock the paging budget runs on, in seconds that never go back. A test replaces it to move time by hand."""
    return time.monotonic()


@dataclass(frozen=True)
class PolygonOptionContract:
    """One contract from the listing, as far as the vendor's row could be read.

    ``ticker`` is always there. Every other field is ``None`` when the vendor left it
    out or wrote a value of another kind (a strike that is not a number, a style that
    is not text), and the lake then stores it empty and calls the row non-standard.
    ``strike`` and ``shares_per_contract`` are the exact decimals in the JSON text.
    Whether a value is a sensible strike or share count is the lake's to judge.
    """

    ticker: str
    contract_type: str | None
    strike: Decimal | None
    expiration_date: str | None
    exercise_style: str | None
    shares_per_contract: Decimal | None
    has_additional_underlyings: bool


async def fetch_option_contracts(
    underlying: str, as_of: date, api_key: str, *, budget_s: float | None = None
) -> list[PolygonOptionContract]:
    """Every contract listed for ``underlying`` as of ``as_of``, in the order the vendor gave them.

    Raises the ``Polygon*`` errors of ``polygon_fetcher`` and never returns a
    partial listing.

    ``budget_s`` is the time the whole paging may take, counted from the call. The pages are
    asked one after another, each with its own request timeout, so without it a slow vendor
    could keep the paging going past whatever the caller holds the listing under (the lake's
    lease). Once it has passed, :class:`PolygonListingTimeoutError` is raised before the next
    request. ``None`` means no deadline, for a caller that holds nothing that can run out.
    """
    url = f"{_POLYGON_BASE}{_CONTRACTS_PATH}"
    params = {
        "underlying_ticker": underlying.upper(),
        "as_of": as_of.strftime("%Y-%m-%d"),
        "limit": _PAGE_LIMIT,
    }
    out: list[PolygonOptionContract] = []
    read: set[str] = set()
    next_url: str | None = url
    deadline = None if budget_s is None else _now_s() + budget_s

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        while next_url is not None:
            if next_url in read:
                raise PolygonFetchError(f"Polygon contract paging looped back to a page already read for {underlying}")
            if len(read) >= _MAX_PAGES:
                raise PolygonFetchError(f"Polygon contract paging passed {_MAX_PAGES} pages for {underlying}")
            if next_url != url and not next_url.startswith(f"{_POLYGON_BASE}/"):
                # The key rides in a header on every request, so a cursor that leaves Polygon is never followed.
                raise PolygonFetchError(f"Polygon's next page for {underlying} is not on api.polygon.io")
            if deadline is not None and _now_s() >= deadline:
                raise PolygonListingTimeoutError(
                    f"Polygon contract paging for {underlying} used up its {budget_s:g} s after {len(read)} page(s)"
                )
            read.add(next_url)
            # A later page's URL holds the vendor's cursor; httpx would replace that query if ``params`` were given.
            payload = await get_json_object(
                client, next_url, params=params if next_url == url else None, api_key=api_key, label=underlying
            )
            require_confirmed_status(payload, underlying, 200)
            # Every page, the last included, says its rows as a list. An underlying with no options answers
            # ``[]`` (probed 2026-10-12), so a page without one is a degraded answer, and reading it as an
            # empty last page would store the pages before it as the whole listing.
            results = payload.get("results")
            if not isinstance(results, list):
                raise PolygonFetchError(f"Polygon contract results for {underlying} are not a list", 200)
            # No more pages is a cursor that is absent or null. Any other value that is not a text with something in it
            # (false, 0, [], {}, "") is a damaged answer, and reading it as the end would store a cut-short listing as whole.
            cursor = payload.get("next_url")
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise PolygonFetchError(f"Polygon next page for {underlying} is not a URL", 200)
            if cursor is not None and not results:
                raise PolygonFetchError(f"Polygon sent an empty page with a next page for {underlying}", 200)
            out.extend(_contract_from_row(row, underlying) for row in results)
            next_url = cursor
    logger.info(
        "data_lake.polygon_option_contracts: listing fetched",
        extra={"action": "option_listing_fetched", "underlying": underlying.upper(), "contracts": len(out), "pages": len(read)},
    )
    return out


def _contract_from_row(row: Any, underlying: str) -> PolygonOptionContract:
    if not isinstance(row, dict):
        raise PolygonContractRowError(f"Polygon contract row for {underlying} is not a JSON object", 200)
    ticker = row.get("ticker")
    # ASCII, because a ticker is stored in the listing file and looked up by its text; a real one is plain ASCII.
    if not isinstance(ticker, str) or not ticker.strip() or not ticker.isascii():
        raise PolygonContractRowError(
            f"Polygon contract row for {underlying} has no usable ticker (it must be non-blank ASCII text)", 200
        )
    return PolygonOptionContract(
        ticker=ticker,
        contract_type=_text_or_none(row.get("contract_type")),
        strike=_number_or_none(row.get("strike_price")),
        expiration_date=_text_or_none(row.get("expiration_date")),
        exercise_style=_text_or_none(row.get("exercise_style")),
        shares_per_contract=_number_or_none(row.get("shares_per_contract")),
        # A standard row carries no additional_underlyings key (probed 2026-10-12): absent means none.
        # Anything else, an empty list apart, counts as some, which keeps the row from being called standard.
        has_additional_underlyings=row.get("additional_underlyings") not in (None, []),
    )


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number_or_none(value: Any) -> Decimal | None:
    """A JSON number as the exact decimal it was written as; ``None`` for anything else.

    A float arrives only from a ``NaN`` or ``Infinity`` literal (a decimal point parses
    as ``Decimal``), which is not a number the lake will keep.
    """
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    return Decimal(value)
