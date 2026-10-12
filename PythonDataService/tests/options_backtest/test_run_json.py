"""The JSON form of a run's parts: exact, with no float and no date text, and the same thing on the way back (#3030)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.options_backtest import run_json
from app.options_backtest.run_json import record_of
from tests.options_backtest._a_run import a_busy_run, what_the_busy_run_reaches
from tests.options_backtest._market import OPEN_MS


def _no_float_anywhere(value: Any) -> None:
    assert not isinstance(value, float), f"a float is in the stored JSON: {value}"
    if isinstance(value, dict):
        for each in value.values():
            _no_float_anywhere(each)
    elif isinstance(value, list):
        for each in value:
            _no_float_anywhere(each)


def test_the_busy_run_reaches_every_kind_of_thing_a_result_holds() -> None:
    missing = [what for what, reached in what_the_busy_run_reaches(a_busy_run()).items() if not reached]

    assert missing == []


def test_the_summary_comes_back_as_it_went_and_holds_no_float() -> None:
    summary = a_busy_run().summary

    written = json.loads(json.dumps(run_json.summary_to_json(summary)))

    assert run_json.summary_from_json(written) == summary
    _no_float_anywhere(written)


def test_a_session_is_identified_by_its_open_and_no_date_is_written() -> None:
    summary = a_busy_run().summary

    written = json.dumps(run_json.summary_to_json(summary))

    assert "trading_date" not in written and "peak_date" not in written and "trough_date" not in written
    assert not re.search(r"\d{4}-\d{2}-\d{2}", written), "no date text is in the stored summary"
    assert [row.session_open_ms for row in run_json.summary_from_json(json.loads(written)).by_day] == [
        row.session_open_ms for row in summary.by_day
    ]


def test_the_ladder_and_the_tolerance_come_back_as_they_went() -> None:
    result = a_busy_run()

    ladder = json.loads(json.dumps(run_json.slippage_to_json(result.slippage)))
    tolerance = json.loads(json.dumps(run_json.tolerance_to_json(result.slippage_tolerance)))

    assert run_json.slippage_from_json(ladder) == result.slippage
    assert run_json.tolerance_from_json(tolerance) == result.slippage_tolerance
    _no_float_anywhere(ladder)


def test_every_session_s_events_and_pins_come_back_as_they_went() -> None:
    for session in a_busy_run().sessions:
        events = json.loads(json.dumps(run_json.events_to_json(session.outcome.events)))
        days = json.loads(json.dumps(run_json.option_days_to_json(session.pins)))

        assert run_json.events_from_json(events) == session.outcome.events
        assert (
            run_json.pins_from_json(session.pins.underlying_sha256, session.pins.listing_sha256, days) == session.pins
        )
        _no_float_anywhere(events)


@dataclass(frozen=True)
class _RecordWithADate:
    session_open_ms: int
    trading_date: date | None = None


@dataclass(frozen=True)
class _RecordWithTwo:
    peak_session_open_ms: int | None
    trough_session_open_ms: int
    peak_date: date | None = None
    trough_date: date | None = None


@dataclass(frozen=True)
class _RecordWithNone:
    session_open_ms: int


def test_a_record_that_still_carries_a_date_gets_the_new_york_date_of_the_open_and_one_that_does_not_gets_nothing() -> (
    None
):
    # OPEN_MS is 09:30 New York on 2026-10-09, which is 13:30 UTC the same day; a half day later in the year is 14:30 UTC.
    assert record_of(_RecordWithADate, session_open_ms=OPEN_MS).trading_date == date(2026, 10, 9)
    two = record_of(_RecordWithTwo, peak_session_open_ms=None, trough_session_open_ms=OPEN_MS)
    assert (two.peak_date, two.trough_date) == (None, date(2026, 10, 9))
    assert record_of(_RecordWithNone, session_open_ms=OPEN_MS) == _RecordWithNone(OPEN_MS)
