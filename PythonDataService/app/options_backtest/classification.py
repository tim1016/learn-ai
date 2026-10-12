"""The series an option expiry belongs to in the past-chain browser, decided from the calendar (#3030).

Formula: with ``E`` an expiry that is in the listing of a trading date ``T`` on or after
  ``FIRST_TAGGED_TRADING_DATE``, and ``F`` the third Friday of ``E``'s month:
    unknown     E is not a session, or T is before the floor
    monthly     E = F, or E = the last session before F when F is not a session
    quarterly   E is the last session of March, June, September or December
    weekly      E is the last session of its Monday-to-Sunday week
    daily       any other session
  The tests run in that order. A quarter's last session is never the monthly date (the monthly falls on
  the 14th to the 21st, the quarter's last session in the last days of its month), so "quarterly, when it
  is not the monthly" never has to choose.
Reference: the owner's definition of each series for #3030; no exchange notice was consulted, which is why
  the floor below exists. Each session fact is ``app/lean_sidecar/trading_calendar.py``'s, read through
  ``app/options_backtest/sessions.py``.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_classification.py.

A tag is a claim about a date, never about a contract: it is the same whatever else is listed, and it
says nothing of whether the underlying lists contracts for that series. Before the floor the pattern is
not trusted, and an answer of "unknown" is given in its place. An expiry that expires on the trading
date itself is tagged by its series, and ``expires_today`` is a separate fact, so a monthly on its last
day is never read as a daily.

The tag is versioned: a run or a saved page that shows one can say which rules made it.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from app.options_backtest.sessions import ends_its_week, is_session, sessions_of_week

CLASSIFICATION_VERSION = 1

# Before this trading date no series is claimed. Mid-week expiries reached different underlyings at
# different times, and a date from before a product had them would be mislabelled by the pattern here.
# A chosen cut-off, and not an exchange's date: move it only with a source for the underlying it concerns.
FIRST_TAGGED_TRADING_DATE = date(2024, 10, 1)

_FRIDAY = 4
_DAYS_FROM_FIRST_TO_THIRD_FRIDAY = 14
_MONTHS_PER_QUARTER = 3
_LOOKBACK_WEEKS = 4


class ExpirySeries(StrEnum):
    """The series an expiry date belongs to."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExpiryTag:
    """The series of one listed expiry, and what decided it."""

    series: ExpirySeries
    version: int
    # Which rule decided, in a few plain words.
    evidence: str
    # The expiry is the trading date itself. It says nothing about the series.
    expires_today: bool
    # Calendar days from the trading date.
    days_to_expiry: int


def _last_session_on_or_before(day: date) -> date:
    for weeks_back in range(_LOOKBACK_WEEKS):
        sessions = [session for session in sessions_of_week(day - timedelta(weeks=weeks_back)) if session <= day]
        if sessions:
            return sessions[-1]
    raise LookupError(f"the calendar has no session in the {_LOOKBACK_WEEKS} weeks up to {day}")


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(_FRIDAY - first.weekday()) % 7)
    return first_friday + timedelta(days=_DAYS_FROM_FIRST_TO_THIRD_FRIDAY)


def standard_monthly_date(year: int, month: int) -> date:
    """The standard monthly expiry of a month: its third Friday, or the session before it when that is closed."""
    friday = _third_friday(year, month)
    return friday if is_session(friday) else _last_session_on_or_before(friday - timedelta(days=1))


def last_session_of_month(year: int, month: int) -> date:
    """The last day of a month the calendar has a session on."""
    first_of_next = date(year + (month == 12), month % 12 + 1, 1)
    return _last_session_on_or_before(first_of_next - timedelta(days=1))


def tag_expiry(expiry: date, *, trading_date: date, listed: Collection[date]) -> ExpiryTag:
    """The tag ``expiry`` carries on ``trading_date``, for an expiry that is in the day's ``listed`` dates.

    Raises ``ValueError`` for an expiry the listing does not hold and for one before the trading date:
    a listing as of a date holds neither, so either is the caller's mistake and not a market fact.
    """
    if expiry not in listed:
        raise ValueError(f"{expiry} is not an expiry in the listing for {trading_date}; only a listed expiry is tagged")
    days_to_expiry = (expiry - trading_date).days
    if days_to_expiry < 0:
        raise ValueError(f"{expiry} is before the trading date {trading_date}: a listing holds no expiry that has gone")

    def tag(series: ExpirySeries, evidence: str) -> ExpiryTag:
        return ExpiryTag(
            series=series,
            version=CLASSIFICATION_VERSION,
            evidence=evidence,
            expires_today=days_to_expiry == 0,
            days_to_expiry=days_to_expiry,
        )

    if trading_date < FIRST_TAGGED_TRADING_DATE:
        return tag(
            ExpirySeries.UNKNOWN,
            f"the trading date is before {FIRST_TAGGED_TRADING_DATE.isoformat()}, from when expiries are tagged",
        )
    if not is_session(expiry):
        return tag(ExpirySeries.UNKNOWN, "the date is not a session on the exchange calendar")
    if expiry == standard_monthly_date(expiry.year, expiry.month):
        if expiry.weekday() == _FRIDAY:
            return tag(ExpirySeries.MONTHLY, "the third Friday of its month")
        return tag(ExpirySeries.MONTHLY, "the session before the third Friday of its month, which is closed")
    if expiry.month % _MONTHS_PER_QUARTER == 0 and expiry == last_session_of_month(expiry.year, expiry.month):
        return tag(ExpirySeries.QUARTERLY, "the last session of its quarter")
    if ends_its_week(expiry):
        return tag(ExpirySeries.WEEKLY, "the last session of its week")
    return tag(ExpirySeries.DAILY, "a session that is not the last of its week")
