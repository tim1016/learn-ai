"""Tests for the adapter's ingestion-boundary helpers (temporal + numeric)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.adapter import (
    _NON_FINITE_RENOTE_S,
    et_date_to_ms,
    occurred_at_ms,
    opt_bool,
    opt_finite_or_none,
    opt_float,
    opt_rfc3339_to_ms,
    rfc3339_to_ms,
    str_or_blank,
    to_bool,
    to_float,
    to_str,
)


def test_rfc3339_epoch_and_z_suffix() -> None:
    assert rfc3339_to_ms("1970-01-01T00:00:00+00:00") == 0
    assert rfc3339_to_ms("1970-01-01T00:00:01Z") == 1000


def test_rfc3339_honors_offset() -> None:
    # Midnight at -05:00 is 05:00 UTC → 5h = 18_000_000 ms after epoch.
    assert rfc3339_to_ms("1970-01-01T00:00:00-05:00") == 18_000_000


def test_rfc3339_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="not timezone-aware"):
        rfc3339_to_ms("2021-03-16T18:38:01")


def test_rfc3339_trims_overlong_fraction() -> None:
    assert rfc3339_to_ms("1970-01-01T00:00:00.123456789Z") == 123


def test_rfc3339_rounds_fractional_milliseconds_to_nearest_ms() -> None:
    assert rfc3339_to_ms("1970-01-01T00:00:00.123600Z") == 124


@pytest.mark.parametrize("parser", [opt_float, opt_rfc3339_to_ms])
def test_optional_helpers_read_a_vendor_blank_as_absent(parser: Callable[[Any], object]) -> None:
    assert parser("") is None


@pytest.mark.parametrize("parser", [to_float, opt_float])
@pytest.mark.parametrize("value", [True, False])
def test_numeric_helpers_reject_booleans(parser: Callable[[Any], float | None], value: bool) -> None:
    # ``float(True) == 1.0``: a JSON boolean in a money field would otherwise
    # become $1 or $0 of broker evidence.
    with pytest.raises(TypeError, match="not a boolean"):
        parser(value)


@pytest.mark.parametrize("parser", [to_float, opt_float])
def test_numeric_helpers_refuse_a_number_too_large_for_a_float_as_unreadable(
    parser: Callable[[Any], float | None],
) -> None:
    # ``float(10**400)`` raises ``OverflowError``, which no mapper's caller
    # is written to catch: it escaped as a raw error (#2627, #2648 review).
    with pytest.raises(ValueError, match="too large"):
        parser(10**400)


@pytest.mark.parametrize("parser", [to_float, opt_float])
@pytest.mark.parametrize(
    "value", ["NaN", "nan", "Infinity", "-inf", "1e999", float("nan"), float("inf"), float("-inf")], ids=repr
)
def test_numeric_helpers_refuse_a_number_that_is_not_finite(
    parser: Callable[[Any], float | None], value: object
) -> None:
    # ``float("NaN")`` parses, and every comparison made of a NaN is False (#3216).
    with pytest.raises(ValueError, match="must be finite"):
        parser(value)


@pytest.mark.parametrize("parser", [to_float, opt_float])
@pytest.mark.parametrize(("value", "expected"), [("0", 0.0), (-3, -3.0), ("1.25", 1.25), (1e300, 1e300)], ids=repr)
def test_numeric_helpers_keep_every_finite_number_including_zero_and_negatives(
    parser: Callable[[Any], float | None], value: object, expected: float
) -> None:
    assert parser(value) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-inf", "1e999", float("nan"), float("inf")], ids=repr)
def test_an_optional_informational_figure_that_is_not_finite_reads_absent_with_one_warning(
    value: object, caplog: pytest.LogCaptureFixture
) -> None:
    """The lenient helper refuses the value without refusing the read it came in (#3216)."""
    with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.adapter"):
        assert opt_finite_or_none(value, field="sma") is None

    (record,) = caplog.records
    assert (record.action, record.field, record.levelno) == (
        "alpaca_optional_figure_not_finite",
        "sma",
        logging.WARNING,
    )


def test_a_persistently_non_finite_figure_is_logged_once_per_field_per_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The account is read every few seconds: one bad figure must not write four lines a minute (#3216)."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.adapter.monotonic", lambda: now[0])

    with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.adapter"):
        for _ in range(5):
            assert opt_finite_or_none("NaN", field="sma") is None
        assert [record.field for record in caplog.records] == ["sma"]

        # Another field is its own fault; the figure's reads in between do not re-arm the first.
        assert opt_finite_or_none("inf", field="last_equity") is None
        assert opt_finite_or_none(1.0, field="sma") == 1.0
        assert opt_finite_or_none("NaN", field="sma") is None
        assert [record.field for record in caplog.records] == ["sma", "last_equity"]

        # Still bad an hour on: said again, once.
        now[0] += _NON_FINITE_RENOTE_S
        assert opt_finite_or_none("NaN", field="sma") is None
        assert opt_finite_or_none("NaN", field="sma") is None
    assert [record.field for record in caplog.records] == ["sma", "last_equity", "sma"]


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("0", 0.0), (-3, -3.0), ("1.25", 1.25)], ids=repr)
def test_an_optional_informational_figure_that_is_finite_or_absent_is_read_as_it_is(
    value: object, expected: float | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.adapter"):
        assert opt_finite_or_none(value, field="sma") == expected

    assert caplog.records == []


@pytest.mark.parametrize(
    ("value", "error"), [(True, TypeError), ("not-a-number", ValueError), (10**400, ValueError)], ids=repr
)
def test_the_lenient_helper_still_refuses_what_is_not_a_number_at_all(value: object, error: type[Exception]) -> None:
    # Only a finite-ness failure reads absent: a boolean or text in a money field is a corrupt answer.
    with pytest.raises(error):
        opt_finite_or_none(value, field="sma")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="blank"),
        pytest.param("   ", id="whitespace"),
        pytest.param(12345, id="number"),
        pytest.param(True, id="boolean"),
    ],
)
def test_to_str_refuses_a_value_that_is_not_text(value: object) -> None:
    # ``str(None) == "None"``: a null identity field would otherwise become a
    # real-looking order id, symbol or account number (#2643).
    with pytest.raises(ValueError, match="'symbol' must be a non-blank string"):
        to_str(value, field="symbol")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("SPY", "SPY", id="text"),
        pytest.param(None, "", id="null"),
        pytest.param("", "", id="blank"),
        pytest.param("   ", "", id="whitespace"),
        pytest.param(12345, "", id="number"),
        pytest.param(True, "", id="boolean"),
    ],
)
def test_str_or_blank_reads_anything_but_text_as_blank_never_none(value: object, expected: str) -> None:
    # Per-order text the Clerk contains when it is missing: ``str(None)``
    # once made a real-looking ticker "NONE" (#2643).
    assert str_or_blank(value) == expected


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_to_bool_names_the_field_it_refuses(value: object) -> None:
    with pytest.raises(TypeError, match="'is_open' must be a boolean"):
        to_bool(value, field="is_open")


def test_opt_bool_names_the_field_it_refuses_and_keeps_null_unknown() -> None:
    assert opt_bool(None, field="shortable") is None
    assert opt_bool(False, field="shortable") is False
    with pytest.raises(TypeError, match="'shortable' must be a boolean or null"):
        opt_bool("false", field="shortable")


def test_et_date_anchors_at_ny_midnight() -> None:
    expected = int(
        datetime(2021, 1, 4, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
    )
    assert et_date_to_ms("2021-01-04") == expected


def test_occurred_at_prefers_transaction_time_then_date() -> None:
    assert occurred_at_ms({"transaction_time": "1970-01-01T00:00:01Z"}) == 1000
    assert occurred_at_ms({"date": "2021-01-04"}) == et_date_to_ms("2021-01-04")
    assert occurred_at_ms({}) is None
