"""The key both option fetchers send, and what is done with a key that cannot be sent (#3030).

Both fetchers build their request in one place (``polygon_option_http.get_json_object``), so each
rule here is driven through both of them: the minute-bar fetcher and the contract-listing fetcher.
The vendor is faked with ``respx``; the keys are made-up sentinels.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date

import h11
import httpx
import pytest
import respx

from app.data_lake.polygon_fetcher import PolygonAuthError
from app.data_lake.polygon_option_bars import fetch_option_minute_bars
from app.data_lake.polygon_option_contracts import fetch_option_contracts
from tests._helpers.option_lake_cases import pieces_of_the_key_in
from tests._helpers.option_vendor import bar_row, traded

DAY = date(2025, 6, 13)
SYMBOL = "SPY250613C00600000"
KEY = "sentinel-key-4f9a1c"
#: A key that is malformed has this in it, so a test can look for any piece of it in a message.
TOKEN = "Zq9xV7wK"
LISTING_URL = "https://api.polygon.io/v3/reference/options/contracts"


def _listing_page() -> httpx.Response:
    row = {"ticker": f"O:{SYMBOL}", "contract_type": "call", "strike_price": 600, "expiration_date": "2025-06-13"}
    return httpx.Response(200, json={"status": "OK", "request_id": "r", "results": [row]})


#: Each fetcher, with a vendor answer that is good for it and a call that takes the key to send.
FETCHERS: dict[str, tuple[Callable[[str], Awaitable[object]], Callable[[], httpx.Response]]] = {
    "bars": (lambda key: fetch_option_minute_bars(SYMBOL, DAY, key), lambda: traded(SYMBOL, [bar_row(0)])),
    "listing": (lambda key: fetch_option_contracts("SPY", DAY, key), _listing_page),
}


def _h11_sends(request: httpx.Request) -> None:
    """The wire library (the one under httpx) refuses an illegal header value; sending this request's head must not raise."""
    h11.Connection(h11.CLIENT).send(
        h11.Request(method=request.method, target=request.url.raw_path, headers=request.headers.raw)
    )


@respx.mock
@pytest.mark.parametrize("fetcher", list(FETCHERS))
@pytest.mark.parametrize(
    "stray",
    ["\n", "\r\n", " ", "\t", "\u00a0", "  \r\n", "\x1f", "\x85"],
    ids=["newline", "crlf", "space", "tab", "no-break space", "spaces and crlf", "unit separator", "next line"],
)
async def test_a_key_with_stray_whitespace_at_either_end_is_sent_as_its_stripped_self(fetcher: str, stray: str) -> None:
    """The HTTP library refuses a header value that ends in whitespace and names the value in its error."""
    call, answer = FETCHERS[fetcher]
    route = respx.route().mock(return_value=answer())

    await call(f"{stray}{KEY}{stray}")

    [sent] = [c.request for c in route.calls]
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    _h11_sends(sent)  # the head the wire library would send is a legal one
    assert KEY not in str(sent.url)


@respx.mock
@pytest.mark.parametrize("fetcher", list(FETCHERS))
@pytest.mark.parametrize(
    "key",
    [
        "",
        " ",
        "\n\t ",
        f"{TOKEN} {TOKEN}",
        f"{TOKEN}\n{TOKEN}",
        f"{TOKEN}\r{TOKEN}",
        f"{TOKEN}é",
        f"ключ{TOKEN}",
        f"{TOKEN}\u2603",
        f"{TOKEN}\x00",
        f"{TOKEN}\x7f",
        f"{TOKEN}\x1f{TOKEN}",
        f"{TOKEN}\ud800",
    ],
    ids=[
        "empty", "one space", "only whitespace", "inner space", "inner newline", "inner carriage return",
        "accented letter", "cyrillic", "snowman", "nul", "delete", "inner unit separator", "lone surrogate",
    ],
)
async def test_a_key_that_cannot_ride_in_a_header_is_the_auth_error_before_any_request_and_names_no_part_of_itself(
    fetcher: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    call, answer = FETCHERS[fetcher]
    caplog.set_level(logging.DEBUG)
    route = respx.route().mock(return_value=answer())

    with pytest.raises(PolygonAuthError) as raised:  # the error a 401 gives; not a UnicodeEncodeError
        await call(key)

    assert route.call_count == 0
    assert raised.value.status_code is None  # no answer was ever received
    assert "malformed" in str(raised.value)
    shown = str(raised.value) + repr(raised.value) + repr(raised.value.__cause__) + repr(raised.value.__context__)
    shown += "".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
    assert pieces_of_the_key_in(key.strip(), shown) == set()


@respx.mock
@pytest.mark.parametrize("fetcher", list(FETCHERS))
async def test_a_good_key_is_sent_unchanged(fetcher: str) -> None:
    call, answer = FETCHERS[fetcher]
    route = respx.route().mock(return_value=answer())

    await call(KEY)

    assert route.calls[0].request.headers["Authorization"] == f"Bearer {KEY}"
