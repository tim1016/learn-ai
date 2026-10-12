"""The contract-listing fetcher: every page, or a typed error (#3030).

A listing is admitted to the lake only if it is whole, so the fetcher either
returns every contract the vendor lists or raises. The vendor is faked with
``respx``; no request leaves the machine.
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
from app.data_lake.polygon_option_contracts import PolygonOptionContract, fetch_option_contracts

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

    contracts = await fetch_option_contracts("SPY", AS_OF, "test-key")

    assert [c.ticker for c in contracts] == ["O:SPY250613C00375000", "O:SPY250613C00376000", "O:SPY250613C00377000"]
    assert (first.call_count, second.call_count, third.call_count) == (1, 1, 1)


@respx.mock
async def test_the_query_is_as_of_the_date_with_no_expired_flag() -> None:
    """``as_of`` plus ``expired=true`` answers only already-expired contracts (probed 2026-10-12)."""
    route = respx.get(CONTRACTS_URL).mock(return_value=_page([_row("O:SPY250613C00375000", 375)]))

    await fetch_option_contracts("spy", AS_OF, "test-key")

    params = route.calls[0].request.url.params
    assert params["underlying_ticker"] == "SPY"
    assert params["as_of"] == "2025-06-13"
    assert params["limit"] == "1000"
    assert "expired" not in params


@respx.mock
async def test_a_later_page_keeps_the_vendors_cursor_and_adds_the_key() -> None:
    """httpx replaces a URL's query when ``params`` is passed; a fetcher that did so would read page one again."""
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    second = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page([_row("O:SPY250613C00376000", 376)]))

    await fetch_option_contracts("SPY", AS_OF, "test-key")

    asked = second.calls[0].request.url.params
    assert asked["cursor"] == "page2"
    assert asked["apiKey"] == "test-key"
    assert "underlying_ticker" not in asked


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

    contracts = await fetch_option_contracts("SPY", AS_OF, "test-key")

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

    adjusted, bare = await fetch_option_contracts("SPY", AS_OF, "test-key")

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
        await fetch_option_contracts("SPY", AS_OF, "test-key")

    assert raised.value.status_code == 502


@respx.mock
async def test_a_rate_limit_on_a_later_page_keeps_its_own_type() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=httpx.Response(429))

    with pytest.raises(PolygonRateLimitedError):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_a_timeout_on_a_later_page_is_an_error() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(side_effect=httpx.ConnectTimeout("slow"))

    with pytest.raises(PolygonFetchError, match="timed out"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_the_keys_own_failures_keep_their_types() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(401, text="no"))
    with pytest.raises(PolygonAuthError):
        await fetch_option_contracts("SPY", AS_OF, "test-key")

    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(403, text="not entitled"))
    with pytest.raises(PolygonEntitlementError):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_a_page_that_does_not_confirm_ok_is_an_error() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ERROR", "error": "something broke", "results": []})
    )

    with pytest.raises(PolygonFetchError, match="status=ERROR"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_a_page_with_no_status_at_all_is_not_taken_as_a_complete_answer() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    with pytest.raises(PolygonFetchError, match="did not confirm"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_a_cursor_that_points_back_at_a_page_already_read_is_an_error() -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page([_row("O:SPY250613C00376000", 376)], next_url=SECOND_PAGE))

    with pytest.raises(PolygonFetchError, match="looped"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_a_next_page_on_another_host_is_refused_and_the_key_is_not_sent_there() -> None:
    respx.get(CONTRACTS_URL).mock(
        return_value=_page([_row("O:SPY250613C00375000", 375)], next_url="https://elsewhere.example/steal")
    )
    elsewhere = respx.get("https://elsewhere.example/steal").mock(return_value=_page([]))

    with pytest.raises(PolygonFetchError, match=r"not on api\.polygon\.io"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")

    assert elsewhere.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    "broken",
    [
        {"contract_type": "call", "strike_price": 375, "expiration_date": "2025-06-13"},  # no ticker
        {"ticker": "O:SPY250613C00375000", "contract_type": "call", "expiration_date": "2025-06-13"},  # no strike
        {"ticker": "O:SPY250613C00375000", "contract_type": "call", "strike_price": 375},  # no expiry
        {"ticker": "O:SPY250613C00375000", "strike_price": 375, "expiration_date": "2025-06-13"},  # no type
    ],
)
async def test_a_contract_row_missing_what_the_file_needs_is_an_error(broken: dict[str, Any]) -> None:
    respx.get(CONTRACTS_URL).mock(return_value=_page([broken]))

    with pytest.raises(PolygonFetchError, match="contract row"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")


@respx.mock
async def test_a_listing_with_no_contracts_comes_back_empty_for_the_caller_to_judge() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, json={"status": "OK", "request_id": "r"}))

    assert await fetch_option_contracts("SPY", AS_OF, "test-key") == []


@respx.mock
async def test_a_page_that_is_not_json_is_an_error() -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(PolygonFetchError, match="not JSON"):
        await fetch_option_contracts("SPY", AS_OF, "test-key")
