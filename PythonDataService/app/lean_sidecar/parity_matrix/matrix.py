"""Cross-engine parity matrix — 4 tickers × 3 nested windows = 12 cells.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

TICKERS: Final[tuple[str, ...]] = ("SPY", "QQQ", "AAPL", "TSLA")
END_DATE: Final[date] = date(2026, 4, 30)


class WindowLabel(StrEnum):
    W6MO = "W6mo"
    W12MO = "W12mo"
    W24MO = "W24mo"


_WINDOW_STARTS: Final[dict[WindowLabel, date]] = {
    WindowLabel.W6MO: date(2025, 11, 3),
    WindowLabel.W12MO: date(2025, 5, 1),
    WindowLabel.W24MO: date(2024, 6, 3),
}


@dataclass(frozen=True)
class Cell:
    ticker: str
    window_label: WindowLabel
    start_date: date
    end_date: date

    @property
    def cell_id(self) -> str:
        return f"{self.ticker}_{self.window_label.value}_{self.start_date.isoformat()}_to_{self.end_date.isoformat()}"


def _build_cells() -> tuple[Cell, ...]:
    out: list[Cell] = []
    for ticker in TICKERS:
        for label, start in _WINDOW_STARTS.items():
            out.append(Cell(ticker=ticker, window_label=label, start_date=start, end_date=END_DATE))
    return tuple(out)


CELLS: Final[tuple[Cell, ...]] = _build_cells()
_CELL_INDEX: Final[dict[str, Cell]] = {c.cell_id: c for c in CELLS}


def cell_by_id(cell_id: str) -> Cell:
    """Return cell by canonical id; raise KeyError if unknown."""
    return _CELL_INDEX[cell_id]
