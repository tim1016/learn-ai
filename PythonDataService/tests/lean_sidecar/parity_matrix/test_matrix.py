"""Parity-matrix cell lookup by id."""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.parity_matrix.matrix import (
    Cell,
    WindowLabel,
    cell_by_id,
)


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
