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

The wide scene: standard SPY calls and puts at every dollar from 570 to 639 expiring that Friday, the underlying at 602.40
at minute 10. The money is 602 (0.40 away, against 0.60 for 603). 14 each side is 588 to 616, 29 strikes a right, 58
standard contracts; 13 each side is 589 to 615, 27 a right, 54. Non-standard calls (ten shares) at 601.5, 602.5 and 603.5
all lie inside both windows.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.broker.fleet.errors import DataPlaneControlSecretRefused, FleetControlPlaneNotInstalled
from app.config import settings
from app.data_lake import catalog_client, option_lake
from app.data_lake.option_listing import OptionListingFileError
from app.main import app
from app.options.contract import OptionContract
from app.options_backtest import chain_download
from app.options_backtest.data_check import lake_reason_words
from app.security.data_plane_control import CONTROL_SECRET_ENV_VAR, CONTROL_SECRET_HEADER
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_lake_cases import DISK_FULL_AT_THE_RENAME, install_write_fault
from tests._helpers.option_vendor import no_trades
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_CLOSE_MS,
    FRIDAY_OPEN_MS,
    MINUTE_MS,
    StockFetchDouble,
    minute_bar,
    option_day_zip,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    stock_day_zip,
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
LIMITS = "/api/research/options-backtest/limits"
FRIDAY_MIDNIGHT_MS = 1749787200000
NEXT_FRIDAY = date(2025, 6, 20)
NEXT_FRIDAY_ANCHOR_MS = 1750449600000
MINUTE_10_MS = FRIDAY_OPEN_MS + 10 * MINUTE_MS  # 13:40 UTC
SATURDAY_MIDNIGHT_MS = 1749873600000
# 2026-10-12 (a Monday) at New York midnight, 00:00 EDT = 04:00 UTC: 2026-01-01 is day 20454 (20089 + 365), the 12th of
# October is 284 days on, day 20738, and 20738 * 86 400 000 = 1791763200000 plus four hours (14 400 000). The tests' fixed
# clock, 1791828000000, is 14 hours later: 14:00 New York that day, so its session has not closed.
TODAY_MIDNIGHT_MS = 1791777600000
LISTING_PATH = "option/usa/listing/spy/20250613_contracts.zip"
STOCK_PATH = "equity/usa/minute/spy/20250613_trade.zip"
BARS_PATH_602 = "option/usa/minute/spy/spy250613c00602000/20250613_trade.zip"

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
        "fetch_refusal": None,
    }


# The lake takes a session's option data from the New York midnight after its date. Friday 2025-06-13 is taken from
# Saturday's midnight, 2025-06-14 00:00 EDT = 04:00 UTC = 1749873600000 ms (SATURDAY_MIDNIGHT_MS).


@pytest.mark.parametrize(
    ("now_ms", "refused"),
    [
        pytest.param(SATURDAY_MIDNIGHT_MS - 1, True, id="one millisecond before the lake takes the session"),
        pytest.param(SATURDAY_MIDNIGHT_MS, False, id="at the millisecond it takes it"),
    ],
)
async def test_the_day_carries_the_refusal_until_the_millisecond_the_lake_takes_the_session_and_not_after(
    client: httpx.AsyncClient,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    monkeypatch: pytest.MonkeyPatch,
    now_ms: int,
    refused: bool,
) -> None:
    # The route asks the lake's own clock, the one a download takes when it is given none.
    monkeypatch.setattr(option_lake, "current_ms", lambda: now_ms)

    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    assert response.status_code == 200, response.text
    expected = {"reason": "session_not_over", "words": lake_reason_words("session_not_over")}
    assert response.json()["fetch_refusal"] == (expected if refused else None)


async def test_the_day_of_a_session_long_over_has_no_refusal_and_todays_has_one(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The tests' clock is 2026-10-12 14:00 New York: 2025-06-13 is over, and 2026-10-12 is a session not yet over.
    async with client as c:
        long_over = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})
        today = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": TODAY_MIDNIGHT_MS})

    assert long_over.json()["fetch_refusal"] is None
    assert today.json()["fetch_refusal"]["reason"] == "session_not_over"
    # Everything else of the answer is what it was for a day with nothing stored.
    assert today.json()["listing_state"] == "not_stored" and today.json()["expiries"] == []


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


@respx.mock
async def test_a_download_of_a_session_that_is_not_over_says_why_for_the_listing_and_the_underlying_and_fetches_nothing(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch: StockFetchDouble
) -> None:
    async with client as c:
        response = await c.post(
            f"{PREFIX}/download", json={"underlying": "SPY", "trading_date_ms": TODAY_MIDNIGHT_MS}
        )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(i["kind"], i["outcome"], i["reason"]) for i in items] == [
        ("listing", "failed", "session_not_over"),
        ("underlying", "failed", "session_not_over"),
    ]
    assert all(i["detail"] for i in items)
    assert stock_fetch.calls == []  # and an un-mocked request to the vendor would have failed the test


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
        pytest.param(
            {"at_ms": MINUTE_10_MS + 30_000},
            "OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE",
            id="half way through a minute",
        ),
        pytest.param(
            {"at_ms": MINUTE_10_MS + 1}, "OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE", id="a millisecond past a minute"
        ),
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


async def test_the_most_strikes_each_side_a_read_takes_is_the_limit_served_the_limit_is_allowed_and_one_more_is_not(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    async with client as c:
        limit = (await c.get(LIMITS)).json()["max_strikes_each_side"]
        at_the_limit = await c.get(f"{PREFIX}/expiry", params=_expiry_params(strikes=limit))
        one_more = await c.get(f"{PREFIX}/expiry", params=_expiry_params(strikes=limit + 1))

    # 14 strikes each side is 29 strikes a right, 58 standard contracts, under the 60 a window may hold.
    assert limit == 14
    assert (at_the_limit.status_code, one_more.status_code) == (200, 422)


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


@respx.mock
async def test_an_instant_on_the_next_whole_minute_is_answered_and_the_price_is_the_last_bar_closing_by_it(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS + MINUTE_MS))

    # Minute 11 is a whole minute: 602.40 is still the last close (minute 10's bar), with that bar's time.
    assert response.status_code == 200 and response.json()["at_ms"] == MINUTE_10_MS + MINUTE_MS
    assert response.json()["underlying_price"]["price_time_ms"] == MINUTE_10_MS


@respx.mock
async def test_the_default_window_is_ten_strikes_each_side_of_the_money(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_underlying_day(
        fake_catalog,
        tmp_lake,
        [stock_bar(FRIDAY_OPEN_MS + 9 * MINUTE_MS, "602.00", "602.50", "601.90", "602.40")],
    )
    await store_listing(
        [
            vendor_listing_row(OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=strike))
            for strike in range(580, 621)
        ]
    )

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS))

    body = response.json()
    # Calls at 580 to 620; the money is 602, so ten each side is 592 to 612: 21 contracts.
    assert body["strikes_each_side"] == 10
    assert [c["strike_thousandths"] for c in body["calls"]["contracts"]] == [k * 1000 for k in range(592, 613)]


@respx.mock
async def test_the_two_counts_of_rows_left_out_are_each_on_their_own_field(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    yesterday = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 12), right="call", strike=600)
    adjusted_root = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=604)
    no_share_count = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=605)
    await store_listing(
        [
            *(vendor_listing_row(contract) for contract in CALLS.values()),
            # Expired the day before the trading date: listed, and left out of every count (1).
            vendor_listing_row(yesterday),
            # A ticker no contract can be named from (an adjusted root carries a digit) and a row with no share count:
            # stored, and not turned into contracts (2).
            vendor_listing_row(adjusted_root, ticker="O:SPY1250613C00604000"),
            vendor_listing_row(no_share_count, shares_per_contract=None),
        ]
    )

    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    body = response.json()
    assert (body["rows_left_out_unusable"], body["rows_left_out_expired"]) == (2, 1)
    assert [(e["expiry_ms"], e["calls"]) for e in body["expiries"]] == [(FRIDAY_CLOSE_MS, 3)]


# ── The first hours of 1970 are the last session of 1969 ──────────────────


@pytest.mark.parametrize("trading_date_ms", [0, 17_999_999])
@respx.mock
async def test_a_date_in_the_first_hours_of_1970_is_refused_on_all_three_routes_before_anything_is_read_or_fetched(
    client: httpx.AsyncClient, stock_fetch: StockFetchDouble, trading_date_ms: int
) -> None:
    async with client as c:
        day = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": trading_date_ms})
        expiry = await c.get(f"{PREFIX}/expiry", params=_expiry_params(trading_date_ms=trading_date_ms))
        download = await c.post(f"{PREFIX}/download", json=_download(trading_date_ms=trading_date_ms))

    for response in (day, expiry, download):
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_NOT_A_SESSION"
        assert "1969-12-31" in response.json()["detail"]["message"]
    # No vendor request (an unmocked one fails the test) and no fetch of the underlying.
    assert stock_fetch.calls == []


async def test_the_first_session_after_the_epoch_is_answered(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # 1970-01-02 New York midnight is 104 400 000 ms; the session opens at 10:00 EST = 15:00 UTC = 140 400 000 ms.
    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": 104_400_000})

    assert response.status_code == 200 and response.json()["session_open_ms"] == 140_400_000


# ── Nothing on the wire names a file on the server ────────────────────────

_GARBAGE = b"admitted, but not a zip"
_CHANGED = b"changed after its receipt"


def _put_a_file_where_the_underlying_was(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    (raw_root(tmp_lake) / STOCK_PATH).unlink()
    (raw_root(tmp_lake) / STOCK_PATH).mkdir()


def _stamp_a_bar_off_the_minute_on_the_underlying(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    # The stored day has a bar starting at 09:39:30, 1749821400000 + 9 * 60 000 + 30 000 = 1749821970000, which closes
    # 30 000 ms past a whole minute.
    bars = [
        stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25"),
        stock_bar(FRIDAY_OPEN_MS + 9 * MINUTE_MS + 30_000, "602.00", "602.50", "601.90", "602.40"),
    ]
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_PATH, stock_day_zip(bars), admit=True)


def _stamp_a_bar_off_the_minute_on_call_602(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    # Call 602's stored day has a bar starting at 11:09:30 (minute 99 plus 30 000 ms), which closes off a whole minute.
    starts = [FRIDAY_OPEN_MS + 4 * MINUTE_MS, FRIDAY_OPEN_MS + 99 * MINUTE_MS + 30_000]
    replace_the_stored_file(
        fake_catalog, raw_root(tmp_lake), BARS_PATH_602, option_day_zip(CALLS[602], starts), admit=True
    )


def _stamp_a_bar_closing_on_another_day_on_call_602(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    # Friday's New York midnight is 04:00 UTC, 34 200 000 ms before the 09:30 open (1749821400000 - 34 200 000 =
    # 1749787200000). A bar starting at 23:59 starts inside Friday, so the lake's parser accepts it, and closes 60 000
    # later at Saturday's midnight: 1749787200000 + 23 * 3 600 000 + 59 * 60 000 = 1749873540000 for the start.
    starts = [FRIDAY_OPEN_MS + 4 * MINUTE_MS, 1749873540000]
    replace_the_stored_file(
        fake_catalog, raw_root(tmp_lake), BARS_PATH_602, option_day_zip(CALLS[602], starts), admit=True
    )


def _replace(path: str, payload: bytes, *, admit: bool):
    def damage(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
        replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, payload, admit=admit)

    return damage


def _listing_problem(body: dict[str, Any]) -> str | None:
    return body["listing_problem"]


def _underlying_problem(body: dict[str, Any]) -> str | None:
    return body["underlying_price"]["problem"]


def _contract_problem(body: dict[str, Any]) -> str | None:
    return _contracts(body["calls"])[602000]["problem"]


# (how the stored file is spoilt, where the problem is on the wire, what it must name, the kind of error it must give)
_UNREADABLE_ON_THE_EXPIRY = [
    pytest.param(
        _replace(LISTING_PATH, _GARBAGE, admit=True),
        _listing_problem,
        "SPY listing",
        "OptionListingFileError",
        id="a damaged listing",
    ),
    pytest.param(
        _replace(LISTING_PATH, _CHANGED, admit=False),
        _listing_problem,
        "SPY listing",
        "LakeAdmissionError",
        id="a listing the catalog does not admit",
    ),
    pytest.param(
        _replace(STOCK_PATH, _GARBAGE, admit=True),
        _underlying_problem,
        "SPY minute bars",
        "UnderlyingDayUnreadable",
        id="a damaged underlying",
    ),
    pytest.param(
        _replace(STOCK_PATH, _CHANGED, admit=False),
        _underlying_problem,
        "SPY minute bars",
        "LakeAdmissionError",
        id="an underlying the catalog does not admit",
    ),
    pytest.param(
        _put_a_file_where_the_underlying_was,
        _underlying_problem,
        "SPY minute bars",
        "UnderlyingDayUnreadable",
        id="a directory where the underlying was",
    ),
    pytest.param(
        _replace(BARS_PATH_602, _GARBAGE, admit=True),
        _contract_problem,
        "SPY250613C00602000",
        "OptionBarsFileError",
        id="damaged bars of a contract",
    ),
    pytest.param(
        _replace(BARS_PATH_602, _CHANGED, admit=False),
        _contract_problem,
        "SPY250613C00602000",
        "LakeAdmissionError",
        id="bars the catalog does not admit",
    ),
    pytest.param(
        _stamp_a_bar_off_the_minute_on_the_underlying,
        _underlying_problem,
        "SPY minute bars",
        "UnderlyingBarsCannotBePlaced",
        id="an underlying day with a bar off the minute",
    ),
    pytest.param(
        _stamp_a_bar_off_the_minute_on_call_602,
        _contract_problem,
        "SPY250613C00602000",
        "OptionBarsFileError",  # the lake's own parser refuses a start off a whole minute, before the reader's guard
        id="a contract day with a bar off the minute",
    ),
    pytest.param(
        _stamp_a_bar_closing_on_another_day_on_call_602,
        _contract_problem,
        "SPY250613C00602000",
        "OptionBarsCannotBePlaced",
        id="a contract day with a bar closing on another day",
    ),
]


@pytest.mark.parametrize(("damage", "where", "subject", "kind"), _UNREADABLE_ON_THE_EXPIRY)
@respx.mock
async def test_what_the_expiry_cannot_read_is_told_on_the_wire_by_what_and_why_and_names_no_file(
    client: httpx.AsyncClient,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    damage: Any,
    where: Any,
    subject: str,
    kind: str,
) -> None:
    await _the_lake(fake_catalog, tmp_lake)
    damage(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=1))

    assert response.status_code == 200, response.text
    problem = where(response.json())
    assert problem is not None and subject in problem and "2025-06-13" in problem and kind in problem
    assert str(tmp_lake) not in response.text and ".zip" not in response.text


@pytest.mark.parametrize(
    ("payload", "admit", "kind"),
    [
        pytest.param(_GARBAGE, True, "OptionListingFileError", id="a damaged listing"),
        pytest.param(_CHANGED, False, "LakeAdmissionError", id="a listing the catalog does not admit"),
    ],
)
@respx.mock
async def test_what_the_day_cannot_read_is_told_on_the_wire_by_what_and_why_and_names_no_file(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, payload: bytes, admit: bool, kind: str
) -> None:
    await store_listing([vendor_listing_row(CALLS[602])])
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), LISTING_PATH, payload, admit=admit)

    async with client as c:
        response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})

    problem = response.json()["listing_problem"]
    assert response.status_code == 200 and "SPY listing" in problem and "2025-06-13" in problem and kind in problem
    assert str(tmp_lake) not in response.text and ".zip" not in response.text


@pytest.mark.parametrize("route", ["day", "expiry", "download"])
async def test_a_catalog_that_cannot_be_reached_is_told_without_the_file_it_was_reading(
    client: httpx.AsyncClient,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    stock_fetch: StockFetchDouble,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    with respx.mock:
        await store_listing([vendor_listing_row(CALLS[602])])
    # The lake's own message for this: the path of the file it was admitting, then the reason.
    why = f"{raw_root(tmp_lake)}/{LISTING_PATH}: cannot verify committed lake receipt (OSError)"

    async def down(*args: object, **kwargs: object) -> Any:
        raise catalog_client.CatalogUnavailableError(why)

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)
    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", down)

    async with client as c:
        if route == "day":
            response = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})
        elif route == "expiry":
            response = await c.get(f"{PREFIX}/expiry", params=_expiry_params())
        else:
            with respx.mock:
                vendor_lists(CALLS[602])
                response = await c.post(f"{PREFIX}/download", json=_download())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE"
    assert str(tmp_lake) not in response.text and ".zip" not in response.text


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
async def test_a_contract_whose_file_cannot_be_written_is_an_item_failed_with_io_error_and_the_request_still_succeeds(
    client: httpx.AsyncClient,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    stock_fetch: StockFetchDouble,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor_lists(CALLS[602])
    vendor_bars(lambda symbol: no_trades(symbol))
    install_write_fault(monkeypatch, DISK_FULL_AT_THE_RENAME, only_bars=True)

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download(contracts=["SPY250613C00602000"]))

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(i["kind"], i["subject"], i["outcome"], i["reason"]) for i in items] == [
        ("listing", "SPY", "fetched", None),
        ("underlying", "SPY", "fetched", None),
        ("contract", "SPY250613C00602000", "failed", "io_error"),
    ]
    # The words say what failed and the system's reason, and name no path on the server's disk.
    assert "No space left on device" in items[2]["detail"] and str(tmp_lake) not in items[2]["detail"]


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


@respx.mock
async def test_the_most_contracts_a_download_takes_is_the_limit_served_the_limit_is_taken_and_one_more_is_refused(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch: StockFetchDouble
) -> None:
    async with client as c:
        limit = (await c.get(LIMITS)).json()["max_contracts_per_download"]
        symbols = [f"SPY250613C{strike * 1000:08d}" for strike in range(400, 400 + limit + 1)]
        vendor_lists(*(OptionContract.from_occ_symbol(symbol) for symbol in symbols))
        vendor_bars(lambda symbol: no_trades(symbol))
        at_the_limit = await c.post(f"{PREFIX}/download", json=_download(contracts=symbols[:limit]))
        one_more = await c.post(f"{PREFIX}/download", json=_download(contracts=symbols))

    # A window holds at most 60 contracts and a download takes as many: the listing, the underlying and 60 contracts
    # are 62 items; 61 contracts are refused at the body's own bound, before a symbol of them is parsed.
    assert limit == 60
    assert at_the_limit.status_code == 200, at_the_limit.text
    assert len(at_the_limit.json()["items"]) == 62
    assert one_more.status_code == 422
    assert one_more.json()["detail"][0]["loc"] == ["body", "contracts"]


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


@pytest.mark.parametrize(
    ("spoil", "kind"),
    [
        pytest.param(_replace(STOCK_PATH, _GARBAGE, admit=True), "UnderlyingDayUnreadable", id="a damaged file"),
        pytest.param(
            _stamp_a_bar_off_the_minute_on_the_underlying, "UnderlyingBarsCannotBePlaced", id="a bar off the minute"
        ),
    ],
)
@respx.mock
async def test_a_download_that_finds_the_underlying_damaged_says_so_by_what_and_why_and_names_no_file(
    spoil: Any,
    kind: str,
    client: httpx.AsyncClient,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    stock_fetch: StockFetchDouble,
) -> None:
    await _the_lake(fake_catalog, tmp_lake)
    spoil(fake_catalog, tmp_lake)

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download(contracts=[]))

    assert response.status_code == 200, response.text
    underlying = next(i for i in response.json()["items"] if i["kind"] == "underlying")
    assert (underlying["outcome"], underlying["reason"]) == ("failed", "underlying_unreadable")
    assert "SPY minute bars" in underlying["detail"] and kind in underlying["detail"]
    assert str(tmp_lake) not in response.text and ".zip" not in response.text and stock_fetch.calls == []


@respx.mock
async def test_a_download_whose_listing_cannot_be_read_back_says_so_by_what_and_why_and_names_no_file(
    client: httpx.AsyncClient,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    stock_fetch: StockFetchDouble,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor_lists(CALLS[602])
    bars = vendor_bars(lambda symbol: no_trades(symbol))

    def damaged(*args: object, **kwargs: object) -> None:
        raise OptionListingFileError(f"{raw_root(tmp_lake)}/{LISTING_PATH}: expected one member")

    monkeypatch.setattr(chain_download, "read_option_listing", damaged)

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download(contracts=["SPY250613C00602000"]))

    listing, _, contract = response.json()["items"]
    assert (listing["outcome"], listing["reason"]) == ("failed", "listing_not_captured")
    assert "SPY listing" in listing["detail"] and "OptionListingFileError" in listing["detail"]
    assert contract["reason"] == "listing_not_captured" and bars.call_count == 0
    assert str(tmp_lake) not in response.text and ".zip" not in response.text


# ── A window is the contracts a download takes ────────────────────────────


async def _the_wide_lake(fake_catalog: FakeCatalog, tmp_lake: Path, non_standard: tuple[str, ...]) -> None:
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS + 9 * MINUTE_MS, "602.00", "602.50", "601.90", "602.40")]
    )
    rows = [
        vendor_listing_row(OptionContract.of(root="SPY", expiry_date=FRIDAY, right=right, strike=strike))
        for right in ("call", "put")
        for strike in range(570, 640)
    ]
    rows += [
        vendor_listing_row(
            OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=strike), shares_per_contract=10
        )
        for strike in non_standard
    ]
    await store_listing(rows)


@respx.mock
async def test_the_largest_window_with_non_standard_contracts_inside_it_is_named_and_downloaded_in_one_call(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch: StockFetchDouble
) -> None:
    # 58 standard contracts and the non-standard calls at 602.5 and 603.5: 60, which is what a download takes.
    await _the_wide_lake(fake_catalog, tmp_lake, ("602.5", "603.5"))
    bars = vendor_bars(lambda symbol: no_trades(symbol))

    async with client as c:
        window = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=14))
        named = [
            contract["symbol"]
            for side in ("calls", "puts")
            for contract in window.json()[side]["contracts"]
            if contract["price_state"] == "not_stored"
        ]
        download = await c.post(f"{PREFIX}/download", json=_download(contracts=named))
        after = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=14))

    assert window.status_code == 200 and len(named) == 60
    assert download.status_code == 200, download.text
    items = download.json()["items"]
    assert [(i["kind"], i["outcome"]) for i in items[:2]] == [
        ("listing", "already_stored"),
        ("underlying", "already_stored"),
    ]
    assert len(items) == 62 and all(i["outcome"] == "fetched" for i in items[2:])
    assert bars.call_count == 60
    assert {c["price_state"] for side in ("calls", "puts") for c in after.json()[side]["contracts"]} == {
        "did_not_trade"
    }


@respx.mock
async def test_a_window_over_what_a_download_takes_is_refused_with_its_count_and_a_narrower_one_is_answered(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The same 58 standard contracts and three non-standard calls inside 588 to 616: 61. At 13 each side, 54 and three.
    await _the_wide_lake(fake_catalog, tmp_lake, ("601.5", "602.5", "603.5"))

    async with client as c:
        too_wide = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=14))
        narrower = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS, strikes=13))

    assert too_wide.status_code == 400
    assert too_wide.json()["detail"]["code"] == "OPTIONS_BACKTEST_WINDOW_TOO_WIDE"
    assert "61" in too_wide.json()["detail"]["message"] and "60" in too_wide.json()["detail"]["message"]
    assert narrower.status_code == 200
    assert sum(len(narrower.json()[side]["contracts"]) for side in ("calls", "puts")) == 57


# ── The download needs the data plane's control secret ────────────────────

_SECRET = "test-control-secret"


@pytest.fixture
def secret_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed posture: a secret is configured and the local-development opt-out is off."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)


@pytest.mark.parametrize("headers", [{}, {CONTROL_SECRET_HEADER: "wrong"}], ids=["no secret", "a wrong secret"])
@respx.mock
async def test_the_download_without_the_control_secret_is_refused_as_the_lakes_fetch_route_is_and_fetches_nothing(
    client: httpx.AsyncClient, stock_fetch: StockFetchDouble, secret_is_required: None, headers: dict[str, str]
) -> None:
    async with client as c:
        refused = await c.post(f"{PREFIX}/download", json=_download(), headers=headers)
        lakes_refusal = await c.post("/api/data-lake/ensure-data", json={}, headers=headers)

    assert refused.status_code == 403
    assert refused.json()["reason"] == DataPlaneControlSecretRefused.reason
    assert CONTROL_SECRET_HEADER in refused.json()["message"] and "detail" not in refused.json()
    # The very refusal the lake's own fetch route gives, body for body.
    assert (lakes_refusal.status_code, lakes_refusal.json()) == (refused.status_code, refused.json())
    # Nothing was fetched: no stock fetch, and a vendor request would have failed the test (nothing is mocked).
    assert stock_fetch.calls == []


@respx.mock
async def test_the_download_with_the_control_secret_runs(
    client: httpx.AsyncClient, stock_fetch: StockFetchDouble, secret_is_required: None
) -> None:
    vendor_lists(CALLS[602])
    vendor_bars(lambda symbol: no_trades(symbol))

    async with client as c:
        response = await c.post(
            f"{PREFIX}/download",
            json=_download(contracts=["SPY250613C00602000"]),
            headers={CONTROL_SECRET_HEADER: _SECRET},
        )

    assert response.status_code == 200, response.text
    assert [(i["kind"], i["outcome"]) for i in response.json()["items"]] == [
        ("listing", "fetched"),
        ("underlying", "fetched"),
        ("contract", "fetched"),
    ]


async def test_the_download_fails_closed_when_no_secret_is_configured_and_the_opt_out_is_off(
    client: httpx.AsyncClient, stock_fetch: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with client as c:
        response = await c.post(f"{PREFIX}/download", json=_download())

    assert response.status_code == 503
    assert response.json()["reason"] == FleetControlPlaneNotInstalled.reason
    assert CONTROL_SECRET_ENV_VAR in response.json()["message"] and stock_fetch.calls == []


@respx.mock
async def test_the_two_reads_need_no_secret_even_when_one_is_required_for_the_download(
    client: httpx.AsyncClient, fake_catalog: FakeCatalog, tmp_lake: Path, secret_is_required: None
) -> None:
    await _the_lake(fake_catalog, tmp_lake)

    async with client as c:
        day = await c.get(f"{PREFIX}/day", params={"underlying": "SPY", "trading_date_ms": FRIDAY_MIDNIGHT_MS})
        expiry = await c.get(f"{PREFIX}/expiry", params=_expiry_params(at_ms=MINUTE_10_MS))

    assert (day.status_code, expiry.status_code) == (200, 200)


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
    "PastChainFetchRefusal",
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


def test_only_the_download_operation_publishes_the_control_secret_header() -> None:
    paths = app.openapi()["paths"]

    def headers(path: str, method: str) -> set[str]:
        return {
            parameter["name"] for parameter in paths[path][method].get("parameters", []) if parameter["in"] == "header"
        }

    assert headers(f"{PREFIX}/download", "post") == {CONTROL_SECRET_HEADER}
    assert headers(f"{PREFIX}/day", "get") == set() and headers(f"{PREFIX}/expiry", "get") == set()


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
