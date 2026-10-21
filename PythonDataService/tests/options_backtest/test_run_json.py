"""The JSON form of a run's parts: exact, with no float and no date text, and the same thing on the way back (#3030)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import pytest

from app.data_lake.option_bars import SessionMemberRef
from app.options_backtest import run_json
from app.options_backtest.results import EntryRefusal, ExitReason
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


def test_the_summary_comes_back_as_it_went_holds_no_float_and_identifies_a_session_by_its_open_with_no_date() -> None:
    summary = a_busy_run().summary

    text = json.dumps(run_json.summary_to_json(summary))
    written = json.loads(text)

    assert run_json.summary_from_json(written) == summary, "the summary comes back as it went"
    _no_float_anywhere(written)
    assert "trading_date" not in text and "peak_date" not in text and "trough_date" not in text, (
        "a session is identified by its open: no date key is written"
    )
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text), "no date text is in the stored summary"
    assert [row.session_open_ms for row in run_json.summary_from_json(json.loads(text)).by_day] == [
        row.session_open_ms for row in summary.by_day
    ], "the sessions read back are identified by their opens"


def test_a_summary_stored_before_a_sold_leg_could_be_closed_with_its_protection_reads_with_the_five_reasons_it_had() -> (
    None
):
    # A run saved before #3107 lists five exit reasons. It is read as it was written: the sixth is not invented for it.
    written = json.loads(json.dumps(run_json.summary_to_json(a_busy_run().summary)))
    written["exits_by_reason"] = [
        ["stop", 1],
        ["square_off", 0],
        ["exit_time", 2],
        ["marked_not_sold", 1],
        ["marked_not_bought_back", 0],
    ]

    read = run_json.summary_from_json(written)

    assert read.exits_by_reason == (
        (ExitReason.STOP, 1),
        (ExitReason.SQUARE_OFF, 0),
        (ExitReason.EXIT_TIME, 2),
        (ExitReason.MARKED_NOT_SOLD, 1),
        (ExitReason.MARKED_NOT_BOUGHT_BACK, 0),
    )


def test_the_refusals_counted_by_reason_ride_in_the_stored_summary_and_come_back_equal() -> None:
    summary = replace(
        a_busy_run().summary,
        entries_refused_by_reason=(
            (EntryRefusal.OTHER_SIDE, 4),
            (EntryRefusal.SECOND_EXPIRY, 3),
            (EntryRefusal.COVER_DID_NOT_TRADE, 1),
            (EntryRefusal.COVER_NOT_YET_HELD, 2),
            (EntryRefusal.SALE_AT_ZERO, 0),
        ),
    )

    written = json.loads(json.dumps(run_json.summary_to_json(summary)))

    assert written["entries_refused_by_reason"] == [
        ["other_side", 4],
        ["second_expiry", 3],
        ["cover_did_not_trade", 1],
        ["cover_not_yet_held", 2],
        ["sale_at_zero", 0],
    ]
    assert run_json.summary_from_json(written) == summary


def test_a_summary_stored_before_refusals_were_counted_by_reason_reads_them_as_not_recorded_and_writes_none() -> None:
    summary = a_busy_run().summary
    assert summary.entries_refused_by_reason is not None
    old = json.loads(json.dumps(run_json.summary_to_json(summary)))
    del old["entries_refused_by_reason"]

    read = run_json.summary_from_json(old)

    assert read.entries_refused_by_reason is None
    assert read == replace(summary, entries_refused_by_reason=None)
    # Its JSON is the JSON it always was, and not a key of null.
    assert run_json.summary_to_json(read) == old


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


_CONTAINER_PIN_JSON = (
    '[{"contract": "SPY250613C00500000", "sha256": "' + "b" * 64 + '", "reference": {"version": "1", '
    '"layout": "option_session", "container_sha256": "' + "a" * 64 + '", "member_sha256": "' + "b" * 64 + '", '
    '"listing_sha256": "' + "c" * 64 + '", "recipe": "' + "d" * 64 + '", '
    '"data_root_id": "00000000-0000-4000-8000-000000000001", "state": "captured"}}]'
)


def test_a_container_pin_is_stored_as_the_exact_json_saved_runs_hold_and_reads_back_typed() -> None:
    [day] = json.loads(_CONTAINER_PIN_JSON)

    pins = run_json.pins_from_json(None, "c" * 64, [day])

    assert pins.option_days[0].reference == SessionMemberRef(
        "a" * 64, "b" * 64, "c" * 64, "d" * 64, "00000000-0000-4000-8000-000000000001", "captured"
    )
    assert json.dumps(run_json.option_days_to_json(pins)) == _CONTAINER_PIN_JSON, "stored bytes are unchanged"


@pytest.mark.parametrize(
    "damage",
    [
        lambda ref: {key: value for key, value in ref.items() if key != "recipe"},
        lambda ref: {**ref, "extra": "x"},
        lambda ref: {**ref, "version": "2"},
        lambda ref: {**ref, "layout": "day_file"},
        lambda ref: {**ref, "container_sha256": "A" * 64},
        lambda ref: {**ref, "state": "unresolved"},
        lambda ref: {**ref, "data_root_id": ""},
        lambda ref: "not an object",
    ],
    ids=["missing", "extra", "version", "layout", "sha", "state", "root", "shape"],
)
def test_a_malformed_stored_container_pin_is_refused_on_load_not_later(damage: Any) -> None:
    [day] = json.loads(_CONTAINER_PIN_JSON)

    with pytest.raises(ValueError, match="session member reference"):
        run_json.pins_from_json(None, "c" * 64, [{**day, "reference": damage(day["reference"])}])
