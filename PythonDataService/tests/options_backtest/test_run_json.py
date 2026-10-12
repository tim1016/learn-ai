"""The JSON form of a run's parts: exact, with no float and no date text, and the same thing on the way back (#3030)."""

from __future__ import annotations

import json
from typing import Any

from app.options_backtest import run_json
from tests.options_backtest._a_run import a_busy_run, what_the_busy_run_reaches


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


def test_a_session_date_is_made_again_from_its_open_and_is_not_stored() -> None:
    summary = a_busy_run().summary

    written = run_json.summary_to_json(summary)

    assert "trading_date" not in json.dumps(written) and "peak_date" not in json.dumps(written)
    assert [row.trading_date for row in run_json.summary_from_json(written).by_day] == [
        row.trading_date for row in summary.by_day
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
