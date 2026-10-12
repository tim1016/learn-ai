"""The contract-listing fetcher: every page, or a typed error (#3030).

A listing is admitted to the lake only if it is whole, so the fetcher either
returns every contract the vendor lists or raises. The vendor is faked with
``respx``; no request leaves the machine. The key is a made-up sentinel and rides
in a header, never in a URL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
)
from app.data_lake.polygon_option_contracts import (
    PolygonContractRowError,
    PolygonOptionContract,
    fetch_option_contracts,
)
from app.data_lake.polygon_option_http import PolygonOutsidePlanError

SENTINEL_KEY = "sentinel-key-4f9a1c"

AS_OF = date(2025, 6, 13)
CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"
SECOND_PAGE = f"{CONTRACTS_URL}?cursor=page2"
THIRD_PAGE = f"{CONTRACTS_URL}?cursor=page3"


def _row(ticker: str, strike: float | int, **overrides: Any) -> dict[str, Any]:
    row = {
        "ticker": ticker,
        "underlying_ticker": "SPY",
        "contract_type": "call",
        "strike_price": strike,
        "expiration_date": "2025-06-13",
        "exercise_style": "american",
        "shares_per_contract": 100,
        "primary_exchange": "BATO",
        "cfi": "OCASPS",
    }
    row.update(overrides)
    return row


def _page(results: list[dict[str, Any]], next_url: str | None = None, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"status": "OK", "request_id": "r", "results": results, **extra}
    if next_url is not None:
        body["next_url"] = next_url
    return httpx.Response(200, json=body)


@respx.mock
async def test_every_page_is_followed_until_the_vendor_names_no_next_page() -> None:
    first = respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    second = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(
        return_value=_page([_row("O:SPY250613C00376000", 376)], next_url=THIRD_PAGE)
    )
    third = respx.get(CONTRACTS_URL, params={"cursor": "page3"}).mock(return_value=_page([_row("O:SPY250613C00377000", 377)]))

    contracts = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert [c.ticker for c in contracts] == ["O:SPY250613C00375000", "O:SPY250613C00376000", "O:SPY250613C00377000"]
    assert (first.call_count, second.call_count, third.call_count) == (1, 1, 1)


@respx.mock
async def test_the_query_is_as_of_the_date_with_no_expired_flag() -> None:
    """``as_of`` plus ``expired=true`` answers only already-expired contracts (probed 2026-10-12)."""
    route = respx.get(CONTRACTS_URL).mock(return_value=_page([_row("O:SPY250613C00375000", 375)]))

    await fetch_option_contracts("spy", AS_OF, SENTINEL_KEY)

    params = route.calls[0].request.url.params
    assert params["underlying_ticker"] == "SPY"
    assert params["as_of"] == "2025-06-13"
    assert params["limit"] == "1000"
    assert "expired" not in params


@respx.mock
async def test_a_later_page_keeps_the_vendors_cursor() -> None:
    """httpx replaces a URL's query when ``params`` is passed; a fetcher that did so would read page one again."""
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    second = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page([_row("O:SPY250613C00376000", 376)]))

    await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    asked = second.calls[0].request.url.params
    assert asked["cursor"] == "page2"
    assert "underlying_ticker" not in asked


@respx.mock
async def test_the_key_rides_in_a_header_on_every_page_and_in_no_url() -> None:
    first = respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    second = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page([_row("O:SPY250613C00376000", 376)]))

    await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    for route in (first, second):
        request = route.calls[0].request
        assert request.headers["Authorization"] == f"Bearer {SENTINEL_KEY}"
        assert SENTINEL_KEY not in str(request.url)
        assert "apikey" not in str(request.url).lower()


@respx.mock
async def test_a_strike_comes_back_as_the_exact_decimal_the_vendor_wrote() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            200,
            content=(
                b'{"status":"OK","results":['
                b'{"ticker":"O:SPY250613C00452500","contract_type":"call","strike_price":452.5,'
                b'"expiration_date":"2025-06-13","exercise_style":"american","shares_per_contract":100},'
                b'{"ticker":"O:SPY250613P00400000","contract_type":"put","strike_price":400,'
                b'"expiration_date":"2025-06-13","exercise_style":"american","shares_per_contract":100}]}'
            ),
            headers={"content-type": "application/json"},
        )
    )

    contracts = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert [c.strike for c in contracts] == [Decimal("452.5"), Decimal("400")]
    assert contracts[0] == PolygonOptionContract(
        ticker="O:SPY250613C00452500",
        contract_type="call",
        strike=Decimal("452.5"),
        expiration_date="2025-06-13",
        exercise_style="american",
        shares_per_contract=Decimal("100"),
        has_additional_underlyings=False,
    )


@respx.mock
async def test_additional_underlyings_are_noticed_and_a_missing_field_is_left_unknown() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=_page(
            [
                _row(
                    "O:SPY1250613C00375000",
                    375,
                    additional_underlyings=[{"amount": 50, "type": "equity", "underlying": "XYZ"}],
                ),
                {k: v for k, v in _row("O:SPY250613C00376000", 376).items() if k not in ("exercise_style", "shares_per_contract")},
            ]
        )
    )

    adjusted, bare = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert adjusted.has_additional_underlyings is True
    assert bare.has_additional_underlyings is False
    assert bare.exercise_style is None
    assert bare.shares_per_contract is None


@respx.mock
async def test_a_page_that_fails_part_way_is_an_error_never_a_shorter_listing() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=httpx.Response(502, text="bad gateway"))

    with pytest.raises(PolygonFetchError) as raised:
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert raised.value.status_code == 502


@respx.mock
async def test_a_rate_limit_on_a_later_page_keeps_its_own_type() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=httpx.Response(429))

    with pytest.raises(PolygonRateLimitedError):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_a_timeout_on_a_later_page_is_an_error() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(side_effect=httpx.ConnectTimeout("slow"))

    with pytest.raises(PolygonFetchError, match="timed out"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_the_keys_own_failures_keep_their_types() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(401, text="no"))
    with pytest.raises(PolygonAuthError):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(403, text="not entitled"))
    with pytest.raises(PolygonEntitlementError):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_a_page_that_does_not_confirm_ok_is_an_error() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ERROR", "error": "something broke", "results": []})
    )

    with pytest.raises(PolygonFetchError, match="status=ERROR"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_a_page_with_no_status_at_all_is_not_taken_as_a_complete_answer() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    with pytest.raises(PolygonFetchError, match="did not confirm"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_a_cursor_that_points_back_at_a_page_already_read_is_an_error() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page([_row("O:SPY250613C00376000", 376)], next_url=SECOND_PAGE))

    with pytest.raises(PolygonFetchError, match="looped"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
@pytest.mark.parametrize(
    "elsewhere_url",
    [
        "https://elsewhere.example/steal",
        "https://api.polygon.io.elsewhere.example/v3/reference/options/contracts?cursor=x",  # a lookalike host
        "https://api.polygon.io@elsewhere.example/v3/reference/options/contracts?cursor=x",  # polygon as the user name
        "http://api.polygon.io/v3/reference/options/contracts?cursor=x",  # the same host, downgraded to plain http
        "/v3/reference/options/contracts?cursor=x",  # a relative reference
    ],
)
async def test_a_next_page_that_leaves_polygon_is_refused_before_any_request_and_the_key_goes_nowhere_else(
    elsewhere_url: str,
) -> None:
    first = respx.get(CONTRACTS_URL).mock(return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=elsewhere_url))

    with pytest.raises(PolygonFetchError, match=r"not on api\.polygon\.io"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert first.call_count == 1
    assert [call.request.url.host for call in respx.calls] == ["api.polygon.io"]  # the one request, to Polygon


@respx.mock
async def test_a_redirect_to_another_host_is_an_error_and_the_key_is_not_forwarded() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://elsewhere.example/steal"})
    )
    elsewhere = respx.get("https://elsewhere.example/steal").mock(return_value=_page([]))

    with pytest.raises(PolygonFetchError):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert elsewhere.call_count == 0


@respx.mock
async def test_a_403_that_says_the_plan_lacks_the_data_timeframe_is_the_outside_plan_error() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            403,
            json={"status": "NOT_AUTHORIZED", "request_id": "r", "message": "Your plan doesn't include this data timeframe. Please upgrade your plan at https://polygon.io/pricing"},
        )
    )

    with pytest.raises(PolygonOutsidePlanError, match="timeframe"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_a_listing_that_pages_past_the_cap_is_an_error_and_stops_asking() -> None:
    """A chain is tens of pages; 200 distinct cursors in a row is not a chain."""
    def next_page(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("cursor", "0").removeprefix("p")) + 1
        return _page([_row(f"O:SPY250613C{number:08d}", number)], next_url=f"{CONTRACTS_URL}?cursor=p{number}")

    route = respx.get(CONTRACTS_URL).mock(side_effect=next_page)

    with pytest.raises(PolygonFetchError, match="passed 200 pages"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert route.call_count == 200


@respx.mock
async def test_a_listing_of_exactly_the_cap_is_read_whole() -> None:
    def next_page(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params.get("cursor", "0").removeprefix("p")) + 1
        more = f"{CONTRACTS_URL}?cursor=p{number}" if number < 200 else None
        return _page([_row(f"O:SPY250613C{number:08d}", number)], next_url=more)

    respx.get(CONTRACTS_URL).mock(side_effect=next_page)

    contracts = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert len(contracts) == 200


@respx.mock
@pytest.mark.parametrize(
    "broken",
    [
        None,  # a row that is not an object
        "O:SPY250613C00375000",
        ["O:SPY250613C00375000"],
        375,
        {"contract_type": "call", "strike_price": 375, "expiration_date": "2025-06-13"},  # no ticker
        {"ticker": "", "contract_type": "call", "strike_price": 375},
        {"ticker": "   ", "contract_type": "call", "strike_price": 375},
        {"ticker": None, "contract_type": "call", "strike_price": 375},
        {"ticker": 375, "contract_type": "call", "strike_price": 375},
    ],
)
async def test_a_row_that_is_no_object_or_has_no_ticker_is_an_error_naming_the_row_kind(broken: Any) -> None:
    respx.get(CONTRACTS_URL).mock(return_value=_page([_row("O:SPY250613C00376000", 376), broken]))

    with pytest.raises(PolygonContractRowError, match="contract row"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)


@respx.mock
async def test_a_row_with_a_ticker_is_always_kept_and_what_could_not_be_read_comes_back_none() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=_page([{"ticker": "O:SPY250613C00375000"}]))

    [bare] = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert bare == PolygonOptionContract(
        ticker="O:SPY250613C00375000", contract_type=None, strike=None, expiration_date=None,
        exercise_style=None, shares_per_contract=None, has_additional_underlyings=False,
    )


@respx.mock
@pytest.mark.parametrize("junk", [b"null", b'"375"', b"true", b"[375]", b'{"x": 1}', b"Infinity", b"-Infinity", b"NaN"])
async def test_a_strike_or_share_count_that_is_no_number_comes_back_none_and_does_not_raise(junk: bytes) -> None:
    """``Infinity`` and ``NaN`` are not JSON, but the parser reads them as floats; a float is never kept."""
    body = (
        b'{"status":"OK","results":[{"ticker":"O:SPY250613C00375000","contract_type":"call","strike_price":%s,'
        b'"expiration_date":"2025-06-13","exercise_style":"american","shares_per_contract":%s}]}'
    ) % (junk, junk)
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "application/json"}))

    [kept] = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert kept.strike is None and kept.shares_per_contract is None
    assert kept.contract_type == "call" and kept.exercise_style == "american"


@respx.mock
@pytest.mark.parametrize(
    ("value", "has_some"),
    [(None, False), ([], False), ([{"amount": 50, "type": "equity", "underlying": "XYZ"}], True), ({}, True), ("yes", True), (0, True)],
)
async def test_additional_underlyings_absent_or_an_empty_list_mean_none_anything_else_means_some(
    value: Any, has_some: bool
) -> None:
    row = _row("O:SPY250613C00375000", 375) if value is None else _row("O:SPY250613C00375000", 375, additional_underlyings=value)
    respx.get(CONTRACTS_URL).mock(return_value=_page([row]))

    [contract] = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert contract.has_additional_underlyings is has_some


@respx.mock
async def test_a_standard_row_as_the_vendor_sends_it_has_no_additional_underlyings_key() -> None:
    """The row exactly as probed on 2026-10-12: nine keys, no ``additional_underlyings``."""
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            200,
            content=(
                b'{"request_id":"r","status":"OK","results":[{"cfi":"OCASPS","contract_type":"call",'
                b'"exercise_style":"american","expiration_date":"2025-06-13","primary_exchange":"BATO",'
                b'"shares_per_contract":100,"strike_price":375,"ticker":"O:SPY250613C00375000","underlying_ticker":"SPY"}]}'
            ),
            headers={"content-type": "application/json"},
        )
    )

    [row] = await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)

    assert row == PolygonOptionContract(
        ticker="O:SPY250613C00375000", contract_type="call", strike=Decimal("375"), expiration_date="2025-06-13",
        exercise_style="american", shares_per_contract=Decimal("100"), has_additional_underlyings=False,
    )


@respx.mock
async def test_a_listing_with_no_contracts_comes_back_empty_for_the_caller_to_judge() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, json={"status": "OK", "request_id": "r"}))

    assert await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY) == []


@respx.mock
async def test_a_page_that_is_not_json_is_an_error() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(PolygonFetchError, match="not JSON"):
        await fetch_option_contracts("SPY", AS_OF, SENTINEL_KEY)
