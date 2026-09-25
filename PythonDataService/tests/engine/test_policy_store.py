"""Tests for the policy-keyed canonical bar store (app.engine.data.policy_store)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.data_lake import path_policy
from app.engine.data.policy_store import (
    policy_key,
    resolve_data_roots,
    snapshot_minute_trade_zips,
)
from app.lean_sidecar.workspace import SymbolValidationError


def test_resolve_data_roots_returns_the_lake_root_alone(monkeypatch, tmp_path: Path):
    """The lake is the only reader root (#1893).

    The pre-lake arrangement stacked a read-only reference mount in front of a
    policy-keyed cache subtree, selected by DATA_LAKE_ENABLED. Both are gone:
    a run must be able to say which bytes it consumed, and a fixture silently
    outranking the lake would make that answer a lie.
    """
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(tmp_path))

    roots = resolve_data_roots(source="polygon", adjusted=False)

    assert roots == [path_policy.resolve_lake_root("raw")]
    assert roots[0].exists(), "the lake root is created if missing"


def test_resolve_data_roots_serves_each_adjustment_mode_from_its_own_root(monkeypatch, tmp_path: Path):
    """``adjusted`` selects a different physical root, and is never refused.

    Since #1866 the mode is a segment of the root, so raw and split-adjusted
    requests cannot name the same directory -- which is what makes returning
    the requested mode's bytes, rather than raising, the honest answer.
    """
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(tmp_path))

    raw = resolve_data_roots(source="polygon", adjusted=False)
    adjusted = resolve_data_roots(source="polygon", adjusted=True)

    assert raw == [path_policy.resolve_lake_root("raw")]
    assert adjusted == [path_policy.resolve_lake_root("polygon_split_adjusted")]
    assert raw != adjusted


def test_policy_key_encodes_source_and_adjustment():
    assert policy_key(source="polygon", adjusted=True) == "polygon-adjusted"
    assert policy_key(source="polygon", adjusted=False) == "polygon-raw"




























def test_snapshot_minute_trade_zips_is_path_independent_and_reference_first(tmp_path: Path):
    reference = tmp_path / "reference"
    cache = tmp_path / "cache"
    logical = Path("equity/usa/minute/spy")
    (reference / logical).mkdir(parents=True)
    (cache / logical).mkdir(parents=True)
    (reference / logical / "20260105_trade.zip").write_bytes(b"reference-day-one")
    (cache / logical / "20260105_trade.zip").write_bytes(b"shadowed-cache-day-one")
    (cache / logical / "20260106_trade.zip").write_bytes(b"cache-day-two")

    receipt = snapshot_minute_trade_zips(
        [reference, cache],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        adjusted=False,
        session="regular",
    )

    assert receipt["fixture_id"].startswith("bar-store-v1-")
    assert len(receipt["fixture_sha256"]) == 64
    assert [item["path"] for item in receipt["files"]] == [
        "equity/usa/minute/spy/20260105_trade.zip",
        "equity/usa/minute/spy/20260106_trade.zip",
    ]
    assert receipt["files"][0]["size_bytes"] == len(b"reference-day-one")

    relocated_reference = tmp_path / "elsewhere" / "reference"
    relocated_cache = tmp_path / "elsewhere" / "cache"
    (relocated_reference / logical).mkdir(parents=True)
    (relocated_cache / logical).mkdir(parents=True)
    (relocated_reference / logical / "20260105_trade.zip").write_bytes(b"reference-day-one")
    (relocated_cache / logical / "20260106_trade.zip").write_bytes(b"cache-day-two")
    relocated = snapshot_minute_trade_zips(
        [relocated_reference, relocated_cache],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        adjusted=False,
        session="regular",
    )
    assert relocated["fixture_sha256"] == receipt["fixture_sha256"]


def test_snapshot_minute_trade_zips_changes_when_one_input_byte_changes(tmp_path: Path):
    logical = tmp_path / "equity/usa/minute/spy"
    logical.mkdir(parents=True)
    path = logical / "20260105_trade.zip"
    path.write_bytes(b"first")
    before = snapshot_minute_trade_zips(
        [tmp_path],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 5),
        adjusted=False,
        session="regular",
    )
    path.write_bytes(b"second")
    after = snapshot_minute_trade_zips(
        [tmp_path],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 5),
        adjusted=False,
        session="regular",
    )

    assert after["fixture_sha256"] != before["fixture_sha256"]


def test_snapshot_minute_trade_zips_rejects_path_unsafe_symbol(tmp_path: Path):
    with pytest.raises(SymbolValidationError):
        snapshot_minute_trade_zips(
            [tmp_path],
            symbol="../evil",
            start=date(2026, 1, 5),
            end=date(2026, 1, 5),
            adjusted=False,
            session="regular",
        )


def test_snapshot_minute_trade_zips_ignores_symlink_that_escapes_root(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    symbol_parent = root / "equity" / "usa" / "minute"
    symbol_parent.mkdir(parents=True)
    outside.mkdir()
    (outside / "20260105_trade.zip").write_bytes(b"outside-root")
    (symbol_parent / "spy").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError, match="no minute trade zips"):
        snapshot_minute_trade_zips(
            [root],
            symbol="SPY",
            start=date(2026, 1, 5),
            end=date(2026, 1, 5),
            adjusted=False,
            session="regular",
        )
