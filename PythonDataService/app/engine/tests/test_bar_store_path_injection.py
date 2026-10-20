"""Path-injection guards for the shared bar store (CodeQL py/path-injection).

The policy store and the LEAN-format writers build filesystem paths from a
caller-supplied ``symbol``. These tests pin two layers of defense:

* ``validate_symbol`` rejects any non-ticker character before a path join
  (closes the real gap: ``polygon_bars`` does not pre-validate).
* ``ensure_within_root`` rebuilds with ``realpath`` and a root-prefix check,
  the sanitizer CodeQL recognizes, and catches symlink escapes at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from app.engine.data.lean_format import (
    write_lean_daily_zip,
    write_lean_day_zip,
    write_lean_quote_day_zip,
)
from app.engine.data.path_safety import ensure_within_root
from app.lean_sidecar.workspace import SymbolValidationError

TRADING_DATE = date(2025, 1, 6)
PATH_UNSAFE_SYMBOLS = ("../evil", "a/b", r"a\b", "..", "AB/../CD", "\0")


def test_ensure_within_root_returns_contained_path(tmp_path: Path):
    resolved = ensure_within_root(tmp_path, tmp_path / "equity" / "spy.zip")
    assert str(resolved).startswith(str(tmp_path.resolve()))


@pytest.mark.parametrize(
    "escape_parts",
    [
        pytest.param(("..", "escape.zip"), id="parent_traversal"),
        pytest.param(("link", "escape.zip"), id="symlink_escape"),
    ],
)
def test_ensure_within_root_rejects_escape(tmp_path: Path, escape_parts: tuple[str, ...]):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes root"):
        ensure_within_root(root, root.joinpath(*escape_parts))


@pytest.mark.parametrize(
    "bad_symbol",
    PATH_UNSAFE_SYMBOLS,
    ids=["parent_traversal", "slash", "backslash", "dotdot", "embedded_traversal", "nul"],
)
@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda root, symbol: write_lean_day_zip(root, symbol, TRADING_DATE, []), id="minute_writer"),
        pytest.param(lambda root, symbol: write_lean_quote_day_zip(root, symbol, TRADING_DATE, []), id="quote_writer"),
        pytest.param(lambda root, symbol: write_lean_daily_zip(root, symbol, []), id="daily_writer"),
    ],
)
def test_writers_reject_path_unsafe_symbol(tmp_path: Path, write: Callable[[Path, str], object], bad_symbol: str):
    with pytest.raises(SymbolValidationError):
        write(tmp_path, bad_symbol)


@pytest.mark.parametrize(
    ("write", "expected_parts"),
    [
        pytest.param(
            lambda root: write_lean_day_zip(root, "SPY", TRADING_DATE, []),
            ("equity", "usa", "minute", "spy", "20250106_trade.zip"),
            id="minute_writer",
        ),
        pytest.param(
            lambda root: write_lean_daily_zip(root, "SPY", []),
            ("equity", "usa", "daily", "spy.zip"),
            id="daily_writer",
        ),
    ],
)
def test_writer_accepts_valid_symbol_and_stays_within_root(
    tmp_path: Path, write: Callable[[Path], Path], expected_parts: tuple[str, ...]
):
    zip_path = write(tmp_path)

    assert zip_path.exists(), "the writer creates the zip"
    assert str(zip_path.resolve()).startswith(str(tmp_path.resolve())), "the zip stays within the root"
    assert zip_path == tmp_path.joinpath(*expected_parts), "the zip lands at the LEAN layout path"
