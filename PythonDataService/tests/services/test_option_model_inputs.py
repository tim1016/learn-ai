"""The rate and the dividends an option is priced with: what was looked up, and what could not be."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import session_open_ms_utc
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


def test_a_fleet_clerk_has_nowhere_to_look_and_says_unknown_without_asking_polygon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Clerk boots with no Polygon key (ADR 0062). It must not answer that no dividend is declared."""
    monkeypatch.setattr(option_model_inputs.fleet_settings, "ROLE", "clerk_agent")
    monkeypatch.setattr(option_model_inputs.settings, "POLYGON_API_KEY", "")
    monkeypatch.setattr(option_model_inputs, "_from_polygon", lambda *_: pytest.fail("a Clerk asked Polygon"))

    read = read_option_model_inputs("SPY", at_ms=NOW, latest_expiry=_FRIDAY, rate=lambda _days, _today: (0.05, RATE_SOURCE_FRED))

    assert read.dividends is None and read.model is None
    with pytest.raises(DividendsUnknown):
        declared_dividend_lookup()("SPY", _TUESDAY, _FRIDAY)


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


def test_a_caller_that_will_not_wait_is_told_a_lookup_is_running_and_still_gets_what_is_already_kept() -> None:
    """A page asks every second. It must not stack a waiting thread behind a lookup that is already running."""
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80)])
    lookups.read()

    assert option_model_inputs._lookups.acquire(blocking=False)
    try:
        kept = read_option_model_inputs(
            "SPY", at_ms=NOW + 1_000, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=lookups.dividends, wait=False
        )
        with pytest.raises(ModelInputsBeingRead):
            read_option_model_inputs(
                "QQQ", at_ms=NOW, latest_expiry=_NEXT_FRIDAY, rate=lookups.rate, dividends=lookups.dividends, wait=False
            )
    finally:
        option_model_inputs._lookups.release()

    assert [paid.cash for paid in kept.dividends] == [1.80]
    assert lookups.dividends_asked == [("SPY", _TUESDAY, _NEXT_FRIDAY)]


def test_an_expiry_already_past_has_nothing_to_look_up() -> None:
    lookups = Lookups([DeclaredDividend(_FRIDAY, 1.80)])

    read = lookups.read(latest_expiry=date(2026, 10, 2))

    assert (read.dividends, lookups.dividends_asked) == ((), [])
