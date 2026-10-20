"""Matrix invariants — 16 cells, 4 tickers × 4 nested windows."""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.parity_matrix.matrix import (
    CELLS,
    Cell,
    WindowLabel,
    cell_by_id,
)


def test_cells_are_sixteen_over_four_distinct_tickers_with_four_windows_each() -> None:
    assert len(CELLS) == 16, "total cell count is 16"
    assert {c.ticker for c in CELLS} == {"SPY", "QQQ", "AAPL", "TSLA"}, "the cells span four distinct tickers"
    for ticker in ("SPY", "QQQ", "AAPL", "TSLA"):
        labels = {c.window_label for c in CELLS if c.ticker == ticker}
        assert labels == {
            WindowLabel.W3MO,
            WindowLabel.W6MO,
            WindowLabel.W12MO,
            WindowLabel.W24MO,
        }, f"{ticker} has four windows"


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
