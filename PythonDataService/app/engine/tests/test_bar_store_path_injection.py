"""Path-injection guards for the shared bar store (CodeQL py/path-injection).

The policy store and the LEAN-format writers build filesystem paths from a
caller-supplied ``symbol``. These tests pin two layers of defense:

* ``validate_symbol`` rejects any non-ticker character before a path join
  (closes the real gap: ``polygon_export`` does not pre-validate).
* ``ensure_within_root`` rebuilds with ``realpath`` and a root-prefix check,
  the sanitizer CodeQL recognizes, and catches symlink escapes at runtime.
"""

from __future__ import annotations

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


def test_ensure_within_root_returns_contained_path(tmp_path: Path):
    resolved = ensure_within_root(tmp_path, tmp_path / "equity" / "spy.zip")
    assert str(resolved).startswith(str(tmp_path.resolve()))


def test_ensure_within_root_rejects_parent_traversal(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="escapes root"):
        ensure_within_root(root, root / ".." / "escape.zip")


def test_ensure_within_root_rejects_symlink_escape(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes root"):
        ensure_within_root(root, root / "link" / "escape.zip")


@pytest.mark.parametrize("bad_symbol", ["../evil", "a/b", "..", "AB/../CD"])
def test_minute_writer_rejects_path_unsafe_symbol(tmp_path: Path, bad_symbol: str):
    with pytest.raises(SymbolValidationError):
        write_lean_day_zip(tmp_path, bad_symbol, TRADING_DATE, [])


@pytest.mark.parametrize("bad_symbol", ["../evil", "a/b", "..", "AB/../CD"])
def test_quote_writer_rejects_path_unsafe_symbol(tmp_path: Path, bad_symbol: str):
    with pytest.raises(SymbolValidationError):
        write_lean_quote_day_zip(tmp_path, bad_symbol, TRADING_DATE, [])


@pytest.mark.parametrize("bad_symbol", ["../evil", "a/b", "..", "AB/../CD"])
def test_daily_writer_rejects_path_unsafe_symbol(tmp_path: Path, bad_symbol: str):
    with pytest.raises(SymbolValidationError):
        write_lean_daily_zip(tmp_path, bad_symbol, [])


def test_minute_writer_accepts_valid_symbol_and_stays_within_root(tmp_path: Path):
    zip_path = write_lean_day_zip(tmp_path, "SPY", TRADING_DATE, [])

    assert zip_path.exists()
    assert str(zip_path.resolve()).startswith(str(tmp_path.resolve()))
    assert zip_path == tmp_path / "equity" / "usa" / "minute" / "spy" / "20250106_trade.zip"


def test_daily_writer_accepts_valid_symbol_and_stays_within_root(tmp_path: Path):
    zip_path = write_lean_daily_zip(tmp_path, "SPY", [])

    assert zip_path.exists()
    assert str(zip_path.resolve()).startswith(str(tmp_path.resolve()))
    assert zip_path == tmp_path / "equity" / "usa" / "daily" / "spy.zip"
