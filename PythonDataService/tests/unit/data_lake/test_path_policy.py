"""Unit tests for app.data_lake.path_policy.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

from datetime import date
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.config import settings
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
    lake_root,
    staging_path_for,
    staging_root,
)


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

    def test_lake_root_derives_from_write_root(self, monkeypatch):
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert lake_root() == Path("/mnt/writer/lake")

    def test_staging_root_derives_from_write_root(self, monkeypatch):
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert staging_root() == Path("/mnt/writer/staging")

    def test_staging_shares_a_filesystem_with_the_lake(self, monkeypatch):
        """Atomic promotion is a rename(2), so both roots share a parent."""
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        assert lake_root().parent == staging_root().parent

    def test_ensure_data_writes_where_path_policy_points(self, monkeypatch):
        """One answer to "where is the lake?" — ensure_data must not re-derive it."""
        from app.data_lake.ensure_data import _lake_roots

        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")
        spec = _minimal_spec()
        assert _lake_roots(spec) == (lake_root(), staging_root())


def _minimal_spec():
    from app.data_lake.types import DataRunSpec

    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 20),
        lean_image_digest="sha256:test",
    )
