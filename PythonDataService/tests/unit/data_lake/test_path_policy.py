"""Unit tests for app.data_lake.path_policy.

Spec: docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md § 5.3
"""

from __future__ import annotations

from datetime import date
from pathlib import Path, PurePosixPath

import pytest

from app.config import settings
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
    lake_container_within,
    lake_root_within,
    minute_bar_market_root,
    resolve_lake_container,
    resolve_lake_root,
    resolve_staging_root,
    staging_root_within,
)


class TestMinuteBarMarketRoot:
    def test_root_for_usa_is_the_prefix_of_a_full_minute_bar_path(self):
        root = minute_bar_market_root("usa")
        assert root == PurePosixPath("equity/usa/minute"), "root for usa"

        full = LeanMinuteBarPath(
            market="usa", symbol="SPY", trading_date=date(2024, 5, 20), data_type="trade"
        ).relative_path()
        assert str(full).startswith(str(root) + "/"), "the root is the prefix of a full minute-bar path"


class TestLeanMinuteBarPath:
    @pytest.mark.parametrize(
        ("data_type", "expected"),
        [
            ("trade", "equity/usa/minute/spy/20240520_trade.zip"),
            ("quote", "equity/usa/minute/spy/20240520_quote.zip"),
        ],
        ids=["relative_path_for_spy_trade", "relative_path_for_spy_quote"],
    )
    def test_relative_path_for_spy(self, data_type: str, expected: str):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            data_type=data_type,  # type: ignore[arg-type]
        ).relative_path()
        assert path == PurePosixPath(expected)

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


class TestLeanSymbolPaths:
    @pytest.mark.parametrize(
        ("path_class", "expected"),
        [
            (LeanDailyBarPath, "equity/usa/daily/spy.zip"),
            (LeanFactorFilePath, "equity/usa/factor_files/spy.csv"),
            (LeanMapFilePath, "equity/usa/map_files/spy.csv"),
        ],
        ids=["daily_bar_relative_path_for_spy", "factor_file_relative_path_for_spy", "map_file_relative_path_for_spy"],
    )
    def test_relative_path_for_spy(self, path_class: type, expected: str):
        path = path_class(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath(expected)


class TestLeanMetadataPath:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("market_hours", "market-hours/market-hours-database.json"),
            ("symbol_properties", "symbol-properties/symbol-properties-database.csv"),
        ],
        ids=["market_hours", "symbol_properties"],
    )
    def test_relative_path_for_kind(self, kind: str, expected: str):
        path = LeanMetadataPath(kind=kind).relative_path()  # type: ignore[arg-type]
        assert path == PurePosixPath(expected)


class TestLakeRoots:
    """The absolute roots writer and readers must agree on."""

    def test_lake_and_staging_roots_derive_from_the_write_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lake roots carry the mode; the container is the parent of every mode root; staging is partitioned the same way.

        The container is what "is this path in the lake at all?" asks.
        ``policy_store``'s write refusal compares against it rather than
        against one mode root: an exact match on a single root would wave
        through a policy-store write aimed at a sibling mode's subtree.

        Atomic promotion is a rename(2), so the lake and staging roots share a
        filesystem; the shared ancestor is ``<write root>``, which is the
        invariant ``atomic.assert_same_filesystem`` needs.

        The root-relative destination path carries no mode, so raw and
        adjusted bytes for one (symbol, date) would otherwise stage to the
        same ``.tmp`` under a shared ``request_id`` and one could promote the
        other's bytes into the wrong tree.
        """
        monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", "/mnt/writer")

        assert resolve_lake_root("raw") == Path("/mnt/writer/lake/raw"), "lake root derives from the write root and carries the mode (raw)"
        assert resolve_lake_root("polygon_split_adjusted") == Path(
            "/mnt/writer/lake/polygon_split_adjusted"
        ), "lake root derives from the write root and carries the mode (polygon_split_adjusted)"

        container = resolve_lake_container()
        assert container == Path("/mnt/writer/lake"), "lake container is the lake directory under the write root"
        for mode in ("raw", "polygon_split_adjusted", "lean_adjusted"):
            assert resolve_lake_root(mode).parent == container, f"lake container is the parent of the {mode} root"

        assert resolve_staging_root("raw") == Path("/mnt/writer/staging/raw"), "staging root derives from the write root"
        assert (
            resolve_lake_root("raw").parent.parent == resolve_staging_root("raw").parent.parent
        ), "staging shares a filesystem with the lake"
        assert resolve_staging_root("raw") != resolve_staging_root(
            "polygon_split_adjusted"
        ), "staging is partitioned by mode like the lake root"

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


class TestWithinVariants:
    """Root-parameterized siblings of resolve_lake_root/resolve_staging_root.

    These take an explicit base root instead of reading
    settings.LEAN_DATA_WRITE_ROOT, so a caller holding a
    root_identity.RootContext for a non-active root can still derive its
    mode-keyed paths through path_policy — the sole authority for
    constructing LEAN on-disk paths (module docstring) — rather than a
    second, ad hoc join. The settings-based resolve_* functions below become
    thin wrappers over these for the configured active root.
    """

    def test_the_within_variants_derive_mode_keyed_paths_from_the_base_root(self):
        assert lake_container_within(Path("/mnt/other-root")) == Path(
            "/mnt/other-root/lake"
        ), "lake container within is base root slash lake"
        assert lake_root_within(Path("/mnt/other-root"), "raw") == Path(
            "/mnt/other-root/lake/raw"
        ), "lake root within carries the mode (raw)"
        assert lake_root_within(Path("/mnt/other-root"), "polygon_split_adjusted") == Path(
            "/mnt/other-root/lake/polygon_split_adjusted"
        ), "lake root within carries the mode (polygon_split_adjusted)"
        assert staging_root_within(Path("/mnt/other-root"), "raw") == Path(
            "/mnt/other-root/staging/raw"
        ), "staging root within carries the mode"

    def test_lake_root_within_refuses_an_unknown_mode(self):
        with pytest.raises(ValueError, match="is not a price adjustment mode"):
            lake_root_within(Path("/mnt/other-root"), "../../etc")  # type: ignore[arg-type]


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
