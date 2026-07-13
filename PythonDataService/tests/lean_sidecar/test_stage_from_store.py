"""Tests for stage_minute_zips_from_store — byte parity with the bar store."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.lean_sidecar.staging import stage_minute_zips_from_store
from app.lean_sidecar.workspace import resolve_workspace
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)
DAY_TWO = date(2026, 1, 6)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_staged_zips_are_byte_identical_to_store(tmp_path: Path):
    store_root = tmp_path / "polygon-raw"
    store_zips = [seed_store_day(store_root, "SPY", d) for d in (DAY_ONE, DAY_TWO)]
    workspace = resolve_workspace("stage-from-store-test", tmp_path / "artifacts")
    workspace.ensure_layout()

    staged = stage_minute_zips_from_store(
        workspace,
        symbol="SPY",
        trading_dates=[DAY_ONE, DAY_TWO],
        roots=[store_root],
    )

    assert [p.name for p in staged] == ["20260105_trade.zip", "20260106_trade.zip"]
    for source, dest in zip(store_zips, staged, strict=True):
        assert _sha256(source) == _sha256(dest)


def test_first_root_wins_reference_first(tmp_path: Path):
    reference = tmp_path / "reference"
    cache = tmp_path / "polygon-raw"
    ref_zip = seed_store_day(reference, "SPY", DAY_ONE, count=100)
    seed_store_day(cache, "SPY", DAY_ONE, count=390)
    workspace = resolve_workspace("stage-root-order-test", tmp_path / "artifacts")
    workspace.ensure_layout()

    staged = stage_minute_zips_from_store(
        workspace,
        symbol="SPY",
        trading_dates=[DAY_ONE],
        roots=[reference, cache],
    )

    assert _sha256(staged[0]) == _sha256(ref_zip)


def test_missing_date_raises(tmp_path: Path):
    store_root = tmp_path / "polygon-raw"
    seed_store_day(store_root, "SPY", DAY_ONE)
    workspace = resolve_workspace("stage-missing-day-test", tmp_path / "artifacts")
    workspace.ensure_layout()

    with pytest.raises(FileNotFoundError, match=r"20260106_trade\.zip"):
        stage_minute_zips_from_store(
            workspace,
            symbol="SPY",
            trading_dates=[DAY_ONE, DAY_TWO],
            roots=[store_root],
        )
