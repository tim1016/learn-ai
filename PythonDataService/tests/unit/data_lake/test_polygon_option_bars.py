"""The option minute-bar fetcher: a confirmed answer, or a typed error and nothing (#3030).

The vendor says the same thing for a listed contract that did not trade, a
contract that never existed, one that expired and a Saturday, so an answer is
taken only if it is well formed. The vendor is faked with ``respx`` using the
answers probed on the wire (``tests/_helpers/option_vendor.py``); the key is a
made-up sentinel and rides in a header.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
)
from app.data_lake.polygon_option_bars import PolygonOptionBar, fetch_option_minute_bars
from app.data_lake.polygon_option_http import PolygonOutsidePlanError
from tests._helpers.option_vendor import (
    SESSION_OPEN_MS,
    bad_bars_answers,
    bar_row,
    no_trades,
    no_trades_body,
    outside_plan,
    raw_json,
    traded,
    traded_body,
)

DAY = date(2025, 6, 13)
SYMBOL = "SPY250613C00600000"
BARS_URL = rf"https://api\.polygon\.io/v2/aggs/ticker/O:{SYMBOL}/range/1/minute/2025-06-13/2025-06-13"
SENTINEL_KEY = "sentinel-key-4f9a1c"


def _vendor(response: httpx.Response | Exception) -> respx.Route:
    route = respx.get(url__regex=BARS_URL)
    if isinstance(response, Exception):
        return route.mock(side_effect=response)
    return route.mock(return_value=response)


async def _fetch() -> list[PolygonOptionBar]:
    return await fetch_option_minute_bars(SYMBOL, DAY, SENTINEL_KEY)


# ── A confirmed answer ───────────────────────────────────────────────────


@respx.mock
async def test_a_traded_session_comes_back_as_bars_with_the_vendors_exact_decimals() -> None:
    _vendor(traded(SYMBOL, [bar_row(0), bar_row(1, o=0.29, h=0.29, l=0.29, c=0.29, v=1)]))

    bars = await _fetch()

    assert bars == [
        PolygonOptionBar(
            t_ms=SESSION_OPEN_MS, open=Decimal("1.27"), high=Decimal("1.56"), low=Decimal("1.1"), close=Decimal("1.56"),
            volume=Decimal(4245),
        ),
        PolygonOptionBar(
            t_ms=SESSION_OPEN_MS + 60_000, open=Decimal("0.29"), high=Decimal("0.29"), low=Decimal("0.29"),
            close=Decimal("0.29"), volume=Decimal(1),
        ),
    ]
    assert all(type(bar.open) is Decimal and type(bar.t_ms) is int for bar in bars)


@respx.mock
async def test_a_price_keeps_every_digit_the_vendor_wrote() -> None:
    """17 or more digits are where a float would answer for a different number."""
    _vendor(raw_json(
        '{"status":"OK","ticker":"O:' + SYMBOL + '","resultsCount":1,"results":'
        '[{"t":1749821400000,"o":1.0000499999999999999,"h":1.0000499999999999999,"l":1.0000499999999999999,'
        '"c":1.0000499999999999999,"v":1.5}]}'
    ))

    [bar] = await _fetch()

    assert bar.open == Decimal("1.0000499999999999999") != Decimal(str(float("1.0000499999999999999")))
    assert bar.volume == Decimal("1.5")


@respx.mock
async def test_the_request_names_the_o_prefixed_contract_and_the_day_and_asks_for_raw_ascending_bars() -> None:
    route = _vendor(traded(SYMBOL, [bar_row(0)]))

    await _fetch()

    asked = route.calls[0].request
    assert asked.url.path == f"/v2/aggs/ticker/O:{SYMBOL}/range/1/minute/2025-06-13/2025-06-13"
    assert dict(asked.url.params) == {"adjusted": "false", "sort": "asc", "limit": "50000"}


@respx.mock
async def test_a_listed_contract_that_did_not_trade_is_a_confirmed_empty_answer() -> None:
    """The probed body: ``status OK``, the ticker, ``resultsCount 0``, ``queryCount 0`` and no ``results`` key at all."""
    body = no_trades_body(SYMBOL)
    assert "results" not in body and "count" not in body
    _vendor(no_trades(SYMBOL))

    assert await _fetch() == []


@respx.mock
async def test_an_empty_results_list_with_a_zero_count_is_also_confirmed_empty() -> None:
    _vendor(httpx.Response(200, json=no_trades_body(SYMBOL, results=[])))

    assert await _fetch() == []


@respx.mock
async def test_an_answer_that_says_nothing_of_adjusted_is_still_confirmed_and_one_that_says_false_is_the_probed_shape() -> None:
    """The probed answers carry ``adjusted: false``; the echo is not required, only never true."""
    body = traded_body(SYMBOL, [bar_row(0)])
    assert body["adjusted"] is False
    without_it = {k: v for k, v in body.items() if k != "adjusted"}

    _vendor(httpx.Response(200, json=without_it))
    assert len(await _fetch()) == 1
    _vendor(httpx.Response(200, json=body))
    assert len(await _fetch()) == 1


@respx.mock
async def test_an_answer_that_says_its_bars_are_adjusted_is_an_error_because_the_request_asked_for_raw_ones() -> None:
    _vendor(httpx.Response(200, json=traded_body(SYMBOL, [bar_row(0)], adjusted=True)))

    with pytest.raises(PolygonFetchError, match="not raw") as raised:
        await _fetch()

    assert type(raised.value) is PolygonFetchError and raised.value.status_code == 200


@respx.mock
async def test_a_delayed_status_is_a_confirmed_answer() -> None:
    _vendor(httpx.Response(200, json=traded_body(SYMBOL, [bar_row(0)], status="DELAYED")))

    assert len(await _fetch()) == 1


@respx.mock
async def test_an_empty_answer_cannot_tell_a_day_that_did_not_trade_from_one_that_could_not_exist() -> None:
    """The reason the lake proves "listed" from the stored listing: the vendor's empty body is one body.

    The same body answers a contract after it expired and a Saturday (probed 2026-10-12), so
    the fetcher gives ``[]`` for each and cannot say which of them it was.
    """
    route = respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/O:SPY250613C00600000/range/1/minute/.*")
    route.mock(return_value=no_trades(SYMBOL))

    on_a_trading_day = await fetch_option_minute_bars(SYMBOL, DAY, SENTINEL_KEY)
    after_expiry = await fetch_option_minute_bars(SYMBOL, date(2025, 6, 16), SENTINEL_KEY)
    on_a_saturday = await fetch_option_minute_bars(SYMBOL, date(2025, 6, 14), SENTINEL_KEY)

    assert on_a_trading_day == after_expiry == on_a_saturday == []


# ── The key rides in a header ────────────────────────────────────────────


@respx.mock
async def test_the_key_rides_in_an_authorization_header_and_in_no_url() -> None:
    route = _vendor(traded(SYMBOL, [bar_row(0)]))

    await _fetch()

    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {SENTINEL_KEY}"
    assert SENTINEL_KEY not in str(request.url)
    assert "apikey" not in str(request.url).lower()


@respx.mock
async def test_no_log_line_holds_the_key_even_at_the_apps_info_level(caplog: pytest.LogCaptureFixture) -> None:
    """The failure this guards: ``httpx`` logs every request's full URL at INFO, so a key in a URL is in the log."""
    caplog.set_level(logging.INFO)
    _vendor(traded(SYMBOL, [bar_row(0)]))
    await _fetch()
    _vendor(httpx.Response(500, text="boom"))
    with pytest.raises(PolygonFetchError):
        await _fetch()

    request_lines = [r for r in caplog.records if r.name == "httpx"]
    assert len(request_lines) == 2  # the guard is not vacuous: httpx did log both requests
    for record in caplog.records:
        assert SENTINEL_KEY not in record.getMessage()
        assert SENTINEL_KEY not in repr(record.__dict__)


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"status": "ERROR"}),
        outside_plan(),
        httpx.Response(429),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=no_trades_body(SYMBOL, resultsCount=5)),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("[Errno 61] Connection refused"),
    ],
    ids=["401", "outside plan", "429", "500", "not json", "count mismatch", "timeout", "refused"],
)
async def test_no_error_text_holds_the_key(answer: httpx.Response | Exception) -> None:
    _vendor(answer)

    with pytest.raises(PolygonFetchError) as raised:
        await _fetch()

    assert SENTINEL_KEY not in str(raised.value) and SENTINEL_KEY not in repr(raised.value)


@respx.mock
async def test_a_redirect_is_an_error_and_the_key_is_not_sent_to_where_it_points() -> None:
    _vendor(httpx.Response(302, headers={"location": "https://elsewhere.example/steal"}))
    elsewhere = respx.get("https://elsewhere.example/steal").mock(return_value=traded(SYMBOL, [bar_row(0)]))

    with pytest.raises(PolygonFetchError):
        await _fetch()

    assert elsewhere.call_count == 0


# ── Anything that is not a well-formed answer raises, and returns nothing ──


@respx.mock
@pytest.mark.parametrize("name", list(bad_bars_answers(SYMBOL)))
async def test_an_answer_that_is_not_well_formed_is_a_typed_error_and_nothing_is_returned(name: str) -> None:
    _vendor(bad_bars_answers(SYMBOL)[name]())

    with pytest.raises(PolygonFetchError) as raised:
        await _fetch()

    assert type(raised.value) is PolygonFetchError  # the generic typed failure, not a vendor-status subclass
    assert raised.value.status_code == 200


@respx.mock
async def test_a_next_page_is_an_error_and_is_never_followed() -> None:
    route = _vendor(
        httpx.Response(200, json=traded_body(SYMBOL, [bar_row(0)], next_url="https://api.polygon.io/v2/aggs/ticker/x?cursor=abc"))
    )
    next_page = respx.get("https://api.polygon.io/v2/aggs/ticker/x").mock(return_value=traded(SYMBOL, [bar_row(1)]))

    with pytest.raises(PolygonFetchError, match="next page"):
        await _fetch()

    assert route.call_count == 1 and next_page.call_count == 0


@respx.mock
async def test_a_status_the_vendor_uses_for_an_unknown_ticker_is_the_unknown_symbol_error() -> None:
    _vendor(httpx.Response(200, json=no_trades_body(SYMBOL, status="NOT_FOUND")))

    with pytest.raises(PolygonUnknownSymbolError):
        await _fetch()


@pytest.mark.parametrize("not_a_symbol", ["", "../etc/passwd", "O:SPY250613C00600000", "SPY250613C0060000", "spy250613c00600000"])
async def test_a_ticker_that_is_not_an_occ_symbol_never_reaches_a_request(not_a_symbol: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=traded(SYMBOL, [bar_row(0)]))

        with pytest.raises(ValueError, match="option contract symbol"):
            await fetch_option_minute_bars(not_a_symbol, DAY, SENTINEL_KEY)

        assert route.call_count == 0


# ── What the vendor says for a failed request ────────────────────────────


@respx.mock
async def test_a_403_that_names_the_data_timeframe_is_the_outside_plan_error() -> None:
    """The probed answer for 2024-10-11: ``NOT_AUTHORIZED`` and "this data timeframe"."""
    _vendor(outside_plan())

    with pytest.raises(PolygonOutsidePlanError, match="timeframe") as raised:
        await _fetch()

    assert isinstance(raised.value, PolygonEntitlementError)
    assert raised.value.status_code == 403


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(403, text="not entitled"),
        httpx.Response(403, text=""),
        httpx.Response(403, json={"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}),
        httpx.Response(403, json={"status": "ERROR", "message": "Your plan doesn't include this data timeframe."}),
        httpx.Response(403, json={"status": "NOT_AUTHORIZED"}),
        httpx.Response(403, json={"status": "NOT_AUTHORIZED", "message": 7}),
        httpx.Response(403, json=["NOT_AUTHORIZED", "data timeframe"]),
    ],
)
async def test_any_other_403_stays_a_plain_entitlement_error(body: httpx.Response) -> None:
    _vendor(body)

    with pytest.raises(PolygonEntitlementError) as raised:
        await _fetch()

    assert not isinstance(raised.value, PolygonOutsidePlanError)


@respx.mock
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={"status": "ERROR"}), PolygonAuthError),
        (httpx.Response(404), PolygonUnknownSymbolError),
        (httpx.Response(429), PolygonRateLimitedError),
        (httpx.Response(500, text="boom"), PolygonFetchError),
        (httpx.Response(502, text="<html>bad gateway</html>"), PolygonFetchError),
    ],
)
async def test_each_failed_status_keeps_its_own_error_type(response: httpx.Response, error: type[PolygonFetchError]) -> None:
    _vendor(response)

    with pytest.raises(error) as raised:
        await _fetch()

    assert raised.value.status_code == response.status_code


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("slow"),
        httpx.ConnectError("[Errno 61] Connection refused"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
)
async def test_a_request_that_fails_in_transit_is_a_typed_error_with_no_status(failure: Exception) -> None:
    _vendor(failure)

    with pytest.raises(PolygonFetchError) as raised:
        await _fetch()

    assert raised.value.status_code is None
