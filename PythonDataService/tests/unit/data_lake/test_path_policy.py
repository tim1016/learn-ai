"""Unit tests for app.data_lake.path_policy.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

from datetime import date
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from app.config import settings
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
    minute_bar_market_root,
    resolve_lake_container,
    resolve_lake_root,
    resolve_staging_root,
    staging_path_for,
)


class TestMinuteBarMarketRoot:
    def test_root_for_usa(self):
        assert minute_bar_market_root("usa") == PurePosixPath("equity/usa/minute")

    def test_is_the_prefix_of_a_full_minute_bar_path(self):
        root = minute_bar_market_root("usa")
        full = LeanMinuteBarPath(
            market="usa", symbol="SPY", trading_date=date(2024, 5, 20), data_type="trade"
        ).relative_path()
        assert str(full).startswith(str(root) + "/")


class TestLeanMinuteBarPath:
    def test_relative_path_for_spy_trade(self):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            data_type="trade",
        ).relative_path()
        assert path == PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")

    def test_relative_path_for_spy_quote(self):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            data_type="quote",
        ).relative_path()
        assert path == PurePosixPath("equity/usa/minute/spy/20240520_quote.zip")

    def test_symbol_lowercased_in_path(self):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="QQQ",
            trading_date=date(2024, 1, 2),
            data_type="trade",
        ).relative_path()
        # Symbol portion of the path is lowercased per LEAN convention.
        assert "qqq" in str(path)
        assert "QQQ" not in str(path)


class TestLeanDailyBarPath:
    def test_relative_path_for_spy(self):
        path = LeanDailyBarPath(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath("equity/usa/daily/spy.zip")


class TestLeanFactorFilePath:
    def test_relative_path_for_spy(self):
        path = LeanFactorFilePath(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath("equity/usa/factor_files/spy.csv")


class TestLeanMapFilePath:
    def test_relative_path_for_spy(self):
        path = LeanMapFilePath(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath("equity/usa/map_files/spy.csv")


class TestLeanMetadataPath:
    def test_market_hours(self):
        path = LeanMetadataPath(kind="market_hours").relative_path()
        assert path == PurePosixPath("market-hours/market-hours-database.json")

    def test_symbol_properties(self):
        path = LeanMetadataPath(kind="symbol_properties").relative_path()
        assert path == PurePosixPath("symbol-properties/symbol-properties-database.csv")


class TestStagingPathFor:
    def test_staging_path_isolation(self):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        worker_id = "worker-7"
        attempt = 2
        staged = staging_path_for(rel, request_id, worker_id, attempt)
        assert staged == PurePosixPath(
            "staging/12345678-1234-5678-1234-567812345678/worker-7/attempt_2/"
            "equity/usa/minute/spy/20240520_trade.zip.tmp"
        )

    def test_two_attempts_produce_distinct_paths(self):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        a1 = staging_path_for(rel, request_id, "worker-1", 1)
        a2 = staging_path_for(rel, request_id, "worker-1", 2)
        assert a1 != a2


class TestLakeRoots:
    """The absolute roots writer and readers must agree on."""

    def test_lake_root_derives_from_write_root_and_carries_the_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert resolve_lake_root("raw") == Path("/mnt/writer/lake/raw")
        assert resolve_lake_root("polygon_split_adjusted") == Path("/mnt/writer/lake/polygon_split_adjusted")

    def test_lake_container_is_the_parent_of_every_mode_root(self, monkeypatch):
        """The container is what "is this path in the lake at all?" asks.

        ``policy_store``'s write refusal compares against it rather than
        against one mode root: an exact match on a single root would wave
        through a policy-store write aimed at a sibling mode's subtree.
        """
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        container = resolve_lake_container()

        assert container == Path("/mnt/writer/lake")
        for mode in ("raw", "polygon_split_adjusted", "lean_adjusted"):
            assert resolve_lake_root(mode).parent == container

    def test_staging_root_derives_from_write_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert resolve_staging_root("raw") == Path("/mnt/writer/staging/raw")

    def test_staging_shares_a_filesystem_with_the_lake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Atomic promotion is a rename(2), so both roots share a filesystem.

        The shared ancestor is ``<write root>``; same filesystem either way,
        which is the invariant ``atomic.assert_same_filesystem`` needs.
        """
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert resolve_lake_root("raw").parent.parent == resolve_staging_root("raw").parent.parent

    def test_staging_is_partitioned_by_mode_like_the_lake_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two modes must not name the same staged file.

        The root-relative destination path carries no mode, so raw and
        adjusted bytes for one (symbol, date) would otherwise stage to the
        same ``.tmp`` under a shared ``request_id`` and one could promote the
        other's bytes into the wrong tree.
        """
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert resolve_staging_root("raw") != resolve_staging_root("polygon_split_adjusted")

    def test_staging_root_refuses_a_mode_it_does_not_know(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        with pytest.raises(ValueError, match="is not a price adjustment mode"):
            resolve_staging_root("../../etc")  # type: ignore[arg-type]

    def test_ensure_data_imports_the_same_functions_and_does_not_re_derive(self, monkeypatch):
        """One answer to "where is the lake?" — ensure_data must not re-derive it.

        ``ensure_data`` imports ``resolve_lake_root`` / ``resolve_staging_root``
        directly rather than through a wrapper of its own; asserting identity
        (not just equal output) catches a reimplementation that happens to
        agree today but could silently drift from this module.
        """
        from app.data_lake import ensure_data

        assert ensure_data.resolve_lake_root is resolve_lake_root
        assert ensure_data.resolve_staging_root is resolve_staging_root


class TestLakeRootRefusesUntrustedModes:
    """The mode reaches ``resolve_lake_root`` from request input and is now a
    path segment, so it is validated at the one place the segment is built."""

    @pytest.mark.parametrize(
        "mode",
        ["../../etc", "raw/../../..", "..", "/absolute", "nonsense", ""],
    )
    def test_a_mode_that_is_not_one_refuses(self, monkeypatch, mode: str):
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")

        with pytest.raises(ValueError):
            resolve_lake_root(mode)  # type: ignore[arg-type]

    @pytest.mark.parametrize("mode", ["raw", "polygon_split_adjusted", "lean_adjusted"])
    def test_every_real_mode_resolves_inside_the_container(self, monkeypatch, mode: str):
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        container = resolve_lake_container()

        root = resolve_lake_root(mode)  # type: ignore[arg-type]

        assert root.parent == container
        assert root != container
