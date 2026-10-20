from __future__ import annotations

from datetime import date

import pytest

from app.data_lake.map_files import build_map_file_bytes, map_file_coverage
from app.data_lake.polygon_ticker_events import TickerEvent


def test_no_change_emits_two_rows_and_builds_deterministically():
    built = build_map_file_bytes(
        symbol="SPY",
        events=[],
        history_start=date(2010, 1, 1),
        history_end=date(2026, 5, 21),
        exchange="nyse",
    )
    body = built.decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 2, "no ticker change emits exactly two rows"
    assert lines[0] == "20100101,spy,nyse", "first row is the history start"
    assert lines[1] == "20260521,spy,nyse", "last row is the history end"
    assert build_map_file_bytes("SPY", [], date(2010, 1, 1), date(2026, 5, 21), "nyse") == built, (
        "build is deterministic"
    )


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


# ── map_file_coverage — the one parser of a map's stated span (#2453) ──


def test_coverage_reads_the_stated_span():
    payload = b"20100101,spy,nyse\n20240520,spy,nyse\n20260521,spy,nyse\n"
    assert map_file_coverage(payload) == (date(2010, 1, 1), date(2026, 5, 21)), "reads first and last row dates"
    built = build_map_file_bytes("SPY", [], date(2010, 1, 1), date(2026, 5, 21), "nyse")
    assert map_file_coverage(built) == (date(2010, 1, 1), date(2026, 5, 21)), "round-trips the builder"


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(b"20260521,spy,nyse\n20100101,spy,nyse\n", "non-decreasing", id="rejects_disordered_rows"),
        pytest.param(b"20100101,spy,nyse\n", "at least two rows", id="rejects_a_single_row"),
    ],
)
def test_coverage_rejects_malformed_payloads(payload, match):
    with pytest.raises(ValueError, match=match):
        map_file_coverage(payload)
