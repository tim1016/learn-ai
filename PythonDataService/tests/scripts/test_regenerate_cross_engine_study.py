"""Regen script — argument parsing + cell-selection logic.

LEAN container invocation and Engine Lab orchestration are stubs in
Task 7 (filled in Task 10). These tests only exercise the args + matrix
selection shape.
"""

from __future__ import annotations

import argparse

import pytest
from scripts.regenerate_cross_engine_study import (
    _parse_args,
    _resolve_target_cells,
)


def test_parse_args_all() -> None:
    ns = _parse_args(["--all"])
    assert ns.all is True
    assert ns.cell is None
    assert ns.ticker is None


def test_parse_args_one_cell() -> None:
    ns = _parse_args(["--cell", "SPY_W6mo_2025-11-03_to_2026-04-30"])
    assert ns.cell == "SPY_W6mo_2025-11-03_to_2026-04-30"
    assert ns.all is False
    assert ns.ticker is None


def test_parse_args_one_ticker() -> None:
    ns = _parse_args(["--ticker", "SPY"])
    assert ns.ticker == "SPY"
    assert ns.all is False
    assert ns.cell is None


def test_parse_args_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--all", "--cell", "SPY_W6mo_2025-11-03_to_2026-04-30"])


def test_parse_args_requires_one() -> None:
    with pytest.raises(SystemExit):
        _parse_args([])


def test_resolve_target_cells_all() -> None:
    cells = _resolve_target_cells(argparse.Namespace(all=True, cell=None, ticker=None))
    assert len(cells) == 12


def test_resolve_target_cells_ticker() -> None:
    cells = _resolve_target_cells(argparse.Namespace(all=False, cell=None, ticker="SPY"))
    assert len(cells) == 3
    assert all(c.ticker == "SPY" for c in cells)


def test_resolve_target_cells_single() -> None:
    cells = _resolve_target_cells(
        argparse.Namespace(
            all=False,
            cell="SPY_W6mo_2025-11-03_to_2026-04-30",
            ticker=None,
        )
    )
    assert len(cells) == 1
    assert cells[0].cell_id == "SPY_W6mo_2025-11-03_to_2026-04-30"


def test_resolve_target_cells_unknown_cell_id_raises() -> None:
    with pytest.raises(KeyError):
        _resolve_target_cells(
            argparse.Namespace(
                all=False,
                cell="UNKNOWN_W6mo_2025-11-03_to_2026-04-30",
                ticker=None,
            )
        )


def test_resolve_target_cells_unknown_ticker_returns_empty() -> None:
    cells = _resolve_target_cells(argparse.Namespace(all=False, cell=None, ticker="BOGUS"))
    # Unknown ticker returns an empty list — main() decides whether
    # that's an error. Keep the resolver pure.
    assert cells == []
