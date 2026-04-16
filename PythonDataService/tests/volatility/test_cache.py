"""
Tests for app.volatility.cache module.

Tests surface ID computation, data filters, and cache I/O.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.volatility.cache import (
    SCHEMA_VERSION,
    DataFilters,
    SurfaceCache,
    compute_surface_id,
)
from app.volatility.conventions import SurfaceConventions


class TestSurfaceIdComputation:
    """Deterministic surface ID generation tests."""

    def test_surface_id_deterministic(self) -> None:
        """Same inputs → same ID."""
        ticker = "AAPL"
        date = "2026-04-12"
        method = "svi"
        conventions = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        filters = DataFilters(min_dte=7, max_dte=365)
        n_options = 100

        id_1 = compute_surface_id(ticker, date, method, conventions, filters, n_options)
        id_2 = compute_surface_id(ticker, date, method, conventions, filters, n_options)

        assert id_1 == id_2

    def test_surface_id_different_ticker(self) -> None:
        """Different ticker → different ID."""
        date = "2026-04-12"
        method = "svi"
        conventions = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        filters = DataFilters(min_dte=7, max_dte=365)
        n_options = 100

        id_aapl = compute_surface_id("AAPL", date, method, conventions, filters, n_options)
        id_msft = compute_surface_id("MSFT", date, method, conventions, filters, n_options)

        assert id_aapl != id_msft

    def test_surface_id_different_method(self) -> None:
        """Different method → different ID."""
        ticker = "AAPL"
        date = "2026-04-12"
        conventions = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        filters = DataFilters(min_dte=7, max_dte=365)
        n_options = 100

        id_variance = compute_surface_id(ticker, date, "variance", conventions, filters, n_options)
        id_svi = compute_surface_id(ticker, date, "svi", conventions, filters, n_options)

        assert id_variance != id_svi

    def test_surface_id_different_rate(self) -> None:
        """Different rate → different ID."""
        ticker = "AAPL"
        date = "2026-04-12"
        method = "svi"
        filters = DataFilters(min_dte=7, max_dte=365)
        n_options = 100

        conventions_1 = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        conventions_2 = SurfaceConventions(rate=0.03, dividend_yield=0.0)

        id_1 = compute_surface_id(ticker, date, method, conventions_1, filters, n_options)
        id_2 = compute_surface_id(ticker, date, method, conventions_2, filters, n_options)

        assert id_1 != id_2

    def test_surface_id_different_date(self) -> None:
        """Different date → different ID."""
        ticker = "AAPL"
        method = "svi"
        conventions = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        filters = DataFilters(min_dte=7, max_dte=365)
        n_options = 100

        id_1 = compute_surface_id(ticker, "2026-04-12", method, conventions, filters, n_options)
        id_2 = compute_surface_id(ticker, "2026-04-13", method, conventions, filters, n_options)

        assert id_1 != id_2

    def test_surface_id_different_n_options(self) -> None:
        """Different n_options → different ID."""
        ticker = "AAPL"
        date = "2026-04-12"
        method = "svi"
        conventions = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        filters = DataFilters(min_dte=7, max_dte=365)

        id_1 = compute_surface_id(ticker, date, method, conventions, filters, 100)
        id_2 = compute_surface_id(ticker, date, method, conventions, filters, 101)

        assert id_1 != id_2

    def test_surface_id_length(self) -> None:
        """Surface ID is always 20 hex characters."""
        ticker = "AAPL"
        date = "2026-04-12"
        method = "svi"
        conventions = SurfaceConventions(rate=0.05, dividend_yield=0.0)
        filters = DataFilters(min_dte=7, max_dte=365)
        n_options = 100

        surface_id = compute_surface_id(ticker, date, method, conventions, filters, n_options)

        assert len(surface_id) == 20
        assert all(c in "0123456789abcdef" for c in surface_id)


class TestDataFiltersDefaults:
    """DataFilters default values test."""

    def test_data_filters_defaults(self) -> None:
        """Verify DataFilters default values."""
        filters = DataFilters()

        assert filters.min_dte == 7
        assert filters.max_dte == 365
        assert filters.min_open_interest == 10
        assert filters.max_spread_pct == 0.20

    def test_data_filters_custom_values(self) -> None:
        """Custom DataFilters values."""
        filters = DataFilters(min_dte=3, max_dte=730, min_open_interest=5, max_spread_pct=0.10)

        assert filters.min_dte == 3
        assert filters.max_dte == 730
        assert filters.min_open_interest == 5
        assert filters.max_spread_pct == 0.10


class TestSurfaceCacheMetadata:
    """Cache metadata I/O tests."""

    def test_cache_write_read_meta(self, tmp_path: Path) -> None:
        """Write then read meta JSON, verify round-trip."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "test_surface_id_001"

        meta = {
            "ticker": "AAPL",
            "date": "2026-04-12",
            "method": "svi",
            "n_options": 150,
        }

        cache.write_meta(surface_id, meta)

        read_meta = cache.read_meta(surface_id)
        assert read_meta is not None
        assert read_meta["ticker"] == "AAPL"
        assert read_meta["date"] == "2026-04-12"
        assert read_meta["method"] == "svi"
        assert read_meta["n_options"] == 150
        assert read_meta["schema_version"] == SCHEMA_VERSION
        assert "quantlib_version" in read_meta
        assert "built_at" in read_meta

    def test_cache_read_nonexistent_meta(self, tmp_path: Path) -> None:
        """Reading nonexistent meta returns None."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))

        read_meta = cache.read_meta("nonexistent_surface")
        assert read_meta is None


class TestSurfaceCacheGrid:
    """Cache grid (Parquet) I/O tests."""

    def test_cache_write_read_grid(self, tmp_path: Path) -> None:
        """Write then read grid Parquet, verify data."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "test_surface_grid_001"

        grid = {
            "strikes": [90.0, 95.0, 100.0, 105.0, 110.0],
            "ttms": [0.08, 0.08, 0.08, 0.08, 0.08],
            "ivs": [0.28, 0.26, 0.25, 0.26, 0.28],
        }

        cache.write_grid(surface_id, grid)

        read_grid = cache.read_grid(surface_id)
        assert read_grid is not None
        assert len(read_grid["strikes"]) == 5
        assert read_grid["strikes"][2] == 100.0
        assert abs(read_grid["ivs"][2] - 0.25) < 1e-6

    def test_cache_read_nonexistent_grid(self, tmp_path: Path) -> None:
        """Reading nonexistent grid returns None."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))

        read_grid = cache.read_grid("nonexistent_surface")
        assert read_grid is None


class TestSurfaceCacheSmiles:
    """Cache smiles (JSON) I/O tests."""

    def test_cache_write_read_smiles(self, tmp_path: Path) -> None:
        """Write then read smiles JSON."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "test_surface_smiles_001"

        smiles = {
            "0.08": {"strikes": [90.0, 100.0, 110.0], "ivs": [0.28, 0.25, 0.28]},
            "0.25": {"strikes": [85.0, 100.0, 115.0], "ivs": [0.32, 0.24, 0.32]},
        }

        cache.write_smiles(surface_id, smiles)

        read_smiles = cache.read_smiles(surface_id)
        assert read_smiles is not None
        assert "0.08" in read_smiles
        assert read_smiles["0.08"]["strikes"][1] == 100.0

    def test_cache_read_nonexistent_smiles(self, tmp_path: Path) -> None:
        """Reading nonexistent smiles returns None."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))

        read_smiles = cache.read_smiles("nonexistent_surface")
        assert read_smiles is None


class TestSurfaceCacheDiagnostics:
    """Cache diagnostics (JSON) I/O tests."""

    def test_cache_write_read_diagnostics(self, tmp_path: Path) -> None:
        """Write then read diagnostics JSON."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "test_surface_diag_001"

        diagnostics = {
            "n_expiries": 4,
            "n_total_solved": 142,
            "n_total_failed": 8,
            "fit_quality": "good",
        }

        cache.write_diagnostics(surface_id, diagnostics)

        read_diag = cache.read_diagnostics(surface_id)
        assert read_diag is not None
        assert read_diag["n_expiries"] == 4
        assert read_diag["n_total_solved"] == 142


class TestSurfaceCacheExistence:
    """Cache existence checks."""

    def test_cache_exists_false_before_write(self, tmp_path: Path) -> None:
        """exists() returns False before writing."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "new_surface"

        assert cache.exists(surface_id) is False

    def test_cache_exists_true_after_write(self, tmp_path: Path) -> None:
        """exists() returns True after writing all artifacts."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "complete_surface"

        meta = {"ticker": "AAPL"}
        grid = {"strikes": [100.0], "ttms": [0.25], "ivs": [0.25]}
        smiles = {"0.25": {"strikes": [100.0], "ivs": [0.25]}}
        diagnostics = {"n_expiries": 1}

        cache.write_meta(surface_id, meta)
        cache.write_grid(surface_id, grid)
        cache.write_smiles(surface_id, smiles)
        cache.write_diagnostics(surface_id, diagnostics)

        assert cache.exists(surface_id) is True

    def test_cache_exists_partial_write(self, tmp_path: Path) -> None:
        """exists() returns False if only some artifacts are written."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "partial_surface"

        meta = {"ticker": "AAPL"}
        cache.write_meta(surface_id, meta)

        assert cache.exists(surface_id) is False


class TestSurfaceCacheValidation:
    """Cache validation tests."""

    def test_cache_is_valid_schema_mismatch(self, tmp_path: Path) -> None:
        """is_valid() returns False if schema_version mismatches."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "invalid_schema"

        meta = {"ticker": "AAPL", "schema_version": "0.9.0"}
        grid = {"strikes": [100.0], "ttms": [0.25], "ivs": [0.25]}
        smiles = {"0.25": {"strikes": [100.0], "ivs": [0.25]}}
        diagnostics = {"n_expiries": 1}

        meta_path = cache.surfaces_dir / f"{surface_id}.meta.json"
        with open(meta_path, "w") as f:
            json.dump(meta, f)

        cache.write_grid(surface_id, grid)
        cache.write_smiles(surface_id, smiles)
        cache.write_diagnostics(surface_id, diagnostics)

        assert cache.is_valid(surface_id) is False

    def test_cache_is_valid_correct_schema(self, tmp_path: Path) -> None:
        """is_valid() returns True with correct schema_version."""
        cache = SurfaceCache(cache_dir=str(tmp_path / "cache"))
        surface_id = "valid_schema"

        meta = {"ticker": "AAPL"}
        grid = {"strikes": [100.0], "ttms": [0.25], "ivs": [0.25]}
        smiles = {"0.25": {"strikes": [100.0], "ivs": [0.25]}}
        diagnostics = {"n_expiries": 1}

        cache.write_meta(surface_id, meta)
        cache.write_grid(surface_id, grid)
        cache.write_smiles(surface_id, smiles)
        cache.write_diagnostics(surface_id, diagnostics)

        assert cache.is_valid(surface_id) is True
