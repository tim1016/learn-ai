"""Regen script — argument parsing + cell-selection logic.

LEAN container invocation and Engine Lab orchestration are stubs in
Task 7 (filled in Task 10). These tests only exercise the args + matrix
selection shape.
"""

from __future__ import annotations

import argparse

import pytest

from app.lean_sidecar.config import (
    HISTORICAL_LEAN_IMAGE_DIGEST_ARM64,
    PINNED_LEAN_IMAGE_DIGEST,
)
from scripts.regenerate_cross_engine_study import (
    _parse_args,
    _resolve_target_cells,
)


def test_parse_args_all_takes_the_pinned_digest_and_accepts_the_retained_historical_fixture_digest() -> None:
    ns = _parse_args(["--all"])
    assert ns.all is True, "--all sets all"
    assert ns.cell is None, "--all leaves cell unset"
    assert ns.ticker is None, "--all leaves ticker unset"
    assert ns.image_digest == PINNED_LEAN_IMAGE_DIGEST, "--all alone takes the pinned image digest"

    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 is not None, "the retained historical fixture digest exists"

    historical = _parse_args(["--all", "--image-digest", HISTORICAL_LEAN_IMAGE_DIGEST_ARM64])

    assert historical.image_digest == HISTORICAL_LEAN_IMAGE_DIGEST_ARM64, (
        "the retained historical fixture digest is accepted"
    )


def test_parse_args_rejects_unapproved_fixture_digest() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--all", "--image-digest", "sha256:" + "f" * 64])


def test_resolve_target_cells_all_ticker_single_and_unknown_ticker() -> None:
    every = _resolve_target_cells(argparse.Namespace(all=True, cell=None, ticker=None))
    assert len(every) == 16, "all selects every cell"

    by_ticker = _resolve_target_cells(argparse.Namespace(all=False, cell=None, ticker="SPY"))
    assert len(by_ticker) == 4, "a ticker selects its four cells"
    assert all(c.ticker == "SPY" for c in by_ticker), "a ticker selects only its own cells"

    single = _resolve_target_cells(
        argparse.Namespace(
            all=False,
            cell="SPY_W6mo_2025-11-03_to_2026-04-30",
            ticker=None,
        )
    )
    assert len(single) == 1, "a cell id selects one cell"
    assert single[0].cell_id == "SPY_W6mo_2025-11-03_to_2026-04-30", "a cell id selects that cell"

    unknown = _resolve_target_cells(argparse.Namespace(all=False, cell=None, ticker="BOGUS"))
    # Unknown ticker returns an empty list — main() decides whether
    # that's an error. Keep the resolver pure.
    assert unknown == [], "an unknown ticker selects nothing"


def test_resolve_target_cells_unknown_cell_id_raises() -> None:
    with pytest.raises(KeyError):
        _resolve_target_cells(
            argparse.Namespace(
                all=False,
                cell="UNKNOWN_W6mo_2025-11-03_to_2026-04-30",
                ticker=None,
            )
        )
