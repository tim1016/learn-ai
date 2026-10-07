"""The rate and the dividends an option is priced with: what was looked up, and what could not be."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.config import fleet_settings, settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.model_input_reasons import WhyNotKnown
from app.options.resolver import DividendOn
from app.services import option_model_inputs
from app.services.fred_service import RATE_SOURCE_DEFAULT, RATE_SOURCE_FRED
from app.services.option_model_inputs import (
    DeclaredDividend,
    DividendsUnknown,
    ModelInputsBeingRead,
    declared_dividend_lookup,
    declared_dividends_from_polygon,
    read_option_model_inputs,
)
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.timestamps import to_ms_utc

_NY = ZoneInfo("America/New_York")
_TUESDAY = date(2026, 10, 6)
_WEDNESDAY = date(2026, 10, 7)
_FRIDAY = date(2026, 10, 9)
_NEXT_FRIDAY = date(2026, 10, 16)
# Tuesday 2026-10-06 at 10:31:02 New York time: the session has been open an hour.
NOW = to_ms_utc(datetime(2026, 10, 6, 10, 31, 2, tzinfo=_NY))


@pytest.fixture(autouse=True)
def _nothing_kept() -> Iterator[None]:
    option_model_inputs.clear_cache()
    yield
    option_model_inputs.clear_cache()


class Hangs:
    """A vendor that does not answer until the test lets it go."""

    def __init__(self) -> None:
        self.asked = threading.Event()
        self.let_go = threading.Event()
        self.times_asked = 0

    def _hang(self) -> None:
        self.times_asked += 1
        self.asked.set()
        # Long enough to outlast any read here; a test that forgot to let it go does not hang the run.
        self.let_go.wait(timeout=20)

    def rate(self, _days: int, _today: date) -> tuple[float, str]:
        self._hang()
        return 0.0512, RATE_SOURCE_FRED

    def dividends(self, _underlying: str, _first: date, _last: date) -> list[DeclaredDividend]:
        self._hang()
        return []


@pytest.fixture
def hung() -> Iterator[Hangs]:
    vendor = Hangs()
    yield vendor
    vendor.let_go.set()


class _OwnClock:
    """This process's own clock, as the lookups read it: seconds that only the test moves."""

    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def own_clock(monkeypatch: pytest.MonkeyPatch) -> _OwnClock:
    clock = _OwnClock()
    monkeypatch.setattr(option_model_inputs, "_monotonic", clock)
    return clock


@contextmanager
def a_read_under_way(underlying: str, hung: Hangs, **lookups: object) -> Iterator[None]:
    """Another caller's read of ``underlying``, waiting on a vendor that hangs."""
    reading = threading.Thread(
        target=read_option_model_inputs,
        args=(underlying,),
        kwargs={"at_ms": NOW, "latest_expiry": _NEXT_FRIDAY, **lookups},
        daemon=True,
    )
    reading.start()
    assert hung.asked.wait(timeout=5)
    try:
        yield
    finally:
        hung.let_go.set()
        reading.join(timeout=5)


class Lookups:
    """Stand-ins for FRED and for the dividend source, counting what they were asked."""

    def __init__(self, declared: list[DeclaredDividend] | Exception, *, rate: tuple[float, str] = (0.0512, RATE_SOURCE_FRED)) -> None:
        self.declared = declared
        self.rate_answer = rate
        self.rate_asked: list[tuple[int, date]] = []
        self.dividends_asked: list[tuple[str, date, date]] = []

    def rate(self, days: int, today: date) -> tuple[float, str]:
        self.rate_asked.append((days, today))
        return self.rate_answer

    def dividends(self, underlying: str, first: date, last: date) -> list[DeclaredDividend]:
        self.dividends_asked.append((underlying, first, last))
        if isinstance(self.declared, Exception):
            raise self.declared
        return self.declared

    def read(self, *, at_ms: int = NOW, latest_expiry: date = _NEXT_FRIDAY, underlying: str = "spy"):
        return read_option_model_inputs(
            underlying, at_ms=at_ms, latest_expiry=latest_expiry, rate=self.rate, dividends=self.dividends
        )


def test_the_rate_is_asked_for_the_days_left_to_the_latest_expiry_and_its_source_is_kept() -> None:
    lookups = Lookups([])

    read = lookups.read()

    assert lookups.rate_asked == [(10, _TUESDAY)]
    assert (read.rate, read.model.rate) == (0.0512, 0.0512)
    assert read.model.rate_source == read.rate_source


def test_a_rate_that_is_only_the_default_says_so_in_its_source() -> None:
    from_fred = Lookups([]).read()
    option_model_inputs.clear_cache()

    fell_back = Lookups([], rate=(DEFAULT_RISK_FREE_RATE, RATE_SOURCE_DEFAULT)).read()

    assert (from_fred.rate_is_default, fell_back.rate_is_default) == (False, True)
    assert fell_back.rate == DEFAULT_RISK_FREE_RATE
    assert fell_back.rate_source != from_fred.rate_source


def test_a_declared_dividend_goes_ex_at_the_open_of_its_ex_dates_session() -> None:
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80), DeclaredDividend(_NEXT_FRIDAY, 0.25)])

    read = lookups.read()

    assert lookups.dividends_asked == [("SPY", _TUESDAY, _NEXT_FRIDAY)]
    assert read.dividends == (
        DividendOn(ex_ms=session_open_ms_utc(_FRIDAY), cash=1.80),
        DividendOn(ex_ms=session_open_ms_utc(_NEXT_FRIDAY), cash=0.25),
    )
    assert read.model.dividends == read.dividends
    assert read.dividends_read_ms == NOW


def test_a_dividend_whose_session_has_already_opened_is_in_the_price_and_left_out() -> None:
    """Going ex today. An hour into the session the stock already trades without it."""
    lookups = Lookups([DeclaredDividend(_TUESDAY, 1.80), DeclaredDividend(_WEDNESDAY, 0.40)])

    after_the_open = lookups.read()
    option_model_inputs.clear_cache()
    before_the_open = lookups.read(at_ms=session_open_ms_utc(_TUESDAY) - 1)

    assert [paid.cash for paid in after_the_open.dividends] == [0.40]
    assert [paid.cash for paid in before_the_open.dividends] == [1.80, 0.40]


def test_none_declared_is_known_and_is_an_empty_list_not_an_unknown() -> None:
    read = Lookups([]).read()

    assert read.dividends == ()
    assert read.model is not None and read.model.dividends == ()


def test_a_lookup_that_fails_leaves_the_dividends_unknown_and_never_none_declared() -> None:
    lookups = Lookups(DividendsUnknown("the source did not answer"))

    read = lookups.read()

    assert read.dividends is None
    assert read.model is None
    assert read.dividends_source == "the source did not answer"
    # The rate is still the rate.
    assert read.rate == 0.0512


def test_what_stands_in_for_unknown_dividends_says_in_its_source_that_it_is_not_known() -> None:
    unknown = Lookups(DividendsUnknown("the source did not answer")).read()
    known = Lookups([]).read(underlying="QQQ")

    stand_in = unknown.unverified_model()

    assert stand_in.dividends == ()
    # Its source is not the lookup's own words alone: it says the stand-in is one, and why.
    assert stand_in.dividends_source != unknown.dividends_source
    assert unknown.dividends_source in stand_in.dividends_source
    with pytest.raises(ValueError):
        known.unverified_model()


def test_what_was_learned_is_kept_for_an_hour_and_a_failure_is_asked_again_after_half_a_minute() -> None:
    known, failing = Lookups([DeclaredDividend(_FRIDAY, 1.80)]), Lookups(DividendsUnknown("no answer"))

    for later in (0, 59 * 60_000, 60 * 60_000 - 1):
        known.read(at_ms=NOW + later)
    asked_within_the_hour = len(known.dividends_asked)
    known.read(at_ms=NOW + 60 * 60_000)
    for later in (0, 29_999):
        failing.read(at_ms=NOW + later, underlying="QQQ")
    failures_asked_within_half_a_minute = len(failing.dividends_asked)
    failing.read(at_ms=NOW + 30_000, underlying="QQQ")

    assert (asked_within_the_hour, len(known.dividends_asked)) == (1, 2)
    assert (failures_asked_within_half_a_minute, len(failing.dividends_asked)) == (1, 2)
    assert len(known.rate_asked) == 2


def test_a_failure_kept_is_still_unknown_when_read_again() -> None:
    failing = Lookups(DividendsUnknown("no answer"))

    first, again = failing.read(), failing.read(at_ms=NOW + 1_000)

    assert (first.dividends, again.dividends) == (None, None)


def test_polygons_rows_become_the_declared_cash_dividends_in_the_span_asked() -> None:
    rows = [
        {"ex_dividend_date": "2026-10-16", "cash_amount": 0.25, "currency": "USD", "dividend_type": "SC"},
        {"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "usd", "dividend_type": "CD"},
        # Outside what was asked: not counted.
        {"ex_dividend_date": "2026-12-18", "cash_amount": 1.9, "currency": "USD"},
    ]

    declared = declared_dividends_from_polygon("SPY", rows, _TUESDAY, _NEXT_FRIDAY)

    assert declared == (DeclaredDividend(_FRIDAY, 1.8), DeclaredDividend(_NEXT_FRIDAY, 0.25))
    assert declared_dividends_from_polygon("SPY", [], _TUESDAY, _NEXT_FRIDAY) == ()


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"ex_dividend_date": "2026-10-09", "cash_amount": None, "currency": "USD"}, id="no amount"),
        pytest.param({"cash_amount": 1.8, "currency": "USD"}, id="no ex-date"),
        pytest.param({"ex_dividend_date": "soon", "cash_amount": 1.8}, id="an ex-date that is no date"),
        pytest.param({"ex_dividend_date": "2026-10-09", "cash_amount": 0.0}, id="an amount of zero"),
        pytest.param({"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "CAD"}, id="not in dollars"),
        pytest.param({"ex_dividend_date": "2026-10-09", "cash_amount": float("nan")}, id="an amount that is no number"),
    ],
)
def test_a_row_that_cannot_be_read_makes_the_whole_answer_unknown(row: dict) -> None:
    """Leaving the row out would be pricing as if the dividend were not there."""
    good = {"ex_dividend_date": "2026-10-16", "cash_amount": 0.25, "currency": "USD"}

    with pytest.raises(DividendsUnknown):
        declared_dividends_from_polygon("SPY", [good, row], _TUESDAY, _NEXT_FRIDAY)


def _a_clerk_not_set_up_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_settings, "ROLE", "clerk_agent")
    for name in ("COORDINATOR_URL", "CLERK_ID", "AGENT_SERVICE_TOKEN"):
        monkeypatch.setattr(fleet_settings, name, None)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "")
    monkeypatch.setattr(option_model_inputs, "_from_polygon", lambda *_: pytest.fail("a Clerk asked Polygon"))
    monkeypatch.setattr(option_model_inputs, "_fred_rate", lambda *_: pytest.fail("a Clerk asked FRED"))


def test_on_a_clerk_a_rate_lookup_handed_in_answers_for_the_rate_and_the_dividends_stay_the_coordinators_to_give(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Clerk boots with no Polygon key (#2204), and this one has no coordinator to ask. It must not answer
    that no dividend is declared."""
    _a_clerk_not_set_up_to_ask(monkeypatch)

    read = read_option_model_inputs("SPY", at_ms=NOW, latest_expiry=_FRIDAY, rate=lambda _days, _today: (0.05, RATE_SOURCE_FRED))

    assert read.dividends is None and read.model is None
    assert read.dividends_unknown_why == WhyNotKnown.COORDINATOR_NOT_CONFIGURED
    # The lookup handed in answers for its own fact, and for no other.
    assert (read.rate, read.rate_is_default, read.rate_default_why) == (0.05, False, None)
    with pytest.raises(DividendsUnknown):
        declared_dividend_lookup()("SPY", _TUESDAY, _FRIDAY)


def test_on_a_clerk_a_dividend_lookup_handed_in_answers_for_the_dividends_and_the_rate_stays_the_coordinators_to_give(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _a_clerk_not_set_up_to_ask(monkeypatch)

    read = read_option_model_inputs(
        "SPY", at_ms=NOW, latest_expiry=_FRIDAY, dividends=lambda *_: [DeclaredDividend(_FRIDAY, 1.80)]
    )

    assert read.dividends == (DividendOn(ex_ms=session_open_ms_utc(_FRIDAY), cash=1.80),)
    assert (read.rate, read.rate_is_default, read.rate_default_why) == (
        DEFAULT_RISK_FREE_RATE, True, WhyNotKnown.COORDINATOR_NOT_CONFIGURED,
    )


def test_a_process_with_a_polygon_key_of_its_own_looks_there(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict] = []

    class Polygon:
        def list_dividends(self, **query: object) -> list[dict]:
            asked.append(query)
            return [{"ex_dividend_date": "2026-10-09", "cash_amount": 1.8, "currency": "USD"}]

    monkeypatch.setattr(option_model_inputs.fleet_settings, "ROLE", "combined")
    monkeypatch.setattr(option_model_inputs.settings, "POLYGON_API_KEY", "a-key")
    monkeypatch.setattr(option_model_inputs, "_polygon", Polygon())

    read = read_option_model_inputs("SPY", at_ms=NOW, latest_expiry=_FRIDAY, rate=lambda _days, _today: (0.05, RATE_SOURCE_FRED))

    assert asked == [{"ticker": "SPY", "ex_dividend_date_gte": "2026-10-06", "ex_dividend_date_lte": "2026-10-09"}]
    assert read.dividends == (DividendOn(ex_ms=session_open_ms_utc(_FRIDAY), cash=1.8),)


def test_polygon_failing_is_unknown_not_none_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    class Polygon:
        def list_dividends(self, **_query: object) -> list[dict]:
            raise RuntimeError("503 from Polygon")

    monkeypatch.setattr(option_model_inputs.fleet_settings, "ROLE", "fleet_coordinator")
    monkeypatch.setattr(option_model_inputs.settings, "POLYGON_API_KEY", "a-key")
    monkeypatch.setattr(option_model_inputs, "_polygon", Polygon())

    read = read_option_model_inputs("SPY", at_ms=NOW, latest_expiry=_FRIDAY, rate=lambda _days, _today: (0.05, RATE_SOURCE_FRED))

    assert read.dividends is None and read.model is None


def test_a_caller_that_will_not_wait_is_told_a_lookup_is_running_and_still_gets_what_is_already_kept(hung: Hangs) -> None:
    """A page asks every second. It must not stack a waiting thread behind a lookup that is already running."""
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80)])
    lookups.read()

    with a_read_under_way("QQQ", hung, rate=lookups.rate, dividends=hung.dividends):
        kept = read_option_model_inputs(
            "SPY", at_ms=NOW + 1_000, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=lookups.dividends, wait=False
        )
        with pytest.raises(ModelInputsBeingRead):
            read_option_model_inputs(
                "QQQ", at_ms=NOW, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=lookups.dividends, wait=False
            )

    assert [paid.cash for paid in kept.dividends] == [1.80]
    # The lookup that was running is the only one QQQ was given: the ask that would not wait began none.
    assert lookups.dividends_asked == [("SPY", _TUESDAY, _NEXT_FRIDAY)]
    assert hung.times_asked == 1


@pytest.mark.parametrize("wait", [True, False], ids=["a caller that waits", "a caller that will not"])
def test_a_slow_lookup_for_one_ticker_holds_up_nobody_asking_about_another(hung: Hangs, wait: bool) -> None:
    """QQQ's dividends hang at the vendor. IWM's are asked for meanwhile, and answered."""
    lookups = Lookups([DeclaredDividend(_FRIDAY, 0.55)])
    lookups.read()

    with a_read_under_way("QQQ", hung, rate=lookups.rate, dividends=hung.dividends):
        about_another = read_option_model_inputs(
            "IWM", at_ms=NOW, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=lookups.dividends, wait=wait
        )

    assert [paid.cash for paid in about_another.dividends] == [0.55]
    assert ("IWM", _TUESDAY, _NEXT_FRIDAY) in lookups.dividends_asked


def test_a_caller_that_waits_joins_the_lookup_already_running_and_begins_no_second_one(hung: Hangs) -> None:
    lookups = Lookups([])
    lookups.read()
    joined: list[object] = []

    with a_read_under_way("QQQ", hung, rate=lookups.rate, dividends=hung.dividends):
        joining = threading.Thread(
            target=lambda: joined.append(
                read_option_model_inputs("QQQ", at_ms=NOW, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=lookups.dividends)
            ),
            daemon=True,
        )
        joining.start()
        joining.join(timeout=0.2)
        still_waiting = joining.is_alive()
        hung.let_go.set()
        joining.join(timeout=5)

    assert still_waiting
    assert [read.dividends for read in joined] == [()]
    assert hung.times_asked == 1 and ("QQQ", _TUESDAY, _NEXT_FRIDAY) not in lookups.dividends_asked


# ---- A vendor that hangs, and what is kept ------------------------------------------------


def test_a_rate_lookup_that_hangs_leaves_the_rate_the_default_with_its_reason_and_the_dividends_as_they_were_learned(
    monkeypatch: pytest.MonkeyPatch, hung: Hangs
) -> None:
    """Each fact has its own bound. Polygon answered at once: its answer is not lost to a FRED that hangs."""
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80)])

    read = read_option_model_inputs("SPY", at_ms=NOW, latest_expiry=_NEXT_FRIDAY, rate=hung.rate, dividends=lookups.dividends)

    assert [paid.cash for paid in read.dividends] == [1.80] and read.dividends_unknown_why is None
    assert (read.rate, read.rate_is_default, read.rate_default_why) == (
        DEFAULT_RISK_FREE_RATE, True, WhyNotKnown.LOOKUP_TOO_SLOW,
    )
    assert read.model is not None


def test_a_dividend_lookup_that_hangs_leaves_the_dividends_unknown_with_its_reason_and_the_rate_as_it_was_learned(
    monkeypatch: pytest.MonkeyPatch, hung: Hangs
) -> None:
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    lookups = Lookups([])

    read = read_option_model_inputs("SPY", at_ms=NOW, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=hung.dividends)

    # Not known is never none declared.
    assert read.dividends is None and read.model is None
    assert read.dividends_unknown_why == WhyNotKnown.LOOKUP_TOO_SLOW and "SPY" in read.dividends_source
    assert (read.rate, read.rate_is_default) == (0.0512, False)


def test_a_lookup_given_up_on_is_asked_again_half_a_minute_after_the_read_stopped_waiting_for_it(
    monkeypatch: pytest.MonkeyPatch, hung: Hangs, own_clock: _OwnClock
) -> None:
    """The half minute runs from when the lookup was over for its reader, eight seconds in, not from the ask."""
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    lookups = Lookups([])

    def hangs_eight_seconds(days: int, today: date) -> tuple[float, str]:
        own_clock.now += 8.0
        return hung.rate(days, today)

    def read(later_ms: int):
        return read_option_model_inputs(
            "SPY", at_ms=NOW + later_ms, latest_expiry=_NEXT_FRIDAY, rate=hangs_eight_seconds, dividends=lookups.dividends
        )

    first = read(0)
    read(8_000 + 29_999)
    asked_inside_the_half_minute = hung.times_asked
    read(8_000 + 30_000)

    assert first.rate_default_why == WhyNotKnown.LOOKUP_TOO_SLOW
    assert (asked_inside_the_half_minute, hung.times_asked) == (1, 2)


def test_a_failure_is_asked_again_half_a_minute_after_its_lookup_ended_however_long_it_took(own_clock: _OwnClock) -> None:
    lookups = Lookups(DividendsUnknown("no answer"))

    def three_seconds_to_fail(underlying: str, first: date, last: date) -> list[DeclaredDividend]:
        own_clock.now += 3.0
        return lookups.dividends(underlying, first, last)

    def read(later_ms: int):
        return read_option_model_inputs(
            "SPY", at_ms=NOW + later_ms, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=three_seconds_to_fail
        )

    read(0)
    read(3_000 + 29_999)
    asked_inside_the_half_minute = len(lookups.dividends_asked)
    read(3_000 + 30_000)

    assert (asked_inside_the_half_minute, len(lookups.dividends_asked)) == (1, 2)


def test_a_lookup_given_up_on_that_learns_the_fact_after_all_is_kept(monkeypatch: pytest.MonkeyPatch, hung: Hangs) -> None:
    """FRED was only slow. The read that would not wait longer said so; the next one finds the rate it gave."""
    monkeypatch.setattr(option_model_inputs, "VENDOR_LOOKUP_BOUND_S", 0.2)
    lookups = Lookups([])

    def read(later_ms: int):
        return read_option_model_inputs(
            "SPY", at_ms=NOW + later_ms, latest_expiry=_NEXT_FRIDAY, rate=hung.rate, dividends=lookups.dividends
        )

    gave_up = read(0)
    hung.let_go.set()
    for _ in range(100):
        learned = read(1_000)
        if not learned.rate_is_default:
            break
        threading.Event().wait(0.02)

    assert gave_up.rate_default_why == WhyNotKnown.LOOKUP_TOO_SLOW
    assert (learned.rate, learned.rate_is_default) == (0.0512, False)
    assert hung.times_asked == 1


def test_only_so_many_answers_are_kept_and_the_oldest_go_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(option_model_inputs, "MOST_KEPT", 3)
    lookups = Lookups([])
    tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "META"]

    for ticker in tickers:
        lookups.read(underlying=ticker)
    lookups.read(underlying="META")
    asked_for_the_newest = lookups.dividends_asked.count(("META", _TUESDAY, _NEXT_FRIDAY))
    lookups.read(underlying="AAPL")

    assert asked_for_the_newest == 1
    assert lookups.dividends_asked.count(("AAPL", _TUESDAY, _NEXT_FRIDAY)) == 2
    assert len(option_model_inputs._dividends) <= 3


def test_what_is_no_longer_kept_is_dropped_when_something_newer_is_learned() -> None:
    lookups = Lookups(DividendsUnknown("no answer"))

    lookups.read(underlying="AAPL")
    lookups.read(underlying="MSFT", at_ms=NOW + 30_000)

    assert len(option_model_inputs._dividends) == 1


def test_a_moment_read_off_an_array_is_the_same_moment() -> None:
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80)])

    read = lookups.read(at_ms=np.int64(NOW))

    assert (read.dividends_read_ms, type(read.dividends_read_ms)) == (NOW, int)
    with pytest.raises(TypeError):
        lookups.read(at_ms=float(NOW))


def test_an_expiry_already_past_has_nothing_to_look_up() -> None:
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80)])

    read = lookups.read(latest_expiry=date(2026, 10, 2))

    assert (read.dividends, lookups.dividends_asked) == ((), [])
