"""The Options Backtest page's past-chain routes (#3030) over HTTP.

A small lake is built behind the fake catalog with the lake's own writers, the vendor is faked with ``respx``, and the
routes read it back. The happy-path reads also prove they run off the event loop: ``LakeRunData`` refuses a thread with
a running loop.

Hand-worked figures. Friday 2025-06-13 is a full session: New York midnight is 04:00 UTC = 1749787200000 ms, the open
is 13:30 UTC = 1749821400000 and the close is 20:00 UTC = 1749844800000, which is also that date's expiry anchor (16:00
EDT). The Friday after, 2025-06-20, anchors at 1750449600000 (seven days of 86 400 000 ms later) and is the third
Friday of June 2025 (the first is the 6th), so a monthly. The 13th is the second Friday: a weekly.

The underlying closed at 600.25 in minute 1, at 602.40 in minute 10 (13:40 UTC = 1749822000000) and at 602.60 in minute
390. The calls listed that day are 601, 602 and 603 and the puts 602 and 603, all expiring the 13th.
At minute 10, with the underlying at 602.40, the nearest strike is 602 for both rights (0.40 against 0.60 for 603).
Call 602 traded at minute 5 (close 3.10, 13:35 UTC = 1749821700000) and at minute 100 (close 2.90, 15:10 UTC =
1749827400000); call 603 was captured and never traded; call 601 was never captured; put 603 traded at minute 10 (4.40).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.data_lake import catalog_client
from app.main import app
from app.options.contract import OptionContract
from app.options_backtest import chain_download
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_vendor import no_trades
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_CLOSE_MS,
    FRIDAY_OPEN_MS,
    MINUTE_MS,
    StockFetchDouble,
    minute_bar,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    vendor_bars,
    vendor_listing_row,
    vendor_lists,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

PREFIX = "/api/research/options-backtest/past-chain"
FRIDAY_MIDNIGHT_MS = 1749787200000
NEXT_FRIDAY = date(2025, 6, 20)
NEXT_FRIDAY_ANCHOR_MS = 1750449600000
MINUTE_10_MS = FRIDAY_OPEN_MS + 10 * MINUTE_MS  # 13:40 UTC
SATURDAY_MIDNIGHT_MS = 1749873600000
LISTING_PATH = "option/usa/listing/spy/20250613_contracts.zip"

CALLS = {
    strike: OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=strike) for strike in (601, 602, 603)
}
PUTS = {strike: OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=strike) for strike in (602, 603)}
NEXT_CALL = OptionContract.of(root="SPY", expiry_date=NEXT_FRIDAY, right="call", strike=604)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _the_lake(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    await store_underlying_day(
        fake_catalog,
        tmp_lake,
        [
            stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25"),
            stock_bar(FRIDAY_OPEN_MS + 9 * MINUTE_MS, "602.00", "602.50", "601.90", "602.40"),
            stock_bar(FRIDAY_CLOSE_MS - MINUTE_MS, "602.50", "602.70", "602.40", "602.60"),
        ],
    )
    await store_listing(
        [
            *(vendor_listing_row(contract) for contract in (*CALLS.values(), *PUTS.values(), NEXT_CALL)),
        ]
    )
    await store_option_day(CALLS[602], [minute_bar(4, 3.00, 3.20, 2.95, 3.10), minute_bar(99, 2.80, 2.95, 2.75, 2.90)])
    await store_option_day(CALLS[603], [])
    await store_option_day(PUTS[603], [minute_bar(9, 4.30, 4.50, 4.25, 4.40)])


def _contracts(side: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {contract["strike_thousandths"]: contract for contract in side["contracts"]}


# ── GET /past-chain/day ───────────────────────────────────────────────────


@respx.mock
async def test_the_day_lists_each_expiry_with_its_tag_and_counts(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["underlying"], body["session_open_ms"], body["session_close_ms"]) == (
        "SPY",
        FRIDAY_OPEN_MS,
        FRIDAY_CLOSE_MS,
    )
    assert (body["listing_state"], body["listing_problem"]) == ("stored", None)
    assert [e["expiry_ms"] for e in body["expiries"]] == [FRIDAY_CLOSE_MS, NEXT_FRIDAY_ANCHOR_MS]
    today, next_friday = body["expiries"]
    assert (today["tag"]["series"], today["tag"]["expires_today"], today["tag"]["days_to_expiry"]) == (
        "weekly",
        True,
        0,
    )
    assert (
        next_friday["tag"]["series"],
        next_friday["tag"]["expires_today"],
        next_friday["tag"]["days_to_expiry"],
    ) == (
        "monthly",
        False,
        7,
    )
    assert today["tag"]["version"] == 1 and today["tag"]["evidence"]
    assert (today["calls"], today["puts"], today["standard_calls"], today["standard_puts"]) == (3, 2, 3, 2)
    assert (next_friday["calls"], next_friday["puts"]) == (1, 0)
    assert (body["rows_left_out_unusable"], body["rows_left_out_expired"]) == (0, 0)


async def test_any_instant_on_the_date_names_it_and_the_session_is_its_own(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    async with client as c:
        midnight = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})
        evening = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_CLOSE_MS})
        before = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS - 1})

    assert midnight.json()["session_open_ms"] == evening.json()["session_open_ms"] == FRIDAY_OPEN_MS
    assert before.json()["session_open_ms"] != FRIDAY_OPEN_MS  # a millisecond earlier is Thursday's session


async def test_a_date_with_no_stored_listing_says_not_stored_and_lists_nothing(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    assert response.status_code == 200
    assert response.json() | {"session_open_ms": 0, "session_close_ms": 0} == {
        "underlying": "SPY",
        "session_open_ms": 0,
        "session_close_ms": 0,
        "listing_state": "not_stored",
        "listing_problem": None,
        "expiries": [],
        "rows_left_out_unusable": 0,
        "rows_left_out_expired": 0,
    }


@respx.mock
async def test_a_stored_listing_the_lake_cannot_read_is_unreadable_with_its_reason_and_not_missing(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing([vendor_listing_row(CALLS[602])])
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), LISTING_PATH, b"admitted, but not a zip", admit=True)

    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    body = response.json()
    assert response.status_code == 200 and body["listing_state"] == "unreadable" and body["expiries"] == []
    assert "OptionListingFileError" in body["listing_problem"]


@pytest.mark.parametrize(
    ("trading_date_ms", "what"),
    [
        pytest.param(SATURDAY_MIDNIGHT_MS, "2025-06-14", id="a Saturday"),
        pytest.param(1766638800000, "2025-12-25", id="Christmas"),  # 2025-12-25T05:00Z is New York midnight (EST)
    ],
)
async def test_a_date_with_no_session_is_refused_with_a_plain_reason(
    client: httpx.AsyncClient, trading_date_ms: int, what: str
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": trading_date_ms})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_NOT_A_SESSION"
    assert what in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"trading_date_ms": FRIDAY_MIDNIGHT_MS}, id="no underlying"),
        pytest.param({"underlying": "SPY"}, id="no date"),
        pytest.param({"underlying": "spy", "trading_date_ms": FRIDAY_MIDNIGHT_MS}, id="lower case"),
        pytest.param({"underlying": "SPYSPYS", "trading_date_ms": FRIDAY_MIDNIGHT_MS}, id="seven letters"),
        pytest.param({"underlying": "BRK.B", "trading_date_ms": FRIDAY_MIDNIGHT_MS}, id="a dot"),
        pytest.param({"underlying": "SPY", "trading_date_ms": -1}, id="before the epoch"),
        pytest.param({"underlying": "SPY", "trading_date_ms": 2**63 - 1}, id="the int64 width, not the domain's end"),
        pytest.param({"underlying": "SPY", "trading_date_ms": "yesterday"}, id="not a number"),
    ],
)
async def test_a_day_request_that_is_malformed_is_the_standard_422(
    client: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/day", params=params)

    assert response.status_code == 422


async def test_a_catalog_that_cannot_be_reached_is_a_503_and_not_a_missing_listing(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file is there, so reading it means asking the catalog.
    with respx.mock:
        await store_listing([vendor_listing_row(CALLS[602])])

    async def down(*args: object, **kwargs: object) -> bool:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)

    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE"


# ── GET /past-chain/expiry ────────────────────────────────────────────────


def _expiry_params(**overrides: Any) -> dict[str, Any]:
    return {"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS, "expiry_ms": FRIDAY_CLOSE_MS} | overrides


@respx.mock
async def test_one_expiry_at_an_instant_has_the_underlyings_price_the_money_and_each_contracts_last_trade(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=1))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["underlying"], body["expiry_ms"], body["at_ms"], body["strikes_each_side"]) == (
        "SPY",
        FRIDAY_CLOSE_MS,
        MINUTE_10_MS,
        1,
    )
    assert (body["session_open_ms"], body["session_close_ms"]) == (FRIDAY_OPEN_MS, FRIDAY_CLOSE_MS)
    assert (body["listing_state"], body["expiry_listed"]) == ("stored", True)
    assert body["tag"]["series"] == "weekly" and body["tag"]["expires_today"] is True
    # The close of the bar that closed at minute 10 is 602.40, with that bar's close time: exactly the instant.
    assert body["underlying_price"] == {
        "state": "priced",
        "price": "602.4",
        "price_time_ms": MINUTE_10_MS,
        "problem": None,
    }
    calls, puts = body["calls"], body["puts"]
    assert (calls["right"], calls["listed"], calls["standard_listed"]) == ("call", 3, 3)
    assert (calls["at_the_money_strike"], calls["at_the_money_strike_thousandths"]) == ("602", 602000)
    assert (puts["at_the_money_strike"], puts["at_the_money_strike_thousandths"]) == ("602", 602000)
    # One strike each side of 602 on calls 601 602 603; on puts 602 603 only, as 601 is not listed.
    assert sorted(_contracts(calls)) == [601000, 602000, 603000] and sorted(_contracts(puts)) == [602000, 603000]
    by_strike = _contracts(calls)
    assert by_strike[601000]["price_state"] == "not_stored" and by_strike[601000]["price"] is None
    assert by_strike[602000]["symbol"] == "SPY250613C00602000"
    assert (by_strike[602000]["price_state"], by_strike[602000]["price"], by_strike[602000]["price_time_ms"]) == (
        "traded",
        "3.1",
        1749821700000,
    )
    assert by_strike[602000]["steps_from_the_money"] == 0 and by_strike[603000]["steps_from_the_money"] == 1
    assert by_strike[603000]["price_state"] == "did_not_trade" and by_strike[602000]["standard"] is True
    assert by_strike[602000]["strike"] == "602"
    assert (_contracts(puts)[603000]["price"], _contracts(puts)[603000]["price_time_ms"]) == ("4.4", MINUTE_10_MS)
    assert _contracts(puts)[602000]["price_state"] == "not_stored"


@respx.mock
async def test_prices_and_strikes_are_strings_of_exact_dollars_and_times_are_integers(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=1))

    body = response.json()
    traded = _contracts(body["calls"])[602000]
    assert isinstance(traded["price"], str) and isinstance(traded["strike"], str)
    assert isinstance(body["underlying_price"]["price"], str) and isinstance(body["calls"]["at_the_money_strike"], str)
    for value in (body["at_ms"], body["expiry_ms"], traded["price_time_ms"], traded["strike_thousandths"]):
        assert type(value) is int


@respx.mock
async def test_the_default_instant_is_the_close_and_the_last_trade_is_the_one_by_then(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(strikes=0))

    body = response.json()
    assert body["at_ms"] == FRIDAY_CLOSE_MS
    # At the close the underlying is 602.60 and the nearest call strike is 603 (0.40 away, against 0.60 for 602).
    assert body["underlying_price"]["price"] == "602.6" and body["underlying_price"]["price_time_ms"] == FRIDAY_CLOSE_MS
    assert body["calls"]["at_the_money_strike_thousandths"] == 603000
    assert [c["strike_thousandths"] for c in body["calls"]["contracts"]] == [603000]


@respx.mock
async def test_the_trading_date_and_the_expiry_are_two_inputs_and_neither_moves_the_other(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        same_day = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS))
        later = await c.get(
            f"{PREFIX}/expiry", params=_expiry_params(expiry_ms=NEXT_FRIDAY_ANCHOR_MS, at_ms=MINUTE_10_MS)
        )

    assert (same_day.json()["session_open_ms"], later.json()["session_open_ms"]) == (FRIDAY_OPEN_MS, FRIDAY_OPEN_MS)
    assert (same_day.json()["expiry_ms"], later.json()["expiry_ms"]) == (FRIDAY_CLOSE_MS, NEXT_FRIDAY_ANCHOR_MS)
    assert later.json()["tag"]["series"] == "monthly" and later.json()["tag"]["days_to_expiry"] == 7
    assert [c["symbol"] for c in later.json()["calls"]["contracts"]] == ["SPY250620C00604000"]


@respx.mock
async def test_an_expiry_the_listing_does_not_hold_is_answered_not_listed_and_not_refused(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)
    monday_anchor = 1750104000000  # 2025-06-16 20:00 UTC (16:00 EDT), a session that the listing does not hold

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(expiry_ms=monday_anchor))

    body = response.json()
    assert response.status_code == 200 and body["expiry_listed"] is False and body["tag"] is None
    assert body["calls"]["contracts"] == [] and body["calls"]["listed"] == 0


async def test_a_date_with_nothing_stored_answers_with_each_thing_missing_named(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params())

    body = response.json()
    assert response.status_code == 200
    assert (body["listing_state"], body["expiry_listed"], body["tag"]) == ("not_stored", None, None)
    assert body["underlying_price"]["state"] == "not_stored" and body["underlying_price"]["price"] is None
    assert body["calls"]["contracts"] == [] and body["puts"]["contracts"] == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"trading_date_ms": SATURDAY_MIDNIGHT_MS}, "OPTIONS_BACKTEST_NOT_A_SESSION", id="not a session"),
        pytest.param(
            {"expiry_ms": FRIDAY_MIDNIGHT_MS}, "OPTIONS_BACKTEST_NOT_AN_EXPIRY", id="an expiry that is midnight"
        ),
        pytest.param(
            {"expiry_ms": FRIDAY_CLOSE_MS + 1}, "OPTIONS_BACKTEST_NOT_AN_EXPIRY", id="a millisecond off the anchor"
        ),
        pytest.param({"at_ms": FRIDAY_OPEN_MS - 1}, "OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION", id="before the open"),
        pytest.param({"at_ms": FRIDAY_CLOSE_MS + 1}, "OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION", id="after the close"),
        pytest.param({"at_ms": 0}, "OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION", id="the epoch"),
    ],
)
async def test_an_expiry_request_the_page_should_not_have_made_is_refused_with_a_code_and_a_reason(
    client: httpx.AsyncClient, overrides: dict[str, Any], code: str
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(**overrides))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == code and response.json()["detail"]["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"strikes": 15}, id="more strikes than the cap"),
        pytest.param({"strikes": -1}, id="fewer than none"),
        pytest.param({"strikes": "many"}, id="not a number"),
        pytest.param({"at_ms": -1}, id="a negative instant"),
        pytest.param({"underlying": "spy"}, id="lower case"),
    ],
)
async def test_an_expiry_request_that_is_malformed_is_the_standard_422(
    client: httpx.AsyncClient, overrides: dict[str, Any]
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(**overrides))

    assert response.status_code == 422


async def test_a_window_of_fourteen_strikes_is_allowed_and_fifteen_is_not(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    async with client as c:
        widest = await c.get(f"{PREFIX}/expiry", params=_expiry_params(strikes=14))
        wider = await c.get(f"{PREFIX}/expiry", params=_expiry_params(strikes=15))

    assert (widest.status_code, wider.status_code) == (200, 422)


async def test_an_expiry_read_with_the_catalog_down_is_a_503(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with respx.mock:
        await _the_lake(fake_catalog, tmp_lake)

    async def down(*args: object, **kwargs: object) -> bool:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE"


# ── POST /past-chain/download ─────────────────────────────────────────────


@pytest.fixture
def stock_fetch(fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch) -> StockFetchDouble:
    double = StockFetchDouble(fake_catalog, tmp_lake)
    monkeypatch.setattr(chain_download, "materialize_engine_run", double)
    return double


def _download(**overrides: Any) -> dict[str, Any]:
    return {
        "underlying": "SPY",
        "trading_date_ms": FRIDAY_MIDNIGHT_MS,
        "expiry_ms": FRIDAY_CLOSE_MS,
        "contracts": ["SPY250613C00602000", "SPY250613C00603000"],
    } | overrides


@respx.mock
async def test_a_download_reports_one_item_each_and_what_it_captured_can_then_be_read_back(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch: StockFetchDouble
) -> None:
    vendor_lists(*CALLS.values(), *PUTS.values())
    bars = vendor_bars(lambda symbol: no_trades(symbol))

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download())
        after = await c.get(f"{PREFIX}/expiry", params=_expiry_params(strikes=2))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["underlying"], body["session_open_ms"], body["session_close_ms"]) == (
        "SPY",
        FRIDAY_OPEN_MS,
        FRIDAY_CLOSE_MS,
    )
    assert [(i["kind"], i["subject"], i["outcome"], i["reason"], i["detail"]) for i in body["items"]] == [
        ("listing", "SPY", "fetched", None, None),
        ("underlying", "SPY", "fetched", None, None),
        ("contract", "SPY250613C00602000", "fetched", None, None),
        ("contract", "SPY250613C00603000", "fetched", None, None),
    ]
    assert bars.call_count == 2 and len(stock_fetch.calls) == 1
    # What was captured is now there to read: the chain no longer says nothing is stored.
    assert after.json()["listing_state"] == "stored"
    assert after.json()["underlying_price"]["state"] == "priced"
    assert {c["strike_thousandths"]: c["price_state"] for c in after.json()["calls"]["contracts"]} == {
        601000: "not_stored",  # listed, and not one of the two named
        602000: "did_not_trade",  # captured, and the vendor said nothing traded
        603000: "did_not_trade",
    }


@respx.mock
async def test_a_failure_of_the_vendor_is_an_item_with_a_reason_and_the_request_still_succeeds(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch: StockFetchDouble
) -> None:
    vendor_lists(CALLS[602])
    vendor_bars(lambda symbol: httpx.Response(429, json={"status": "ERROR"}))

    async with client as c:
        response = await c.post(
            f"{PREFIX}/download", json=_download(contracts=["SPY250613C00602000", "SPY250613C00603000"])
        )

    assert response.status_code == 200
    contract_items = [i for i in response.json()["items"] if i["kind"] == "contract"]
    assert [(i["subject"], i["outcome"], i["reason"]) for i in contract_items] == [
        ("SPY250613C00602000", "failed", "provider_rate_limited"),
        ("SPY250613C00603000", "failed", "not_listed"),
    ]
    assert all(i["detail"] for i in contract_items)


@respx.mock
async def test_a_download_with_no_contracts_or_expiry_captures_the_listing_and_the_underlying(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch: StockFetchDouble
) -> None:
    vendor_lists(CALLS[602])

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    assert response.status_code == 200
    assert [(i["kind"], i["outcome"]) for i in response.json()["items"]] == [
        ("listing", "fetched"),
        ("underlying", "fetched"),
    ]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"trading_date_ms": SATURDAY_MIDNIGHT_MS}, "OPTIONS_BACKTEST_NOT_A_SESSION", id="not a session"),
        pytest.param(
            {"expiry_ms": FRIDAY_MIDNIGHT_MS}, "OPTIONS_BACKTEST_NOT_AN_EXPIRY", id="an expiry that is no anchor"
        ),
        pytest.param({"contracts": ["SPY"]}, "OPTIONS_BACKTEST_NOT_A_CONTRACT", id="a contract that is no OCC symbol"),
        pytest.param(
            {"contracts": ["O:SPY250613C00602000"]}, "OPTIONS_BACKTEST_NOT_A_CONTRACT", id="the vendor's spelling"
        ),
        pytest.param(
            {"contracts": ["SPY250620C00604000"]},
            "OPTIONS_BACKTEST_CONTRACT_NOT_OF_EXPIRY",
            id="a contract of another expiry",
        ),
        pytest.param(
            {"contracts": [f"SPY250613C{strike * 1000:08d}" for strike in range(400, 461)]},
            "OPTIONS_BACKTEST_TOO_MANY_CONTRACTS",
            id="sixty-one contracts",
        ),
    ],
)
@respx.mock
async def test_a_download_the_page_should_not_have_asked_for_is_refused_before_anything_is_fetched(
    client: httpx.AsyncClient, stock_fetch: StockFetchDouble, overrides: dict[str, Any], code: str
) -> None:
    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download(**overrides))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == code and response.json()["detail"]["message"]
    assert stock_fetch.calls == []  # and an un-mocked request to the vendor would have failed the test


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"trading_date_ms": FRIDAY_MIDNIGHT_MS}, id="no underlying"),
        pytest.param({"underlying": "SPY"}, id="no date"),
        pytest.param({"underlying": "spy", "trading_date_ms": FRIDAY_MIDNIGHT_MS}, id="lower case"),
        pytest.param({"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS, "extra": 1}, id="an unknown field"),
        pytest.param({"underlying": "SPY", "trading_date_ms": 2**63 - 1}, id="beyond the domain's last instant"),
        pytest.param(
            {"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS, "contracts": "SPY250613C00602000"},
            id="one string",
        ),
    ],
)
@respx.mock
async def test_a_malformed_download_body_is_the_standard_422(
    client: httpx.AsyncClient, stock_fetch: StockFetchDouble, body: dict[str, Any]
) -> None:
    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=body)

    assert response.status_code == 422 and stock_fetch.calls == []


@respx.mock
async def test_a_download_with_the_catalog_down_is_a_503(
    client: httpx.AsyncClient,
    stock_fetch: StockFetchDouble,
    monkeypatch: pytest.MonkeyPatch,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
) -> None:
    async def down(*args: object, **kwargs: object) -> int:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", down)
    vendor_lists(CALLS[602])

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE"


# ── The published contract ────────────────────────────────────────────────

PAST_CHAIN_MODELS = {
    "PastChainContract",
    "PastChainDayResponse",
    "PastChainDownloadItem",
    "PastChainDownloadRequest",
    "PastChainDownloadResponse",
    "PastChainExpiryResponse",
    "PastChainExpirySummary",
    "PastChainExpiryTag",
    "PastChainSide",
    "PastChainUnderlyingPrice",
}


def test_every_model_the_routes_serve_is_in_the_contract_once_under_its_own_name() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert set(schemas) >= PAST_CHAIN_MODELS
    # A name two served models share is renamed by FastAPI with its module path; none of ours may be.
    assert [name for name in schemas if "PastChain" in name and name not in PAST_CHAIN_MODELS] == []
    assert [name for name in schemas if "__" in name and name.rsplit("__", 1)[-1] in PAST_CHAIN_MODELS] == []


def test_the_three_routes_are_served_under_the_setup_routes_prefix() -> None:
    paths = app.openapi()["paths"]

    assert set(paths[f"{PREFIX}/day"]) == {"get"} and set(paths[f"{PREFIX}/expiry"]) == {"get"}
    assert set(paths[f"{PREFIX}/download"]) == {"post"}


def test_the_routes_publish_the_refusal_body_the_setup_routes_already_use() -> None:
    schemas = app.openapi()["components"]["schemas"]
    day = app.openapi()["paths"][f"{PREFIX}/day"]["get"]["responses"]

    assert day["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OptionsBacktestRefusalBody"
    }
    assert "OptionsBacktestRefusalBody" in schemas


def test_every_timestamp_field_is_bounded_by_the_domains_last_instant_and_not_the_int64_width() -> None:
    from app.utils.session_anchors import MAX_TIMESTAMP_MS

    schemas = app.openapi()["components"]["schemas"]
    ms_fields = [
        (name, field, definition)
        for name in PAST_CHAIN_MODELS
        for field, definition in schemas[name].get("properties", {}).items()
        if field.endswith("_ms")
    ]

    assert ms_fields
    for name, field, definition in ms_fields:
        options = definition.get("anyOf", [definition])
        integer = next(option for option in options if option.get("type") == "integer")
        assert integer["maximum"] == MAX_TIMESTAMP_MS, (name, field)


def test_the_underlying_pattern_is_the_setups_own() -> None:
    from app.options_backtest.setup import OptionsBacktestSetup
    from app.schemas.options_backtest_chain import UNDERLYING_PATTERN

    pattern = next(m.pattern for m in OptionsBacktestSetup.model_fields["underlying"].metadata if hasattr(m, "pattern"))

    assert pattern == UNDERLYING_PATTERN
