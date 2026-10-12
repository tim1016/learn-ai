"""How the records of a run name time: a session by its open in ms, and its New York date derived from that open (#3030).

Nothing a run returns holds a ``date``: a record that is stored or served carries time as ``int64 ms UTC`` (ADR 0022).
``SessionResult.trading_date`` is a read-only property, the New York calendar date of the open, for code that groups by
day or month inside one function.

A real session opens at 09:30 New York time, which is 13:30 or 14:30 UTC: the same calendar day in New York and in UTC.
So the derivation cannot go wrong on a real open, whichever zone it uses, and a test on real opens alone would pass
with the wrong zone. The cases below therefore set the instant by hand, at New York midnights and either side of them,
and through two years that hold all four clock changes, where the two zones name different days.
"""

from __future__ import annotations

import inspect
import random
from dataclasses import is_dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from app.options_backtest.results import SessionStatus
from app.options_backtest.run import RunResult
from app.options_backtest.session_result import SessionResult
from tests.options_backtest._results import a_session

NEW_YORK = ZoneInfo("America/New_York")
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR
EPOCH = date(1970, 1, 1)
_A_SESSION = a_session(date(2026, 10, 7), "10000.00", status=SessionStatus.NO_TRADE)


def _kept_at(open_ms: int) -> SessionResult:
    """A session kept with its open set by hand to any instant; a real session opens only at 13:30 or 14:30 UTC."""
    return replace(_A_SESSION, session_open_ms=open_ms)


# The New York midnight that begins each date, in ms UTC, worked out as the UTC midnight of the date (its day number,
# days since 1970-01-01, times 86,400,000) plus the hours New York is behind UTC that day: 5 on standard time (EST),
# 4 on summer time (EDT). Each is checked against the standard library's zone below. The 8th of March 2026 and the 14th
# of March 2027 are 23 hours long, the 1st of November 2026 and the 7th of November 2027 are 25 hours long.
_MIDNIGHTS = [
    (date(2026, 3, 8), 1_772_946_000_000),  # day 20,520: 1_772_928_000_000 + 5 h
    (date(2026, 3, 9), 1_773_028_800_000),  # day 20,521: 1_773_014_400_000 + 4 h
    (date(2026, 10, 1), 1_790_827_200_000),  # day 20,727: 1_790_812_800_000 + 4 h
    (date(2026, 11, 1), 1_793_505_600_000),  # day 20,758: 1_793_491_200_000 + 4 h
    (date(2026, 11, 2), 1_793_595_600_000),  # day 20,759: 1_793_577_600_000 + 5 h
    (date(2026, 12, 1), 1_796_101_200_000),  # day 20,788: 1_796_083_200_000 + 5 h
    (date(2027, 3, 14), 1_805_000_400_000),  # day 20,891: 1_804_982_400_000 + 5 h
    (date(2027, 3, 15), 1_805_083_200_000),  # day 20,892: 1_805_068_800_000 + 4 h
    (date(2027, 11, 7), 1_825_560_000_000),  # day 21,129: 1_825_545_600_000 + 4 h
    (date(2027, 11, 8), 1_825_650_000_000),  # day 21,130: 1_825_632_000_000 + 5 h
]


@pytest.mark.parametrize(("day", "midnight_ms"), _MIDNIGHTS, ids=[str(day) for day, _ in _MIDNIGHTS])
def test_a_trading_date_turns_over_at_new_york_midnight_on_summer_time_and_on_standard_time(
    day: date, midnight_ms: int
) -> None:
    assert int(datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).timestamp() * 1000) == midnight_ms

    # One millisecond before midnight is still the day before; midnight itself, and 22:00 that evening, are the day.
    # (A fixed offset of four or five hours, or UTC, gets one of these wrong on one of the two kinds of clock.)
    assert _kept_at(midnight_ms - 1).trading_date == day - timedelta(days=1)
    assert _kept_at(midnight_ms).trading_date == day
    assert _kept_at(midnight_ms + 22 * MS_PER_HOUR).trading_date == day


def _nth_sunday(year: int, month: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=nth - 1)


def _utc_ms(day: date, hour: int) -> int:
    return (day - EPOCH).days * MS_PER_DAY + hour * MS_PER_HOUR


# The rule for these years: summer time begins on the second Sunday of March at 02:00 standard time, which is 07:00 UTC,
# and ends on the first Sunday of November at 02:00 summer time, which is 06:00 UTC. Each pair is (begins, ends) in ms.
_SUMMER_TIME = [(_utc_ms(_nth_sunday(year, 3, 2), 7), _utc_ms(_nth_sunday(year, 11, 1), 6)) for year in (2026, 2027)]


def _on_summer_time(ms: int) -> bool:
    return any(begins <= ms < ends for begins, ends in _SUMMER_TIME)


def _new_york_date_by_the_clock_rule(ms: int) -> date:
    """The New York date of an instant by integer arithmetic alone: shift by the hours behind UTC, take the day."""
    behind = 4 if _on_summer_time(ms) else 5
    return EPOCH + timedelta(days=(ms - behind * MS_PER_HOUR) // MS_PER_DAY)


def test_the_clock_rule_this_test_reads_dates_by_changes_the_new_york_clock_where_the_zone_does() -> None:
    # The rule is the test's own reference, so it is held to the standard library's zone: one millisecond before a
    # change New York is five hours behind UTC for the spring change and four for the autumn one, and at the change the
    # other way round. 02:00 EST on Sun 2026-03-08 is 07:00 UTC; 02:00 EDT on Sun 2026-11-01 is 06:00 UTC.
    def behind_utc_at(ms: int) -> timedelta:
        offset = datetime.fromtimestamp(ms / 1000, NEW_YORK).utcoffset()
        assert offset is not None
        return -offset

    five, four = timedelta(hours=5), timedelta(hours=4)
    for begins, ends in _SUMMER_TIME:
        assert (behind_utc_at(begins - 1), behind_utc_at(begins)) == (five, four)
        assert (behind_utc_at(ends - 1), behind_utc_at(ends)) == (four, five)
    assert [(begins % MS_PER_DAY, ends % MS_PER_DAY) for begins, ends in _SUMMER_TIME] == [
        (7 * MS_PER_HOUR, 6 * MS_PER_HOUR)
    ] * 2
    assert [datetime.fromtimestamp(ms / 1000, UTC).date() for span in _SUMMER_TIME for ms in span] == [
        date(2026, 3, 8),
        date(2026, 11, 1),
        date(2027, 3, 14),
        date(2027, 11, 7),
    ]


def test_a_trading_date_is_the_new_york_date_of_the_instant_all_through_two_years_of_clock_changes() -> None:
    rng = random.Random(20261012)
    first_ms = (date(2026, 1, 1) - EPOCH).days * MS_PER_DAY
    last_ms = (date(2028, 1, 1) - EPOCH).days * MS_PER_DAY
    centres = [ms for span in _SUMMER_TIME for ms in span] + [midnight for _, midnight in _MIDNIGHTS]
    instants = [rng.randrange(first_ms, last_ms) for _ in range(20_000)]
    instants += [centre + rng.randrange(-3 * MS_PER_HOUR, 3 * MS_PER_HOUR) for centre in centres * 100]

    wrong_in_utc = in_summer = in_winter = 0
    for ms in instants:
        expected = _new_york_date_by_the_clock_rule(ms)
        assert _kept_at(ms).trading_date == expected, ms
        wrong_in_utc += datetime.fromtimestamp(ms / 1000, tz=UTC).date() != expected
        in_summer += _on_summer_time(ms)
        in_winter += not _on_summer_time(ms)
    # The comparison could fail: many instants fall where UTC names another day, on both kinds of clock.
    assert wrong_in_utc >= 3_000
    assert in_summer >= 5_000 and in_winter >= 5_000


_CLOCK_TYPES = (date, datetime, time)


def _classes_in(annotation: object) -> list[type]:
    """Every class an annotation names, however deep: through ``tuple[...]``, ``X | None`` and ``Annotated``."""
    found = [annotation] if inspect.isclass(annotation) else []
    if get_origin(annotation) is not Literal:
        for argument in get_args(annotation):
            found += _classes_in(argument)
    return found


def _clock_fields_below(root: type) -> tuple[list[str], set[str]]:
    """Every field below ``root`` typed as a date, a datetime or a time, as ``Class.field``, and the classes walked."""
    found: list[str] = []
    seen: set[type] = set()
    pending = [root]
    while pending:
        record = pending.pop()
        if record in seen:
            continue
        seen.add(record)
        if is_dataclass(record):
            annotations = get_type_hints(record)
        elif issubclass(record, BaseModel):
            annotations = {name: field.annotation for name, field in record.model_fields.items()}
        else:
            continue
        for name, annotation in annotations.items():
            for each in _classes_in(annotation):
                if issubclass(each, _CLOCK_TYPES):
                    found.append(f"{record.__name__}.{name}")
                else:
                    pending.append(each)
    return sorted(found), {record.__name__ for record in seen}


def test_nothing_a_run_returns_holds_a_date_or_a_clock_time_at_any_depth() -> None:
    clocks, walked = _clock_fields_below(RunResult)

    assert clocks == []
    # The walk reached the records an owner reads and the ones inside the sessions, so an empty list means something.
    assert {
        "RunResult",
        "SessionResult",
        "SessionOutcome",
        "LegTrade",
        "Fill",
        "Event",
        "SessionPins",
        "PinnedDay",
        "OptionContract",
        "RunSummary",
        "DayRow",
        "MonthRow",
        "Drawdown",
        "SlippagePoint",
        "SlippageTolerance",
        "OptionsBacktestSetup",
    } <= walked
