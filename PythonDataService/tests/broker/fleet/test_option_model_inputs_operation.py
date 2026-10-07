"""The coordinator serving a Clerk the rate and the dividends an option is priced with (#2913).

The route is the real one on an app of its own. FRED and Polygon are
stand-ins that count what they were asked; everything between them and the
answer is the code a coordinator runs.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.broker.fleet.errors import FleetAgentTokenRefused
from app.broker.fleet.history_batch import INTERNAL_HISTORY_BATCH_PATH
from app.broker.fleet.option_model_inputs import INTERNAL_OPTION_MODEL_INPUTS_PATH
from app.config import fleet_settings, settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.expiry import option_expiry_anchor_ms
from app.routers import internal_fleet
from app.services import option_model_inputs
from app.services.fred_service import RATE_SOURCE_DEFAULT, RATE_SOURCE_FRED
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.error_handlers import install_fleet_control_error_handler
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import to_ms_utc

_CLERK_ID = "clrk_inputs000000000000000000aa"
_OTHER_CLERK_ID = "clrk_other0000000000000000000bb"
_TOKEN = "svct_" + "4" * 32
_PATH = INTERNAL_OPTION_MODEL_INPUTS_PATH
_AS_THIS_CLERK = {"X-Fleet-Clerk-Id": _CLERK_ID, "X-Fleet-Agent-Token": _TOKEN}

_NY = ZoneInfo("America/New_York")
_TUESDAY = date(2026, 10, 6)
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
        monkeypatch.setattr(option_model_inputs, "_fred_rate", self._fred)
        monkeypatch.setattr(option_model_inputs, "_polygon", self)

    def _fred(self, days: int, today: date) -> tuple[float, str]:
        self.fred_asked.append((days, today))
        return self.rate

    def list_dividends(self, **query: object) -> list[dict]:
        self.polygon_asked.append(query)
        if isinstance(self.rows, Exception):
            raise self.rows
        return list(self.rows)

    @property
    def asked(self) -> int:
        return len(self.fred_asked) + len(self.polygon_asked)


@pytest.fixture(autouse=True)
def _a_coordinator_that_keeps_nothing_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_settings, "ROLE", "fleet_coordinator")
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "a-key")
    monkeypatch.setattr(option_model_inputs, "_rates", {})
    monkeypatch.setattr(option_model_inputs, "_dividends", {})


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
    assert (rate["value"], rate["source"], rate["why_default"]) == ("0.0512", "FRED", None)
    assert rate["published_by_ms"] == et_midnight_ms(_TUESDAY)
    dividends = answer["dividends"]
    assert (dividends["status"], dividends["read_ms"]) == ("known", NOW)
    assert dividends["declared"] == [
        {"ex_ms": session_open_ms_utc(_FRIDAY), "cash": "1.8", "currency": "USD"},
        {"ex_ms": session_open_ms_utc(_NEXT_FRIDAY), "cash": "0.470625", "currency": "USD"},
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


async def test_an_ask_that_will_not_wait_is_answered_at_once_while_another_lookup_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page asks every second. Its ask stacks no waiting thread on the coordinator either."""
    vendors = Vendors(monkeypatch)

    assert option_model_inputs._lookups.acquire(blocking=False)
    try:
        response = await _post(_ask(wait=False))
    finally:
        option_model_inputs._lookups.release()

    assert (response.status_code, response.json()) == (200, {"status": "being_looked_up"})
    assert vendors.asked == 0


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
    app = _app({_CLERK_ID: _TOKEN, _OTHER_CLERK_ID: "svct_" + "5" * 32})
    a_history_ask = {"clerk_id": _CLERK_ID, "symbol": "SPY", "timeframe": "1m", "required_bar_count": 300, "as_of_ms": NOW}

    refused = await _post(_ask(), headers, app=app)
    by_the_history_read = await _post(a_history_ask, headers, path=INTERNAL_HISTORY_BATCH_PATH, app=app)

    assert refused.status_code == 403
    assert refused.json()["reason"] == FleetAgentTokenRefused.reason
    assert (refused.status_code, refused.json()) == (by_the_history_read.status_code, by_the_history_read.json())
    assert vendors.asked == 0


async def test_a_clerk_the_coordinator_holds_no_token_for_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    vendors = Vendors(monkeypatch)

    refused = await _post(_ask(), app=_app({_OTHER_CLERK_ID: _TOKEN}))

    assert (refused.status_code, refused.json()["reason"]) == (403, FleetAgentTokenRefused.reason)
    assert vendors.asked == 0


def test_the_route_is_left_out_of_the_public_schema() -> None:
    routes = [route for route in _app().routes if getattr(route, "path", None) == _PATH]

    assert routes, "the route did not mount on a coordinator"
    assert all(route.include_in_schema is False for route in routes)


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
