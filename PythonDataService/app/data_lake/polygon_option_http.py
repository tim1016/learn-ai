"""What the two option fetchers share: one authenticated request, read as a JSON object (#3030).

The key rides in an ``Authorization: Bearer`` header and in no URL and no query
(probed 2026-10-12: the vendor accepts it that way for minute bars, for the
contract listing and for a later page of the listing). A URL is what a log line,
an exception message or a stored error text carries; a header is what none of
them do. The stock fetchers still put the key in the URL. The key is sent stripped of
surrounding whitespace, and a key that cannot be a header value (empty, or holding a space
or anything but printable ASCII) is refused before any request, as the error a 401 gives.

Every way a request can go wrong ends as a ``Polygon*`` error of
``polygon_fetcher``, never as a raw ``httpx`` or ``json`` exception: a timeout,
a refused or dropped connection, a status other than 200, a body that is not
JSON, a JSON value that is not an object.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonUnknownSymbolError,
    _raise_for_status,
)

#: The vendor writes an option's ticker as its OCC symbol behind this prefix.
OPTION_TICKER_PREFIX = "O:"

#: What a key may hold to ride in a header: printable ASCII, no space. Anything else is
#: refused before a request, because the HTTP library's own error text for a bad header
#: value names the value, and a stored failure must never hold the key.
_SENDABLE_KEY = re.compile(r"[\x21-\x7e]+")

#: What the vendor's 403 says when a session is older than the plan reaches (probed
#: 2026-10-12: "Your plan doesn't include this data timeframe. Please upgrade your plan").
_OUTSIDE_PLAN_PHRASE = "data timeframe"


def _sendable_key(api_key: str) -> str:
    """The key to send: ``api_key`` without surrounding whitespace, or a typed auth error when it cannot ride in a header.

    A key that is empty, or holds a space or a character that is not printable ASCII, is
    refused before any request with the error a 401 gives. The message names no part of the
    key, not a character, a position or a length, because it is stored on the row and logged.
    """
    key = api_key.strip()
    if _SENDABLE_KEY.fullmatch(key) is None:
        raise PolygonAuthError(
            "the Polygon API key is malformed: it is empty, or holds a space or a character that is not "
            "printable ASCII; correct POLYGON_API_KEY",
            None,
        )
    return key


class PolygonOutsidePlanError(PolygonEntitlementError):
    """403 whose body says the plan does not include this data timeframe.

    The vendor's own word that the session is older than the plan's rolling window.
    Any other 403 stays a plain :class:`PolygonEntitlementError`, a real entitlement problem.
    """


async def get_json_object(
    client: httpx.AsyncClient,
    url: str | httpx.URL,
    *,
    params: Mapping[str, str | int] | None,
    api_key: str,
    label: str,
) -> dict[str, Any]:
    """GET ``url`` with the key in a header; the answer as a JSON object, or a typed error.

    The key is stripped of surrounding whitespace, and a key that cannot ride in a header
    raises :class:`PolygonAuthError` before the request (:func:`_sendable_key`).
    ``params`` is given for a first request only. A later page's URL already holds the
    vendor's cursor, and httpx would replace that query if ``params`` were passed.
    JSON numbers with a decimal point parse as ``Decimal``, so a price or a strike
    keeps the vendor's exact digits. ``label`` names the request in error texts.
    """
    key = _sendable_key(api_key)
    try:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException as exc:
        raise PolygonFetchError(f"Polygon request timed out for {label}: {exc}", status_code=None) from exc
    except (httpx.RequestError, httpx.InvalidURL) as exc:
        raise PolygonFetchError(f"Polygon transport error for {label}: {exc}", status_code=None) from exc
    if resp.status_code == 403:
        message = _outside_plan_message(resp)
        if message is not None:
            raise PolygonOutsidePlanError(f"Polygon 403 for {label}: {message}", 403)
    _raise_for_status(resp, label)
    try:
        payload = resp.json(parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise PolygonFetchError(f"Polygon answer for {label} is not JSON: {exc}", resp.status_code) from exc
    if not isinstance(payload, dict):
        raise PolygonFetchError(f"Polygon answer for {label} is not a JSON object", resp.status_code)
    return payload


def _outside_plan_message(resp: httpx.Response) -> str | None:
    """The vendor's message when a 403 is its "plan does not include this timeframe" answer, else ``None``."""
    try:
        body = resp.json()
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or body.get("status") != "NOT_AUTHORIZED":
        return None
    message = body.get("message")
    if isinstance(message, str) and _OUTSIDE_PLAN_PHRASE in message.lower():
        return message[:200]
    return None


def require_confirmed_status(payload: dict[str, Any], label: str, http_code: int) -> None:
    """Raise unless the answer says its status is ``OK`` (or ``DELAYED``, which a delayed-data plan answers).

    A missing status confirms nothing; the stock fetcher reads it as success.
    """
    status = payload.get("status")
    if status in ("OK", "DELAYED"):
        return
    if status in ("NOT_FOUND", "ERROR_NOT_FOUND"):
        raise PolygonUnknownSymbolError(f"Polygon status={status} for {label}", http_code)
    if status is None:
        raise PolygonFetchError(f"Polygon answer for {label} did not confirm status OK", http_code)
    error = payload.get("error")
    detail = f": {error[:200]}" if isinstance(error, str) else ""
    raise PolygonFetchError(f"Polygon payload status={str(status)[:40]} for {label}{detail}", http_code)
