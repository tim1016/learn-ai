"""Matrix invariants — 12 cells, 4 tickers × 3 nested windows."""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.parity_matrix.matrix import (
    CELLS,
    Cell,
    WindowLabel,
    cell_by_id,
)


def test_cells_total_count() -> None:
    assert len(CELLS) == 12


def test_cells_have_four_distinct_tickers() -> None:
    assert {c.ticker for c in CELLS} == {"SPY", "QQQ", "AAPL", "TSLA"}


def test_each_ticker_has_three_windows() -> None:
    for ticker in ("SPY", "QQQ", "AAPL", "TSLA"):
        labels = {c.window_label for c in CELLS if c.ticker == ticker}
        assert labels == {WindowLabel.W6MO, WindowLabel.W12MO, WindowLabel.W24MO}


def test_all_cells_share_end_date() -> None:
    assert {c.end_date for c in CELLS} == {date(2026, 4, 30)}


def test_window_start_dates_match_spec() -> None:
    starts = {(c.window_label, c.start_date) for c in CELLS}
    assert (WindowLabel.W6MO, date(2025, 11, 3)) in starts
    assert (WindowLabel.W12MO, date(2025, 5, 1)) in starts
    assert (WindowLabel.W24MO, date(2024, 6, 3)) in starts


def test_cell_id_format() -> None:
    spy_w24 = cell_by_id("SPY_W24mo_2024-06-03_to_2026-04-30")
    assert isinstance(spy_w24, Cell)
    assert spy_w24.ticker == "SPY"
    assert spy_w24.window_label == WindowLabel.W24MO
    assert spy_w24.start_date == date(2024, 6, 3)
    assert spy_w24.end_date == date(2026, 4, 30)


def test_cell_by_id_unknown_raises() -> None:
    with pytest.raises(KeyError):
        cell_by_id("UNKNOWN_W6mo_2025-11-03_to_2026-04-30")
