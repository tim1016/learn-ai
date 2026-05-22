"""Tests for the LEAN factor-file builder.

The builder mirrors LEAN's ``FactorFileGenerator``: corporate-action
rows are dated to the trading session *before* the ex-date and carry
that session's RTH close as the reference price. A zero/missing
reference price is a hard error (it makes LEAN's DividendEventProvider
throw and truncates the backtest).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.data_lake.factor_files import FactorFileReferenceError, build_factor_file_bytes
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent


def test_no_events_emits_two_anchor_rows():
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=[],
        history_start=date(2024, 6, 3),
        history_end=date(2026, 4, 30),
        daily_closes={date(2024, 6, 3): Decimal("530.12"), date(2026, 4, 30): Decimal("711.04")},
    ).decode("ascii")
    lines = body.strip().split("\n")
    # Just the two anchor rows: start and end of history, both with factor=1.
    assert len(lines) == 2
    assert lines[0] == "20240603,1,1,530.12"
    assert lines[1] == "20260430,1,1,711.04"


def test_one_split_event_dates_row_to_prior_session():
    splits = [SplitEvent(execution_date="2024-08-30", split_from=1.0, split_to=4.0)]
    body = build_factor_file_bytes(
        symbol="AAPL",
        splits=splits,
        dividends=[],
        history_start=date(2024, 6, 3),
        history_end=date(2026, 4, 30),
        daily_closes={
            date(2024, 6, 3): Decimal("190.00"),
            date(2024, 8, 29): Decimal("226.00"),
            date(2026, 4, 30): Decimal("250.00"),
        },
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 3
    # Pre-split anchor: split_factor = 1/4.
    pre = lines[0].split(",")
    assert pre[0] == "20240603"
    assert Decimal(pre[2]) == Decimal("0.25")
    # Split event row is dated to the prior trading session, not the ex-date.
    event = lines[1].split(",")
    assert event[0] == "20240829"
    assert Decimal(event[2]) == Decimal("0.25")
    # End anchor: split_factor back to 1.
    post = lines[2].split(",")
    assert post[0] == "20260430"
    assert Decimal(post[2]) == Decimal("1")


def test_one_dividend_event_uses_prior_session_close():
    dividends = [DividendEvent(ex_dividend_date="2025-12-19", cash_amount=1.81)]
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=dividends,
        history_start=date(2024, 6, 3),
        history_end=date(2026, 4, 30),
        daily_closes={
            date(2024, 6, 3): Decimal("530.00"),
            date(2025, 12, 18): Decimal("680.00"),
            date(2026, 4, 30): Decimal("711.00"),
        },
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 3
    event = lines[1].split(",")
    # Row dated to the prior trading session (2025-12-18), not the ex-date.
    assert event[0] == "20251218"
    # Reference price is that session's RTH close.
    assert Decimal(event[3]) == Decimal("680.00")
    # price_factor = 1 - cash * split_factor / reference_price (10 dp).
    expected_pf = (Decimal("1") - Decimal("1.81") / Decimal("680.00")).quantize(Decimal("0.0000000001"))
    assert Decimal(event[1]) == expected_pf
    # End anchor is unadjusted.
    assert lines[2].split(",")[1] == "1"


def test_out_of_window_events_are_dropped():
    """A dividend whose ex-date precedes the capture window is ignored —
    the capture cannot price it and a backtest never sees it."""
    dividends = [
        DividendEvent(ex_dividend_date="2007-03-16", cash_amount=0.7),
        DividendEvent(ex_dividend_date="2025-12-19", cash_amount=1.81),
    ]
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=dividends,
        history_start=date(2024, 6, 3),
        history_end=date(2026, 4, 30),
        daily_closes={
            date(2024, 6, 3): Decimal("530.00"),
            date(2025, 12, 18): Decimal("680.00"),
            date(2026, 4, 30): Decimal("711.00"),
        },
    ).decode("ascii")
    # Two anchors + one in-window dividend; the 2007 event is dropped.
    assert len(body.strip().split("\n")) == 3


def test_in_window_dividend_without_prior_session_fails_loudly():
    dividends = [DividendEvent(ex_dividend_date="2025-12-19", cash_amount=1.81)]
    with pytest.raises(FactorFileReferenceError, match="reference price"):
        build_factor_file_bytes(
            symbol="SPY",
            splits=[],
            dividends=dividends,
            history_start=date(2024, 6, 3),
            history_end=date(2026, 4, 30),
            # Only the ex-date itself is present — no prior session to price it.
            daily_closes={date(2025, 12, 19): Decimal("681.00")},
        )


def test_zero_reference_close_fails_loudly():
    dividends = [DividendEvent(ex_dividend_date="2025-12-19", cash_amount=1.81)]
    with pytest.raises(FactorFileReferenceError, match="non-positive reference price"):
        build_factor_file_bytes(
            symbol="SPY",
            splits=[],
            dividends=dividends,
            history_start=date(2024, 6, 3),
            history_end=date(2026, 4, 30),
            daily_closes={
                date(2025, 12, 18): Decimal("0"),
                date(2026, 4, 30): Decimal("711.00"),
            },
        )


def test_build_is_deterministic():
    dividends = [DividendEvent(ex_dividend_date="2025-12-19", cash_amount=1.81)]
    closes = {
        date(2024, 6, 3): Decimal("530.00"),
        date(2025, 12, 18): Decimal("680.00"),
        date(2026, 4, 30): Decimal("711.00"),
    }
    a = build_factor_file_bytes("SPY", [], dividends, date(2024, 6, 3), date(2026, 4, 30), closes)
    b = build_factor_file_bytes("SPY", [], dividends, date(2024, 6, 3), date(2026, 4, 30), closes)
    assert a == b
