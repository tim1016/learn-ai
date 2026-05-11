"""Unit tests for ``FixtureDataReader``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.research.parity.fixture_data_reader import (
    FixtureDataReader,
    fixture_data_source_factory,
)

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


def test_iter_bars_yields_each_row_in_order(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path)
    bars = list(reader.iter_bars("AAPL"))

    assert len(bars) == 3
    assert bars[0].symbol == "AAPL"
    assert bars[0].open == Decimal("189.50")
    assert bars[0].close == Decimal("189.80")
    assert bars[0].volume == 52_341_000
    assert bars[1].open == Decimal("190.00")
    assert bars[2].close == Decimal("191.75")


def test_iter_bars_anchors_time_to_ny_session_open(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path)
    [first, *_] = list(reader.iter_bars("AAPL"))

    assert first.time.tzinfo is not None
    assert first.time.hour == 9
    assert first.time.minute == 30
    assert first.end_time.hour == 16
    assert first.end_time.minute == 0
    assert first.time.date() == date(2026, 2, 10)


def test_iter_bars_filters_by_date_range(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path)
    bars = list(
        reader.iter_bars(
            "AAPL",
            start=date(2026, 2, 11),
            end=date(2026, 2, 11),
        )
    )
    assert len(bars) == 1
    assert bars[0].open == Decimal("190.00")


def test_iter_bars_unknown_symbol_returns_empty(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path, symbol="AAPL")
    bars = list(reader.iter_bars("MSFT"))
    assert bars == []


def test_iter_bars_is_case_insensitive(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path, symbol="AAPL")
    bars = list(reader.iter_bars("aapl"))
    assert len(bars) == 3


def test_bar_open_by_date_returns_decimals(csv_path: Path) -> None:
    reader = FixtureDataReader(csv_path)
    by_date = reader.bar_open_by_date("AAPL")
    assert by_date[date(2026, 2, 10)] == Decimal("189.50")
    assert by_date[date(2026, 2, 12)] == Decimal("190.60")


def test_factory_returns_callable_matching_runner_signature(csv_path: Path) -> None:
    factory = fixture_data_source_factory(csv_path)
    reader = factory("AAPL", date(2026, 2, 10), date(2026, 2, 12))
    bars = list(reader.iter_bars("AAPL"))
    assert len(bars) == 3
