"""The one-time move of the pre-#1839 lake tree into its mode root."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.data_lake.path_policy import lake_subpath, resolve_lake_container
from scripts.migrate_lake_to_mode_roots import LEGACY_MODE_MARKER, legacy_tree_mode, migrate, plan_moves


@pytest.fixture
def write_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(root))
    return root


def _seed_legacy_tree(write_root: Path) -> Path:
    zip_path = write_root / "lake" / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip"
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(b"raw minute bytes")
    return zip_path


def test_an_unmarked_tree_is_raw(write_root: Path) -> None:
    """The live pipeline could not have produced anything else.

    ``DataRunSpec.price_adjustment_mode`` was pinned ``Literal["raw"]`` and
    the fetcher hardcoded ``adjusted=false``, so an unmarked tree is raw by
    construction rather than by assumption.
    """
    _seed_legacy_tree(write_root)

    assert legacy_tree_mode(resolve_lake_container()) == "raw"


def test_a_marked_tree_keeps_the_mode_the_marker_records(write_root: Path) -> None:
    """The one way a tree could hold non-raw bytes was an adjusted import,
    which stamped the marker. Read it rather than relabelling it raw."""
    _seed_legacy_tree(write_root)
    (write_root / "lake" / LEGACY_MODE_MARKER).write_text("polygon_split_adjusted")

    assert legacy_tree_mode(resolve_lake_container()) == "polygon_split_adjusted"


def test_an_unrecognized_marker_refuses_rather_than_guessing(write_root: Path) -> None:
    _seed_legacy_tree(write_root)
    (write_root / "lake" / LEGACY_MODE_MARKER).write_text("something-else")

    with pytest.raises(SystemExit, match="not a known mode"):
        legacy_tree_mode(resolve_lake_container())


def test_dry_run_moves_nothing(write_root: Path) -> None:
    original = _seed_legacy_tree(write_root)

    assert migrate(apply=False) == 0
    assert original.is_file()


def test_apply_moves_the_tree_under_the_mode_root(write_root: Path) -> None:
    original = _seed_legacy_tree(write_root)
    content = original.read_bytes()

    assert migrate(apply=True) == 0

    assert not original.exists()
    moved = write_root / lake_subpath("raw") / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip"
    assert moved.read_bytes() == content


def test_apply_is_idempotent(write_root: Path) -> None:
    _seed_legacy_tree(write_root)
    migrate(apply=True)

    assert plan_moves(resolve_lake_container()) == []
    assert migrate(apply=True) == 0  # second run is a no-op, not an error


def test_apply_refuses_to_merge_onto_an_existing_tree(write_root: Path) -> None:
    """Merging could pair a catalog row with bytes it does not describe."""
    _seed_legacy_tree(write_root)
    (write_root / lake_subpath("raw") / "equity").mkdir(parents=True)

    with pytest.raises(SystemExit, match="refusing to merge"):
        migrate(apply=True)


def test_a_conflict_on_a_later_entry_moves_nothing_at_all(write_root: Path) -> None:
    """The refusal must leave the tree exactly as it found it.

    Regression for #1866 review: the conflict check used to run inside the
    move loop, so a clash on the second entry aborted *after* the first had
    already been renamed. That left a half-migrated lake a re-run could not
    finish, because the moved entry was no longer in the container for
    ``plan_moves`` to see.
    """
    _seed_legacy_tree(write_root)
    legacy_metadata = write_root / "lake" / "market-hours" / "market-hours-database.json"
    legacy_metadata.parent.mkdir(parents=True)
    legacy_metadata.write_bytes(b"{}")

    # Only the *second* entry (sorted: equity, then market-hours) collides.
    (write_root / lake_subpath("raw") / "market-hours").mkdir(parents=True)

    with pytest.raises(SystemExit, match="refusing to merge"):
        migrate(apply=True)

    # Nothing moved: both legacy entries are still where they were.
    assert (write_root / "lake" / "equity").is_dir()
    assert legacy_metadata.is_file()
    assert not (write_root / lake_subpath("raw") / "equity").exists()


def test_a_missing_lake_is_not_an_error(write_root: Path) -> None:
    """A deployment that has never written to the lake has nothing to move."""
    assert plan_moves(resolve_lake_container()) == []
    assert migrate(apply=True) == 0
