"""Where option bars and option listings live on the lake (#3030).

The paths are written in one place, ``path_policy``, and nowhere else.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path, PurePosixPath

import pytest

from app.data_lake.path_policy import OptionListingPath, OptionMinuteBarPath

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_a_contracts_minute_bars_live_under_its_underlying_and_its_own_symbol() -> None:
    path = OptionMinuteBarPath(
        underlying="SPY", contract_symbol="SPY250613C00600000", trading_date=date(2025, 6, 13)
    ).relative_path()

    assert path == PurePosixPath("option/usa/minute/spy/spy250613c00600000/20250613_trade.zip")


def test_a_listing_lives_under_its_underlying_and_its_date() -> None:
    path = OptionListingPath(underlying="SPY", as_of=date(2025, 6, 13)).relative_path()

    assert path == PurePosixPath("option/usa/listing/spy/20250613_contracts.zip")


def test_a_dotted_underlying_keeps_its_dot() -> None:
    path = OptionListingPath(underlying="BRK.B", as_of=date(2025, 6, 13)).relative_path()

    assert path == PurePosixPath("option/usa/listing/brk.b/20250613_contracts.zip")


@pytest.mark.parametrize("hostile", ["", "..", "../etc", "spy/qqq", "SPY\\x", ".hidden", " SPY"])
def test_a_symbol_that_could_leave_its_folder_is_refused(hostile: str) -> None:
    with pytest.raises(ValueError, match="path segment"):
        OptionMinuteBarPath(underlying="SPY", contract_symbol=hostile, trading_date=date(2025, 6, 13))
    with pytest.raises(ValueError, match="path segment"):
        OptionListingPath(underlying=hostile, as_of=date(2025, 6, 13))


def test_option_paths_are_written_only_by_the_path_policy() -> None:
    """A lint written as a test: the option layout is spelled in one module."""
    offenders = [
        str(file.relative_to(PROJECT_ROOT))
        for file in (PROJECT_ROOT / "app").rglob("*.py")
        if file.relative_to(PROJECT_ROOT).as_posix() != "app/data_lake/path_policy.py"
        and any(
            needle in line and not line.lstrip().startswith("#")
            for line in file.read_text(encoding="utf-8").splitlines()
            for needle in ("option/usa/", '"option") / "usa"')
        )
    ]

    assert offenders == []
