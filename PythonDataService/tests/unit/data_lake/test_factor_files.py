"""Format-correctness tests for LEAN factor-file builder.

Real vendor-equivalent factor parity is deferred to Slice 5; v1c produces
a file LEAN can load without errors and that captures the basic cumulative
back-adjustment for the splits + dividends we have.
"""

from __future__ import annotations

from datetime import date

from app.data_lake.factor_files import build_factor_file_bytes
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent


def test_no_events_emits_two_anchor_rows():
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=[],
        history_start=date(2020, 1, 1),
        history_end=date(2026, 5, 21),
    ).decode("ascii")
    lines = body.strip().split("\n")
    # Just the two anchor rows: start and end of history, both with factor=1.
    assert len(lines) == 2
    assert lines[0].startswith("20200101,1")
    assert lines[1].startswith("20260521,1")


def test_one_split_event_emits_three_rows():
    splits = [SplitEvent(execution_date="2020-08-31", split_from=1.0, split_to=4.0)]
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=splits,
        dividends=[],
        history_start=date(2020, 1, 1),
        history_end=date(2026, 5, 21),
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 3
    # Pre-split anchor row: split_factor=0.25 (1/4).
    pre = lines[0].split(",")
    assert pre[0] == "20200101"
    assert float(pre[2]) == 0.25
    # Split event row.
    event = lines[1].split(",")
    assert event[0] == "20200831"
    # Post-split end row: split_factor=1.
    post = lines[2].split(",")
    assert post[0] == "20260521"
    assert float(post[2]) == 1.0


def test_one_dividend_event_emits_three_rows():
    dividends = [DividendEvent(ex_dividend_date="2024-03-15", cash_amount=1.71)]
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=dividends,
        history_start=date(2020, 1, 1),
        history_end=date(2026, 5, 21),
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 3
    # The first row's price_factor < 1 (pre-dividend back-adjustment).
    first = lines[0].split(",")
    assert float(first[1]) < 1.0
    # End row has price_factor=1.
    last = lines[2].split(",")
    assert float(last[1]) == 1.0


def test_build_is_deterministic():
    splits = [SplitEvent(execution_date="2020-08-31", split_from=1.0, split_to=4.0)]
    a = build_factor_file_bytes("SPY", splits, [], date(2020, 1, 1), date(2026, 5, 21))
    b = build_factor_file_bytes("SPY", splits, [], date(2020, 1, 1), date(2026, 5, 21))
    assert a == b
