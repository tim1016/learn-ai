from __future__ import annotations

from datetime import date

from app.data_lake.map_files import build_map_file_bytes
from app.data_lake.polygon_ticker_events import TickerEvent


def test_no_change_emits_two_rows():
    body = build_map_file_bytes(
        symbol="SPY",
        events=[],
        history_start=date(2010, 1, 1),
        history_end=date(2026, 5, 21),
        exchange="nyse",
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 2
    assert lines[0] == "20100101,spy,nyse"
    assert lines[1] == "20260521,spy,nyse"


def test_one_ticker_change_emits_two_rows():
    events = [TickerEvent(date="2022-06-09", new_ticker="META")]
    body = build_map_file_bytes(
        symbol="META",
        events=events,
        history_start=date(2012, 5, 18),  # FB IPO
        history_end=date(2026, 5, 21),
        exchange="nasdaq",
    ).decode("ascii")
    lines = body.strip().split("\n")
    # Three rows: FB pre-change end, META post-change start, history_end.
    # But Polygon's events list gives us "ticker_change to META on 2022-06-09";
    # we don't know the prior ticker from a single event. In v1c we emit the
    # final ticker for the whole range plus the change date — vendor parity
    # for prior-ticker history is deferred to Slice 5.
    assert len(lines) == 2
    assert lines[0] == "20120518,meta,nasdaq"
    assert lines[1] == "20260521,meta,nasdaq"


def test_build_is_deterministic():
    a = build_map_file_bytes("SPY", [], date(2010, 1, 1), date(2026, 5, 21), "nyse")
    b = build_map_file_bytes("SPY", [], date(2010, 1, 1), date(2026, 5, 21), "nyse")
    assert a == b
