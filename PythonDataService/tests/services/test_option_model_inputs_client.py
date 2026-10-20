"""A Clerk reading the rate and the dividends from the coordinator: what it learns, and each way it learns nothing.

The Clerk's side is the real one: ``read_option_model_inputs`` in a process
whose role is ``clerk_agent``. It talks to the coordinator's real internal
route on an app of its own. Only what the coordinator itself read is a
stand-in, because one process cannot be both.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from app.broker.fleet import internal_http
from app.broker.fleet.internal_http import build_internal_sync_client
from app.broker.fleet.option_model_inputs import INTERNAL_OPTION_MODEL_INPUTS_PATH, OPTION_MODEL_INPUTS_READ_TIMEOUT_S
from app.config import fleet_settings, settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.expiry import option_expiry_anchor_ms
from app.options.model_input_reasons import WhyNotKnown
from app.options.resolver import DividendOn
from app.routers import internal_fleet
from app.services import option_model_inputs, option_model_inputs_client, polygon_client
from app.services.fred_service import RATE_SOURCE_FRED
from app.services.option_model_inputs import ModelInputsBeingRead, ModelInputsRead, read_option_model_inputs
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.error_handlers import install_fleet_control_error_handler
from app.utils.timestamps import to_ms_utc

# A real server on a loopback port, as the fleet's lane tests start one.
from tests.broker.fleet import test_a2_alpaca_lane as lanes

CLERK_ID = "clrk_inputs000000000000000000aa"
TOKEN = "svct_" + "4" * 32

_NY = ZoneInfo("America/New_York")
_TUESDAY = date(2026, 10, 6)
_WEDNESDAY = date(2026, 10, 7)
_FRIDAY = date(2026, 10, 9)
_NEXT_FRIDAY = date(2026, 10, 16)
# Tuesday 2026-10-06 at 10:31:02 New York time: the session has been open an hour.
NOW = to_ms_utc(datetime(2026, 10, 6, 10, 31, 2, tzinfo=_NY))

# What the coordinator read. The rate takes seventeen digits to write, and the second dividend six.
READ = ModelInputsRead(
    rate=0.1 + 0.2,
    rate_source="the coordinator's words for where the rate came from",
    rate_is_default=False,
    dividends=(
        DividendOn(ex_ms=session_open_ms_utc(_FRIDAY), cash=1.8),
        DividendOn(ex_ms=session_open_ms_utc(_NEXT_FRIDAY), cash=0.470625),
    ),
    dividends_source="the coordinator's words for what is declared",
    dividends_read_ms=NOW,
)
_DIVIDENDS_NOT_KNOWN = replace(
    READ,
    dividends=None,
    dividends_source="the coordinator's words for why Polygon gave nothing",
    dividends_unknown_why=WhyNotKnown.POLYGON_DID_NOT_ANSWER,
)
_RATE_ONLY_THE_DEFAULT = replace(
    READ,
    rate=DEFAULT_RISK_FREE_RATE,
    rate_source="the coordinator's words for why this is the default",
    rate_is_default=True,
    rate_default_why=WhyNotKnown.FRED_GAVE_NO_RATE,
)


class Coordinator:
    """A stand-in coordinator for a Clerk to ask, and this process made that Clerk.

    The route, its authentication and everything on the wire are real. What
    the coordinator read is ``read``, or the exception it raises. ``answer``
    replaces the whole HTTP answer, for a coordinator that fails.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, read: ModelInputsRead | Exception = READ) -> None:
        self.read = read
        # Each ask as the coordinator's own reading was handed it: (underlying, at_ms, latest_expiry, wait).
        self.asked: list[tuple[str, int, date, bool]] = []
        self.sent: list[httpx.Request] = []
        self.read_timeouts: list[float] = []
        # Anything this Clerk asked a vendor itself. It holds no key and must ask none.
        self.vendors_asked: list[str] = []
        self.answer: Callable[[httpx.Request], httpx.Response] = self._through_the_route

        self._app = FastAPI()
        install_fleet_control_error_handler(self._app)
        self._app.state.fleet_agent_tokens_text = json.dumps({CLERK_ID: TOKEN})
        self._app.include_router(internal_fleet.router)
        monkeypatch.setattr(internal_fleet, "read_option_model_inputs", self._reads)

        monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
        monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", "http://127.0.0.1:8000/")
        monkeypatch.setattr(fleet_settings, "CLERK_ID", CLERK_ID)
        monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", TOKEN)
        monkeypatch.setattr(settings, "POLYGON_API_KEY", "")
        monkeypatch.setattr(option_model_inputs_client, "build_internal_sync_client", self._client)
        option_model_inputs.clear_cache()
        monkeypatch.setattr(option_model_inputs, "_polygon", None)
        monkeypatch.setattr(option_model_inputs, "_fred_rate", self._fred_asked)
        monkeypatch.setattr(polygon_client, "PolygonClientService", self._polygon_client_built)

    def _reads(self, underlying: str, *, at_ms: int, latest_expiry: date, wait: bool) -> ModelInputsRead:
        self.asked.append((underlying, at_ms, latest_expiry, wait))
        if isinstance(self.read, Exception):
            raise self.read
        return self.read

    def _fred_asked(self, _days: int, _today: date) -> tuple[float, str]:
        self.vendors_asked.append("FRED")
        return 0.0, RATE_SOURCE_FRED

    def _polygon_client_built(self) -> Coordinator:
        self.vendors_asked.append("Polygon")
        return self

    def list_dividends(self, **_query: object) -> list[dict]:
        return []

    def _client(self, *, read_timeout_s: float) -> httpx.Client:
        self.read_timeouts.append(read_timeout_s)

        def handle(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return self.answer(request)

        return httpx.Client(transport=httpx.MockTransport(handle))

    def _through_the_route(self, request: httpx.Request) -> httpx.Response:
        async def call() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app)) as client:
                return await client.request(
                    request.method, str(request.url), headers=request.headers, content=request.content
                )

        # The Clerk's ask blocks its own thread. The route runs on a loop in another.
        with ThreadPoolExecutor(max_workers=1) as apart:
            answered = apart.submit(asyncio.run, call()).result()
        return httpx.Response(answered.status_code, headers=answered.headers, content=answered.content)


@pytest.fixture(autouse=True)
def _nothing_kept_afterwards() -> Iterator[None]:
    yield
    option_model_inputs.clear_cache()


def _read(*, at_ms: int = NOW, latest_expiry: date = _NEXT_FRIDAY, wait: bool = True) -> ModelInputsRead:
    return read_option_model_inputs("spy", at_ms=at_ms, latest_expiry=latest_expiry, wait=wait)


def _nothing_known(read: ModelInputsRead) -> tuple[object, ...]:
    """The two codes, when neither fact is known: dividends unknown, never none; the rate the default, saying so."""
    assert read.dividends is None and read.model is None
    assert (read.rate, read.rate_is_default) == (DEFAULT_RISK_FREE_RATE, True)
    assert read.rate_source and read.dividends_source
    return read.rate_default_why, read.dividends_unknown_why


# ---- What a Clerk learns ------------------------------------------------------------


def test_a_clerk_asks_as_itself_and_reads_every_digit_and_every_ex_date_the_coordinator_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(monkeypatch)
    answers: list[dict] = []

    def keeping(request: httpx.Request) -> httpx.Response:
        response = coordinator._through_the_route(request)
        answers.append(response.json())
        return response

    coordinator.answer = keeping

    read = _read()

    assert read.rate.hex() == (0.1 + 0.2).hex(), "a Clerk reads every digit of the rate"
    assert read.dividends == READ.dividends, "a Clerk reads every ex-date the coordinator read"
    assert [paid.cash.hex() for paid in read.dividends] == [(1.8).hex(), (0.470625).hex()], "every digit of each cash"
    assert (read.rate_is_default, read.rate_default_why, read.dividends_unknown_why) == (False, None, None), (
        "neither fact is marked default or unknown"
    )
    assert (read.rate_source, read.dividends_source, read.dividends_read_ms) == (
        READ.rate_source, READ.dividends_source, READ.dividends_read_ms,
    ), "the coordinator's words and read time come through"
    assert read.model is not None and read.model.dividends == READ.dividends, "the model carries the dividends"
    # One ask, for the symbol as the coordinator spells it, and no vendor asked from here.
    assert coordinator.asked == [("SPY", NOW, _NEXT_FRIDAY, True)], "one ask, for the symbol as the coordinator spells it"
    assert len(coordinator.sent) == 1, "one request sent"
    assert coordinator.vendors_asked == [], "no vendor asked from here"

    (sent,) = coordinator.sent
    assert (sent.method, str(sent.url)) == ("POST", f"http://127.0.0.1:8000{INTERNAL_OPTION_MODEL_INPUTS_PATH}"), (
        "the ask goes to the coordinator's route"
    )
    assert (sent.headers["X-Fleet-Clerk-Id"], sent.headers["X-Fleet-Agent-Token"]) == (CLERK_ID, TOKEN), (
        "the ask is made as this Clerk"
    )
    assert json.loads(sent.content) == {
        "clerk_id": CLERK_ID,
        "underlying": "SPY",
        "at_ms": NOW,
        "latest_expiry_ms": option_expiry_anchor_ms(_NEXT_FRIDAY),
        "wait": True,
    }, "the body carries no token and no date text"
    assert coordinator.read_timeouts == [OPTION_MODEL_INPUTS_READ_TIMEOUT_S], "the ask is read within the timeout"

    (answer,) = answers
    assert answer["rate"] == {
        "value": "0.30000000000000004",
        "source": "FRED",
        "why_default": None,
        "sentence": READ.rate_source,
    }, "the answer on the wire has its rate as text"
    assert answer["dividends"]["declared"] == [
        {"ex_ms": session_open_ms_utc(_FRIDAY), "cash": "1.8"},
        {"ex_ms": session_open_ms_utc(_NEXT_FRIDAY), "cash": "0.470625"},
    ], "the answer on the wire has its cash as text and its ex-dates as moments"


def test_over_a_real_socket_the_fleets_own_client_reads_the_same_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other test here hands the Clerk a client that opens no connection. This one is the fleet's own."""
    coordinator = Coordinator(monkeypatch)
    server = lanes._RealServer(coordinator._app)
    server.start()
    try:
        monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", server.base_url)
        monkeypatch.setattr(option_model_inputs_client, "build_internal_sync_client", build_internal_sync_client)
        read = _read()
    finally:
        server.stop()

    assert read.rate.hex() == (0.1 + 0.2).hex()
    assert read.dividends == READ.dividends and read.dividends_read_ms == READ.dividends_read_ms
    assert coordinator.asked == [("SPY", NOW, _NEXT_FRIDAY, True)]


def test_dividends_the_coordinator_could_not_learn_stay_unknown_with_its_reason_and_the_rate_is_still_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Coordinator(monkeypatch, _DIVIDENDS_NOT_KNOWN)

    read = _read()

    # Unknown, not an empty list: an empty list would say none is declared.
    assert read.dividends is None and read.model is None
    assert (read.dividends_unknown_why, read.dividends_source) == (
        WhyNotKnown.POLYGON_DID_NOT_ANSWER, _DIVIDENDS_NOT_KNOWN.dividends_source,
    )
    assert (read.rate, read.rate_is_default, read.rate_default_why) == (0.1 + 0.2, False, None)


def test_a_rate_the_coordinator_has_only_the_default_for_says_so_and_the_dividends_are_still_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Coordinator(monkeypatch, _RATE_ONLY_THE_DEFAULT)

    read = _read()

    assert (read.rate, read.rate_is_default, read.rate_default_why, read.rate_source) == (
        DEFAULT_RISK_FREE_RATE, True, WhyNotKnown.FRED_GAVE_NO_RATE, _RATE_ONLY_THE_DEFAULT.rate_source,
    )
    assert read.dividends == READ.dividends and read.dividends_unknown_why is None


def test_none_declared_at_the_coordinator_is_known_here_too(monkeypatch: pytest.MonkeyPatch) -> None:
    Coordinator(monkeypatch, replace(READ, dividends=()))

    read = _read()

    assert read.dividends == () and read.model is not None


# ---- Each way a Clerk learns nothing ---------------------------------------------------


def _times_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("no answer in time", request=request)


def _refuses_the_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _to_this_very_ask() -> dict:
    """A well-formed answer to the ask every test here makes."""
    return {
        "status": "answered",
        "underlying": "SPY",
        "at_ms": NOW,
        "latest_expiry_ms": option_expiry_anchor_ms(_NEXT_FRIDAY),
        "rate": {"value": "0.05", "source": "FRED", "why_default": None, "sentence": "a rate"},
        "dividends": {"status": "known", "declared": [], "read_ms": NOW, "sentence": "none declared"},
    }


def _to_another_question(**asked: object) -> Callable[[httpx.Request], httpx.Response]:
    """A well-formed answer, to a question that differs from the one asked in one thing."""
    return lambda _request: httpx.Response(200, json=_to_this_very_ask() | asked)


def _in_the_answer(*, rate: dict | None = None, **dividends: object) -> Callable[[httpx.Request], httpx.Response]:
    """The answer to this very ask, with its ``rate`` or its ``dividends`` changed."""

    def answer(_request: httpx.Request) -> httpx.Response:
        body = _to_this_very_ask()
        body["rate"] = body["rate"] | (rate or {})
        body["dividends"] = body["dividends"] | dividends
        return httpx.Response(200, json=body)

    return answer


def test_an_answer_to_this_very_ask_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the cases below each change one thing in."""
    coordinator = Coordinator(monkeypatch)
    coordinator.answer = _in_the_answer()

    read = _read()

    assert (read.rate, read.rate_is_default, read.dividends) == (0.05, False, ())


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        pytest.param(_times_out, WhyNotKnown.COORDINATOR_TIMED_OUT, id="no answer in time"),
        pytest.param(_refuses_the_connection, WhyNotKnown.COORDINATOR_UNREACHABLE, id="not reachable"),
        pytest.param(lambda _: httpx.Response(500, json={"detail": "boom"}), WhyNotKnown.COORDINATOR_FAILED, id="a 500"),
        pytest.param(lambda _: httpx.Response(503, text="busy"), WhyNotKnown.COORDINATOR_FAILED, id="a 503"),
        pytest.param(lambda _: httpx.Response(401, json={"detail": "who"}), WhyNotKnown.COORDINATOR_REFUSED, id="a 401"),
        pytest.param(lambda _: httpx.Response(422, json={"detail": "what"}), WhyNotKnown.COORDINATOR_REFUSED, id="a 422"),
        pytest.param(
            lambda _: httpx.Response(302, headers={"location": "http://elsewhere.example/"}),
            WhyNotKnown.COORDINATOR_REFUSED,
            id="a redirect, which is not followed",
        ),
        pytest.param(
            lambda _: httpx.Response(200, content=b"not json at all"),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a body that is not JSON",
        ),
        pytest.param(
            lambda _: httpx.Response(200, json={"status": "answered", "rate": 0.05, "dividends": []}),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a body of another shape",
        ),
        pytest.param(
            _to_another_question(underlying="QQQ"),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="an answer about another underlying",
        ),
        pytest.param(
            _to_another_question(at_ms=NOW + 1),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="an answer about another moment",
        ),
        pytest.param(
            _to_another_question(latest_expiry_ms=option_expiry_anchor_ms(_FRIDAY)),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="an answer up to another expiry",
        ),
        pytest.param(
            _in_the_answer(declared=[{"ex_ms": session_open_ms_utc(_FRIDAY), "cash": 1.8}]),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="an amount written as a number, not as text",
        ),
        pytest.param(
            _in_the_answer(declared=[{"ex_ms": session_open_ms_utc(_FRIDAY), "cash": "1.8", "currency": "USD"}]),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a field the answer does not have",
        ),
        pytest.param(
            _in_the_answer(declared=[{"ex_ms": session_open_ms_utc(_TUESDAY), "cash": "1.8"}]),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a dividend that went ex before the moment asked",
        ),
        pytest.param(
            _in_the_answer(declared=[{"ex_ms": NOW, "cash": "1.8"}]),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a dividend that goes ex at the very moment asked, when it is in the price already",
        ),
        pytest.param(
            _in_the_answer(declared=[{"ex_ms": session_open_ms_utc(_FRIDAY), "cash": "0"}]),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a dividend of nothing",
        ),
        pytest.param(
            _in_the_answer(
                declared=[
                    {"ex_ms": session_open_ms_utc(_NEXT_FRIDAY), "cash": "0.25"},
                    {"ex_ms": session_open_ms_utc(_FRIDAY), "cash": "1.8"},
                ]
            ),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="dividends out of the order they go ex",
        ),
        pytest.param(
            _in_the_answer(rate={"value": 0.05}),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a rate written as a number, not as text",
        ),
        pytest.param(
            _in_the_answer(rate={"source": "default"}),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="a default rate that does not say why",
        ),
        pytest.param(
            _in_the_answer(status="unknown"),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="dividends said to be unknown that come with a list",
        ),
        pytest.param(
            lambda _: httpx.Response(200, json={"status": "being_looked_up"}),
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
            id="still looking, said to an ask that waits",
        ),
    ],
)
def test_a_coordinator_that_gives_no_usable_answer_leaves_both_unknown_with_that_reason(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response], why: WhyNotKnown
) -> None:
    coordinator = Coordinator(monkeypatch)
    coordinator.answer = answer

    read = _read()

    assert _nothing_known(read) == (why, why)
    assert coordinator.vendors_asked == []


def test_a_timeout_a_failure_a_refusal_and_an_unreadable_answer_are_four_reasons_in_four_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(monkeypatch)
    said = []
    for answer in (
        _times_out,
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(401),
        lambda _: httpx.Response(200, content=b"not json at all"),
    ):
        option_model_inputs.clear_cache()
        coordinator.answer = answer
        read = _read()
        said.append((read.dividends_unknown_why, read.dividends_source, read.rate_source))

    assert len({why for why, _, _ in said}) == 4
    assert len({dividends for _, dividends, _ in said}) == 4
    assert len({rate for _, _, rate in said}) == 4


def test_the_coordinators_own_refusal_of_a_wrong_token_leaves_both_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real route answers 403 to a token it does not hold for this Clerk."""
    coordinator = Coordinator(monkeypatch)
    monkeypatch.setattr(fleet_settings, "AGENT_SERVICE_TOKEN", "svct_" + "9" * 32)

    read = _read()

    assert _nothing_known(read) == (WhyNotKnown.COORDINATOR_REFUSED, WhyNotKnown.COORDINATOR_REFUSED)
    assert coordinator.asked == []


@pytest.mark.parametrize("missing", ["COORDINATOR_URL", "AGENT_SERVICE_TOKEN", "CLERK_ID"])
def test_a_clerk_not_set_up_to_ask_sends_nothing_and_knows_nothing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    coordinator = Coordinator(monkeypatch)
    monkeypatch.setattr(fleet_settings, missing, None)

    read = _read()

    assert _nothing_known(read) == (WhyNotKnown.COORDINATOR_NOT_CONFIGURED, WhyNotKnown.COORDINATOR_NOT_CONFIGURED)
    assert (coordinator.sent, coordinator.vendors_asked) == ([], [])


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        pytest.param("COORDINATOR_URL", "http://127.0.0.1:port", id="an address whose port is no number"),
        pytest.param("COORDINATOR_URL", "http://[::1", id="an address that is half a bracket"),
        pytest.param("AGENT_SERVICE_TOKEN", "svct_" + "é" * 32, id="a token no header can carry"),
        pytest.param("CLERK_ID", "clrk_ünïcode", id="a Clerk id no header can carry"),
    ],
)
def test_settings_no_request_can_be_made_with_read_as_not_set_up_and_never_escape_as_an_error(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    coordinator = Coordinator(monkeypatch)
    monkeypatch.setattr(fleet_settings, setting, value)

    read = _read()

    assert _nothing_known(read) == (WhyNotKnown.COORDINATOR_NOT_CONFIGURED, WhyNotKnown.COORDINATOR_NOT_CONFIGURED)
    assert coordinator.sent == []
    # The reason names the setting, never what it was set to.
    assert value not in read.rate_source and value not in read.dividends_source


def _does_not_resolve(host: str, _port: object) -> list[tuple]:
    raise OSError(f"{host} does not resolve")


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("http://8.8.8.8:8000", id="a public address"),
        pytest.param("http://fleet-coordinator:8000", id="a name that does not resolve, as while it restarts"),
    ],
)
def test_a_coordinator_address_not_shown_to_be_private_is_sent_nothing(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """The ask carries this Clerk's token in the clear."""
    coordinator = Coordinator(monkeypatch)
    monkeypatch.setattr(fleet_settings, "COORDINATOR_URL", address)
    monkeypatch.setattr(internal_http.socket, "getaddrinfo", _does_not_resolve)

    read = _read()

    assert _nothing_known(read) == (WhyNotKnown.COORDINATOR_ADDRESS_REFUSED, WhyNotKnown.COORDINATOR_ADDRESS_REFUSED)
    assert coordinator.sent == []


@pytest.mark.parametrize(
    ("underlying", "at_ms"),
    [
        pytest.param("not a symbol!", NOW, id="an underlying that is no symbol"),
        pytest.param("SPY", 1, id="a moment no calendar holds"),
    ],
)
def test_a_question_the_coordinator_does_not_answer_is_not_sent_and_nothing_is_known(
    monkeypatch: pytest.MonkeyPatch, underlying: str, at_ms: int
) -> None:
    coordinator = Coordinator(monkeypatch)

    read = read_option_model_inputs(underlying, at_ms=at_ms, latest_expiry=_NEXT_FRIDAY)

    assert _nothing_known(read) == (WhyNotKnown.COORDINATOR_NOT_ASKED, WhyNotKnown.COORDINATOR_NOT_ASKED)
    assert coordinator.sent == []


def test_a_moment_read_off_an_array_is_asked_about_as_the_plain_number_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bar's time is often a numpy integer. The wire is strict, and would refuse it on a Clerk alone."""
    coordinator = Coordinator(monkeypatch)

    read = _read(at_ms=np.int64(NOW))

    assert read.dividends == READ.dividends and read.rate_is_default is False
    assert coordinator.asked == [("SPY", NOW, _NEXT_FRIDAY, True)]
    assert type(json.loads(coordinator.sent[0].content)["at_ms"]) is int


def test_a_reason_never_names_the_coordinators_address_or_this_clerks_token(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = Coordinator(monkeypatch)
    coordinator.answer = lambda _: httpx.Response(500, text=f"upstream {TOKEN} at 127.0.0.1:8000 failed")

    read = _read()

    for sentence in (read.rate_source, read.dividends_source):
        assert TOKEN not in sentence and "127.0.0.1" not in sentence


# ---- What is kept, and waiting -----------------------------------------------------------


def test_what_the_coordinator_answered_is_kept_and_a_failure_is_asked_again_after_half_a_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(monkeypatch)
    # A failure's half minute runs from when its ask ended, on this process's own clock. That clock stands
    # still here, so an ask takes no time and the half minute ends at exactly thirty seconds, on any machine.
    monkeypatch.setattr(option_model_inputs, "_monotonic", lambda: 500.0)

    for later in (0, 1_000, 59 * 60_000):
        _read(at_ms=NOW + later)
    asks_while_known = len(coordinator.sent)

    option_model_inputs.clear_cache()
    coordinator.answer = _times_out
    for later in (0, 29_999):
        _read(at_ms=NOW + later)
    asks_while_failing = len(coordinator.sent) - asks_while_known
    coordinator.answer = coordinator._through_the_route
    recovered = _read(at_ms=NOW + 30_000)

    assert (asks_while_known, asks_while_failing) == (1, 1)
    assert len(coordinator.sent) == 3
    assert recovered.dividends == READ.dividends and recovered.rate_is_default is False


def test_dividends_the_coordinator_had_already_kept_a_while_are_kept_only_for_the_rest_of_their_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from Polygon fifty minutes ago: ten minutes are left of the hour, not sixty."""
    coordinator = Coordinator(monkeypatch, replace(READ, dividends_read_ms=NOW - 50 * 60_000))

    first = _read()
    _read(at_ms=NOW + 10 * 60_000 - 1)
    asked_inside_the_hour = len(coordinator.sent)
    _read(at_ms=NOW + 10 * 60_000)

    assert first.dividends_read_ms == NOW - 50 * 60_000
    assert (asked_inside_the_hour, len(coordinator.sent)) == (1, 2)


def test_a_dividend_that_goes_ex_while_the_answer_is_kept_is_left_out_without_asking_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wednesday_open = session_open_ms_utc(_WEDNESDAY)
    going_ex_soon = replace(
        READ,
        dividends=(DividendOn(ex_ms=wednesday_open, cash=0.4), *READ.dividends),
        dividends_read_ms=wednesday_open - 60_000,
    )
    coordinator = Coordinator(monkeypatch, going_ex_soon)

    before = _read(at_ms=wednesday_open - 60_000)
    after = _read(at_ms=wednesday_open)

    assert [paid.cash for paid in before.dividends] == [0.4, 1.8, 0.470625]
    assert [paid.cash for paid in after.dividends] == [1.8, 0.470625]
    assert len(coordinator.sent) == 1


def test_an_ask_that_will_not_wait_is_told_the_coordinator_is_still_looking_and_keeps_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page asks every second. It is not answered "unknown" for a lookup that is only still running."""
    coordinator = Coordinator(monkeypatch, ModelInputsBeingRead("another Clerk's lookup is running"))

    with pytest.raises(ModelInputsBeingRead):
        _read(wait=False)
    coordinator.read = READ
    a_second_later = _read(at_ms=NOW + 1_000, wait=False)

    assert [wait for _, _, _, wait in coordinator.asked] == [False, False]
    assert a_second_later.dividends == READ.dividends


def test_a_caller_that_will_not_wait_is_not_made_to_while_this_clerk_is_already_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(monkeypatch)
    asked, let_go = threading.Event(), threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        asked.set()
        let_go.wait(timeout=20)
        return coordinator._through_the_route(request)

    coordinator.answer = slow
    first: list[ModelInputsRead] = []
    asking = threading.Thread(target=lambda: first.append(_read()), daemon=True)
    asking.start()
    assert asked.wait(timeout=5)
    try:
        with pytest.raises(ModelInputsBeingRead):
            _read(wait=False)
    finally:
        let_go.set()
        asking.join(timeout=5)

    # The one ask already on its way is the only one made, and its answer is the first caller's.
    assert len(coordinator.sent) == 1
    assert [read.dividends for read in first] == [READ.dividends]


def test_one_ask_of_the_coordinator_answers_for_the_rate_and_the_dividends_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two are looked up apart, each in a thread of its own. On a Clerk both come from the one answer."""
    coordinator = Coordinator(monkeypatch)

    for _ in range(5):
        option_model_inputs.clear_cache()
        _read()

    assert len(coordinator.sent) == 5


def test_a_coordinator_still_looking_is_asked_once_and_told_to_both_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = Coordinator(monkeypatch, ModelInputsBeingRead("another Clerk's lookup is running"))

    with pytest.raises(ModelInputsBeingRead):
        _read(wait=False)

    assert len(coordinator.sent) == 1


def test_a_default_rate_from_the_coordinator_is_asked_again_half_a_minute_after_the_ask_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator took eight seconds to say FRED had not answered. The half minute runs from then."""
    coordinator = Coordinator(monkeypatch, replace(_RATE_ONLY_THE_DEFAULT, rate_default_why=WhyNotKnown.LOOKUP_TOO_SLOW))
    own_clock = [500.0]
    monkeypatch.setattr(option_model_inputs, "_monotonic", lambda: own_clock[0])

    def eight_seconds_later(request: httpx.Request) -> httpx.Response:
        own_clock[0] += 8.0
        return coordinator._through_the_route(request)

    coordinator.answer = eight_seconds_later
    first = _read()
    _read(at_ms=NOW + 8_000 + 29_999)
    asked_inside_the_half_minute = len(coordinator.sent)
    _read(at_ms=NOW + 8_000 + 30_000)

    # Known dividends, and a rate marked as the default with the coordinator's own reason.
    assert first.dividends == READ.dividends and first.dividends_unknown_why is None
    assert (first.rate, first.rate_is_default, first.rate_default_why) == (
        DEFAULT_RISK_FREE_RATE, True, WhyNotKnown.LOOKUP_TOO_SLOW,
    )
    assert (asked_inside_the_half_minute, len(coordinator.sent)) == (1, 2)


def test_an_expiry_already_past_still_reads_the_rate_and_has_no_dividends_to_look_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(monkeypatch, replace(READ, dividends=()))

    read = _read(latest_expiry=date(2026, 10, 2))

    assert (read.rate, read.dividends) == (0.1 + 0.2, ())
    assert coordinator.asked == [("SPY", NOW, date(2026, 10, 2), True)]


# ---- A process with keys of its own ---------------------------------------------------------


@pytest.mark.parametrize("role", ["combined", "fleet_coordinator"])
def test_a_process_with_keys_of_its_own_reads_the_vendors_and_never_asks_the_coordinator(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    coordinator = Coordinator(monkeypatch)
    monkeypatch.setattr(fleet_settings, "ROLE", role)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "a-key")

    read = _read()

    # Each once, and at the same time: neither waits for the other.
    assert sorted(coordinator.vendors_asked) == ["FRED", "Polygon"]
    assert (coordinator.sent, coordinator.asked) == ([], [])
    assert (read.rate, read.dividends) == (0.0, ())
