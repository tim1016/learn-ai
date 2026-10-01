"""Tests for the adapter's ingestion-boundary helpers (temporal + numeric)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.adapter import (
    et_date_to_ms,
    occurred_at_ms,
    opt_bool,
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
