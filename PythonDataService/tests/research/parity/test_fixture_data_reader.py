"""Unit tests for ``FixtureDataReader``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.research.parity.fixture_data_reader import (
    FixtureDataReader,
)
from app.utils.timestamps import datetime_at_ms

CSV_CONTENT = (
    "time,open,high,low,close,volume\n"
    "2026-02-10,189.50,190.10,188.20,189.80,52341000\n"
    "2026-02-11,190.00,191.20,189.30,190.55,48127000\n"
    "2026-02-12,190.60,192.00,190.10,191.75,55890000\n"
)


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    p = tmp_path / "qc_price_history.csv"
    p.write_text(CSV_CONTENT)
    return p


def test_iter_bars_yields_ordered_anchored_rows_and_filters_by_date_and_symbol(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path)
    bars = list(reader.iter_bars("AAPL"))

    assert len(bars) == 3, "yields each row in order: three rows"
    assert bars[0].symbol == "AAPL", "yields each row in order: first symbol"
    assert bars[0].open == Decimal("189.50"), "yields each row in order: first open"
    assert bars[0].close == Decimal("189.80"), "yields each row in order: first close"
    assert bars[0].volume == 52_341_000, "yields each row in order: first volume"
    assert bars[1].open == Decimal("190.00"), "yields each row in order: second open"
    assert bars[2].close == Decimal("191.75"), "yields each row in order: third close"

    [first, *_] = bars
    eastern = ZoneInfo("America/New_York")
    start = datetime_at_ms(first.start_ms, tz=eastern)
    end = datetime_at_ms(first.end_ms, tz=eastern)
    assert start.tzinfo is not None, "anchors time to the NY session open: start is tz-aware"
    assert start.hour == 9, "anchors time to the NY session open: start hour"
    assert start.minute == 30, "anchors time to the NY session open: start minute"
    assert end.hour == 16, "anchors time to the NY session open: end hour"
    assert end.minute == 0, "anchors time to the NY session open: end minute"
    assert start.date() == date(2026, 2, 10), "anchors time to the NY session open: start date"

    in_range = list(
        reader.iter_bars(
            "AAPL",
            start=date(2026, 2, 11),
            end=date(2026, 2, 11),
        )
    )
    assert len(in_range) == 1, "filters by date range: one bar in range"
    assert in_range[0].open == Decimal("190.00"), "filters by date range: the bar in range"

    symbol_reader = FixtureDataReader(csv_path, symbol="AAPL")
    assert list(symbol_reader.iter_bars("MSFT")) == [], "an unknown symbol returns empty"
    assert len(list(symbol_reader.iter_bars("aapl"))) == 3, "the symbol match is case-insensitive"


def test_bar_open_by_date_returns_decimals(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path)
    by_date = reader.bar_open_by_date("AAPL")
    assert by_date[date(2026, 2, 10)] == Decimal("189.50")
    assert by_date[date(2026, 2, 12)] == Decimal("190.60")
