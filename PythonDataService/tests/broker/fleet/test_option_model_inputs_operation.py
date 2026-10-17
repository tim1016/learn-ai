"""The coordinator serving a Clerk the rate and the dividends an option is priced with (#2913).

The route is the real one on an app of its own. FRED and Polygon are
stand-ins that count what they were asked; everything between them and the
answer is the code a coordinator runs.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.broker.fleet.errors import FleetAgentTokenRefused
from app.broker.fleet.history_batch import INTERNAL_HISTORY_BATCH_PATH
from app.broker.fleet.option_model_inputs import INTERNAL_OPTION_MODEL_INPUTS_PATH, OPTION_MODEL_INPUTS_READ_TIMEOUT_S
from app.config import fleet_settings, settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.expiry import option_expiry_anchor_ms
from app.routers import internal_fleet
from app.services import option_model_inputs
from app.services.fred_service import RATE_SOURCE_DEFAULT, RATE_SOURCE_FRED
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.error_handlers import install_fleet_control_error_handler
from app.utils.timestamps import to_ms_utc

_CLERK_ID = "clrk_inputs000000000000000000aa"
_OTHER_CLERK_ID = "clrk_other0000000000000000000bb"
_TOKEN = "svct_" + "4" * 32
_OTHER_TOKEN = "svct_" + "5" * 32
_PATH = INTERNAL_OPTION_MODEL_INPUTS_PATH
_AS_THIS_CLERK = {"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": _TOKEN}

_NY = ZoneInfo("America/New_York")
_TUESDAY = date(2026, 10, 6)
_WEDNESDAY = date(2026, 10, 7)
_FRIDAY = date(2026, 10, 9)
_NEXT_FRIDAY = date(2026, 10, 16)
# Tuesday 2026-10-06 at 10:31:02 New York time: the session has been open an hour.
NOW = to_ms_utc(datetime(2026, 10, 6, 10, 31, 2, tzinfo=_NY))


class Vendors:
    """Stand-ins for FRED and Polygon on the coordinator, counting what they were asked."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        rate: tuple[float, str] = (0.0512, RATE_SOURCE_FRED),
        rows: list[dict] | Exception = (),
    ) -> None:
        self.rate = rate
        self.rows = rows
        self.fred_asked: list[tuple[int, date]] = []
        self.polygon_asked: list[dict] = []
        # A vendor named here does not answer until the test lets it go.
        self.hanging: set[str] = set()
        self.reached = {"FRED": threading.Event(), "Polygon": threading.Event()}
        self.let_go = threading.Event()
        # What a vendor does to this process's own clock as it is asked: how long it is taken to have hung.
        self.on_asked = lambda _vendor: None
        monkeypatch.setattr(option_model_inputs, "_fred_rate", self._fred)
        monkeypatch.setattr(option_model_inputs, "_polygon", self)

    def _asked(self, vendor: str) -> None:
        self.on_asked(vendor)
        self.reached[vendor].set()
        if vendor in self.hanging:
            self.let_go.wait(timeout=20)

    def _fred(self, days: int, today: date) -> tuple[float, str]:
        self.fred_asked.append((days, today))
        self._asked("FRED")
        return self.rate

    def list_dividends(self, **query: object) -> list[dict]:
        self.polygon_asked.append(query)
        self._asked("Polygon")
        if isinstance(self.rows, Exception):
            raise self.rows
        return list(self.rows)

    @property
    def asked(self) -> int:
        return len(self.fred_asked) + len(self.polygon_asked)


@pytest.fixture(autouse=True)
def _a_coordinator_that_keeps_nothing_yet(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(fleet_settings, "ROLE", "fleet_coordinator")
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "a-key")
    option_model_inputs.clear_cache()
    yield
    option_model_inputs.clear_cache()


@pytest.fixture
def hung(monkeypatch: pytest.MonkeyPatch) -> Iterator[Vendors]:
    """Stand-in vendors a test may leave hanging. They are let go when it ends."""
    vendors = Vendors(monkeypatch, rows=[{"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "USD"}])
    yield vendors
    vendors.let_go.set()


def _app(tokens: dict[str, str] | None = None) -> FastAPI:
    """A coordinator that mounts only the internal fleet router."""
    app = FastAPI()
    install_fleet_control_error_handler(app)
    app.state.fleet_agent_tokens_text = json.dumps({_CLERK_ID: _TOKEN} if tokens is None else tokens)
    app.include_router(internal_fleet.router)
    return app


def _ask(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "clerk_id": _CLERK_ID,
        "underlying": "SPY",
        "at_ms": NOW,
        "latest_expiry_ms": option_expiry_anchor_ms(_NEXT_FRIDAY),
        "wait": True,
    }
    body.update(overrides)
    return body


async def _post(
    body: dict[str, object], headers: dict[str, str] | None = None, *, path: str = _PATH, app: FastAPI | None = None
) -> Response:
    transport = ASGITransport(app=app or _app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, headers=_AS_THIS_CLERK if headers is None else headers)


# ---- What a Clerk is answered ------------------------------------------------------


async def test_a_clerk_is_answered_with_the_rate_and_the_dividends_still_to_go_ex(monkeypatch: pytest.MonkeyPatch) -> None:
    vendors = Vendors(
        monkeypatch,
        rows=[
            {"ex_dividend_date": "2026-10-16", "cash_amount": 0.470625, "currency": "USD"},
            {"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "USD"},
            # Went ex at this morning's open, an hour ago: it is in the price already.
            {"ex_dividend_date": "2026-10-06", "cash_amount": 0.4, "currency": "USD"},
        ],
    )

    response = await _post(_ask(underlying="spy"))

    assert response.status_code == 200
    answer = response.json()
    assert (answer["status"], answer["underlying"], answer["at_ms"], answer["latest_expiry_ms"]) == (
        "answered", "SPY", NOW, option_expiry_anchor_ms(_NEXT_FRIDAY),
    )
    rate = answer["rate"]
    assert set(rate) == {"value", "source", "why_default", "sentence"}
    assert (rate["value"], rate["source"], rate["why_default"]) == ("0.0512", "FRED", None)
    dividends = answer["dividends"]
    assert (dividends["status"], dividends["read_ms"]) == ("known", NOW)
    # Every one served is in dollars, so none says so.
    assert dividends["declared"] == [
        {"ex_ms": session_open_ms_utc(_FRIDAY), "cash": "1.8"},
        {"ex_ms": session_open_ms_utc(_NEXT_FRIDAY), "cash": "0.470625"},
    ]
    assert rate["sentence"] and dividends["sentence"]
    # Ten days to the latest expiry, and the dividends from today to it.
    assert vendors.fred_asked == [(10, _TUESDAY)]
    assert vendors.polygon_asked == [
        {"ticker": "SPY", "ex_dividend_date_gte": "2026-10-06", "ex_dividend_date_lte": "2026-10-16"}
    ]


async def test_none_declared_is_answered_as_known_with_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    Vendors(monkeypatch, rows=[])

    dividends = (await _post(_ask())).json()["dividends"]

    assert (dividends["status"], dividends["declared"]) == ("known", [])


async def test_a_rate_fred_did_not_give_is_answered_as_the_default_with_its_reason_and_the_dividends_still_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Vendors(
        monkeypatch,
        rate=(DEFAULT_RISK_FREE_RATE, RATE_SOURCE_DEFAULT),
        rows=[{"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "USD"}],
    )

    answer = (await _post(_ask())).json()

    rate = answer["rate"]
    assert (rate["source"], rate["why_default"]) == ("default", "fred_gave_no_rate")
    assert float(rate["value"]) == DEFAULT_RISK_FREE_RATE
    assert answer["dividends"]["status"] == "known"
    assert [paid["cash"] for paid in answer["dividends"]["declared"]] == ["1.8"]


@pytest.mark.parametrize(
    ("rows", "polygon_key", "why"),
    [
        pytest.param(RuntimeError("503 from Polygon"), "a-key", "polygon_did_not_answer", id="Polygon did not answer"),
        pytest.param(
            [{"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "CAD"}],
            "a-key",
            "polygon_answer_unreadable",
            id="a row that cannot be read",
        ),
        pytest.param([], "", "no_polygon_key", id="no Polygon key"),
    ],
)
async def test_dividends_that_could_not_be_looked_up_are_answered_as_not_known_with_the_reason_and_the_rate_still_known(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict] | Exception, polygon_key: str, why: str
) -> None:
    Vendors(monkeypatch, rows=rows)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", polygon_key)

    response = await _post(_ask())

    assert response.status_code == 200
    answer = response.json()
    dividends = answer["dividends"]
    assert (dividends["status"], dividends["why"]) == ("unknown", why)
    assert dividends["sentence"]
    # Not known is not none declared: there is no list to read as empty.
    assert "declared" not in dividends
    assert (answer["rate"]["source"], answer["rate"]["value"]) == ("FRED", "0.0512")


async def test_what_the_coordinator_already_keeps_is_answered_without_asking_the_vendors_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendors = Vendors(monkeypatch, rows=[{"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "USD"}])

    first = (await _post(_ask())).json()
    a_minute_later = (await _post(_ask(at_ms=NOW + 60_000))).json()

    assert vendors.asked == 2
    assert a_minute_later["dividends"]["declared"] == first["dividends"]["declared"]
    # When Polygon was read, not when this Clerk asked.
    assert (a_minute_later["at_ms"], a_minute_later["dividends"]["read_ms"]) == (NOW + 60_000, NOW)


async def test_an_ask_that_will_not_wait_is_answered_at_once_while_another_lookup_runs(hung: Vendors) -> None:
    """A page asks every second. Its ask stacks no waiting thread on the coordinator either."""
    hung.hanging = {"Polygon"}
    app = _app()
    waiting = asyncio.ensure_future(_post(_ask(wait=True), app=app))
    assert await asyncio.to_thread(hung.reached["Polygon"].wait, 5)
    # The vendors run on separate pools; reaching Polygon does not mean FRED has started.
    assert await asyncio.to_thread(hung.reached["FRED"].wait, 5)

    response = await _post(_ask(wait=False), app=app)
    asked_while_it_hung = hung.asked
    hung.let_go.set()
    waited = await waiting

    assert (response.status_code, response.json()) == (200, {"status": "being_looked_up"})
    # The ask that would not wait began no lookup of its own.
    assert asked_while_it_hung == 2 == hung.asked
    assert waited.json()["dividends"]["status"] == "known"


async def test_an_ask_about_another_underlying_is_answered_about_that_one(monkeypatch: pytest.MonkeyPatch) -> None:
    vendors = Vendors(monkeypatch, rows=[{"ex_dividend_date": "2026-10-09", "cash_amount": 0.72, "currency": "USD"}])

    answer = (await _post(_ask(underlying="qqq"))).json()

    assert (answer["status"], answer["underlying"]) == ("answered", "QQQ")
    assert [query["ticker"] for query in vendors.polygon_asked] == ["QQQ"]
    assert [paid["cash"] for paid in answer["dividends"]["declared"]] == ["0.72"]


async def test_a_dividend_is_served_until_the_open_of_its_ex_date_and_not_from_that_moment_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the open the stock already trades without it: it is in the price, to the millisecond."""
    Vendors(monkeypatch, rows=[{"ex_dividend_date": "2026-10-07", "cash_amount": 0.4, "currency": "USD"}])
    the_open = session_open_ms_utc(_WEDNESDAY)

    a_millisecond_before = (await _post(_ask(at_ms=the_open - 1))).json()["dividends"]
    option_model_inputs.clear_cache()
    at_the_open = (await _post(_ask(at_ms=the_open))).json()["dividends"]

    assert a_millisecond_before["declared"] == [{"ex_ms": the_open, "cash": "0.4"}]
    assert (at_the_open["status"], at_the_open["declared"]) == ("known", [])


# ---- A vendor that hangs ---------------------------------------------------------------


def test_a_whole_lookup_ends_well_inside_the_time_a_clerk_waits_for_it() -> None:
    """Both vendors are asked at once and each is waited for no longer than the bound."""
    assert 2 * option_model_inputs.VENDOR_LOOKUP_BOUND_S <= OPTION_MODEL_INPUTS_READ_TIMEOUT_S


async def test_with_fred_hanging_a_clerk_is_still_answered_the_dividends_polygon_gave_at_once_and_a_default_rate(
    monkeypatch: pytest.MonkeyPatch, hung: Vendors
) -> None:
    """FRED does not answer. Polygon answered at once. The Clerk is not left knowing neither."""
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    hung.hanging = {"FRED"}

    response = await _post(_ask())

    assert response.status_code == 200
    answer = response.json()
    assert (answer["dividends"]["status"], [paid["cash"] for paid in answer["dividends"]["declared"]]) == ("known", ["1.8"])
    rate = answer["rate"]
    assert (rate["source"], rate["why_default"]) == ("default", "lookup_too_slow")
    assert float(rate["value"]) == DEFAULT_RISK_FREE_RATE and rate["sentence"]


async def test_a_rate_fred_was_too_slow_to_give_is_asked_for_again_half_a_minute_after_the_wait_for_it_ended(
    monkeypatch: pytest.MonkeyPatch, hung: Vendors
) -> None:
    """Eight seconds waited, then thirty kept: FRED is asked again at thirty-eight, not at thirty and not on every ask."""
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    own_clock = [500.0]
    monkeypatch.setattr(option_model_inputs, "_monotonic", lambda: own_clock[0])
    hung.hanging = {"FRED"}

    def eight_seconds_pass(vendor: str) -> None:
        if vendor == "FRED":
            own_clock[0] += 8.0

    hung.on_asked = eight_seconds_pass
    app = _app()

    await _post(_ask(), app=app)
    kept = (await _post(_ask(at_ms=NOW + 8_000 + 29_999), app=app)).json()
    asked_inside_the_half_minute = len(hung.fred_asked)
    await _post(_ask(at_ms=NOW + 8_000 + 30_000), app=app)

    assert (kept["rate"]["source"], kept["dividends"]["status"]) == ("default", "known")
    assert (asked_inside_the_half_minute, len(hung.fred_asked)) == (1, 2)
    # Polygon was read once and is kept its hour.
    assert len(hung.polygon_asked) == 1


async def test_with_both_vendors_hanging_a_clerk_is_answered_inside_the_bound_that_neither_is_known(
    monkeypatch: pytest.MonkeyPatch, hung: Vendors
) -> None:
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    hung.hanging = {"FRED", "Polygon"}

    answer = (await asyncio.wait_for(_post(_ask()), timeout=5)).json()

    assert (answer["rate"]["source"], answer["rate"]["why_default"]) == ("default", "lookup_too_slow")
    assert (answer["dividends"]["status"], answer["dividends"]["why"]) == ("unknown", "lookup_too_slow")
    assert "declared" not in answer["dividends"]


# ---- Who is answered ------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Fleet-Clerk-Id": _CLERK_ID}, id="no token"),
        pytest.param({"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": "wrong"}, id="a wrong token"),
        pytest.param({"X-Fleet-Agent-Token": _TOKEN}, id="no Clerk named in the header"),
        pytest.param(
            {"X-Fleet-Clerk-Id": _OTHER_CLERK_ID, "X-Fleet-Agent-Token": _TOKEN}, id="another Clerk named in the header"
        ),
        pytest.param(
            {"X-Fleet-Clerk-Id": _CLERK_ID, "X-Data-Plane-Control-Secret": "what-a-browser-would-send"},
            id="the browser's secret in place of the token",
        ),
    ],
)
async def test_a_caller_that_is_not_this_clerk_is_refused_as_the_history_read_refuses_it(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    vendors = Vendors(monkeypatch)
    app = _app({_CLERK_ID: _TOKEN, _OTHER_CLERK_ID: _OTHER_TOKEN})
    a_history_ask = {"clerk_id": _CLERK_ID, "symbol": "SPY", "timeframe": "1m", "required_bar_count": 300, "as_of_ms": NOW}

    refused = await _post(_ask(), headers, app=app)
    by_the_history_read = await _post(a_history_ask, headers, path=INTERNAL_HISTORY_BATCH_PATH, app=app)

    assert refused.status_code == 403
    assert refused.json()["reason"] == FleetAgentTokenRefused.reason
    assert (refused.status_code, refused.json()) == (by_the_history_read.status_code, by_the_history_read.json())
    assert vendors.asked == 0


async def test_another_clerks_own_header_and_token_do_not_ask_in_this_clerks_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other Clerk is who it says it is. The ask names this one, and is not that Clerk's to make."""
    vendors = Vendors(monkeypatch)
    app = _app({_CLERK_ID: _TOKEN, _OTHER_CLERK_ID: _OTHER_TOKEN})
    as_the_other_clerk = {"X-Fleet-Clerk-Id": _OTHER_CLERK_ID, "X-Fleet-Agent-Token": _OTHER_TOKEN}

    in_this_clerks_name = await _post(_ask(clerk_id=_CLERK_ID), as_the_other_clerk, app=app)
    in_its_own_name = await _post(_ask(clerk_id=_OTHER_CLERK_ID), as_the_other_clerk, app=app)

    assert (in_this_clerks_name.status_code, in_this_clerks_name.json()["reason"]) == (403, FleetAgentTokenRefused.reason)
    assert in_its_own_name.status_code == 200
    assert vendors.asked == 2


async def test_a_clerk_the_coordinator_holds_no_token_for_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    vendors = Vendors(monkeypatch)

    refused = await _post(_ask(), app=_app({_OTHER_CLERK_ID: _TOKEN}))

    assert (refused.status_code, refused.json()["reason"]) == (403, FleetAgentTokenRefused.reason)
    assert vendors.asked == 0


# ---- What is not an ask ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"at_ms": str(NOW)}, id="a moment written as text"),
        pytest.param({"at_ms": float(NOW)}, id="a moment written as a float"),
        pytest.param({"at_ms": True}, id="a moment that is a flag"),
        pytest.param({"at_ms": -1}, id="a moment before any calendar"),
        pytest.param({"latest_expiry_ms": "2026-10-16"}, id="an expiry written as a date"),
        pytest.param({"latest_expiry_ms": session_open_ms_utc(_NEXT_FRIDAY)}, id="an expiry that is not a 16:00 anchor"),
        pytest.param({"wait": "yes"}, id="a wait that is not a flag"),
        pytest.param({"underlying": ""}, id="no underlying"),
        pytest.param({"underlying": "123-not-a-symbol"}, id="an underlying that is no symbol"),
        pytest.param({"clerk_id": ""}, id="no Clerk"),
        pytest.param({"latest_expiry": "2026-10-16"}, id="a field the ask does not have"),
    ],
)
async def test_what_is_not_an_ask_is_refused_before_any_vendor_is_asked(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    vendors = Vendors(monkeypatch)

    response = await _post(_ask(**overrides))

    assert response.status_code == 422
    assert vendors.asked == 0


async def test_an_ask_that_leaves_out_whether_it_waits_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    vendors = Vendors(monkeypatch)
    ask = _ask()
    del ask["wait"]

    assert (await _post(ask)).status_code == 422
    assert vendors.asked == 0
