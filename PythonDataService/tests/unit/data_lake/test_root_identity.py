"""Unit tests for app.data_lake.root_identity.

Issue #1876 (PR A of #1861): the marker at ``<base-root>/lake/.data-root.json``
is the sole, portable source of a physical lake root's identity. These tests
cover the acceptance-criteria checklist verbatim: marker parsing/validation,
expected-UUID comparison, explicit empty-root init, refusal to auto-stamp a
populated unmarked root, atomic marker creation, the typed root-context
object, and LakeRootIdentityError.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from app.config import settings
from app.data_lake.path_policy import lake_container_within, lake_root_within, staging_root_within
from app.data_lake.root_identity import (
    LEGACY_ROOT_ID,
    MARKER_FILENAME,
    LakeRootIdentityError,
    RootContext,
    _atomic_write_marker,
    active_root_id,
    init_empty_root,
    inspect_root,
    marker_path,
    read_marker,
    resolve_root_context,
    stamp_existing_root,
)

_ROOT_A = UUID("11111111-1111-1111-1111-111111111111")
_ROOT_B = UUID("22222222-2222-2222-2222-222222222222")


def _write_marker(base_root: Path, *, schema_version: int = 1, data_root_id: str) -> Path:
    path = marker_path(base_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": schema_version, "data_root_id": data_root_id}))
    return path


class TestMarkerPath:
    def test_marker_lives_under_lake_container(self, tmp_path: Path):
        assert marker_path(tmp_path) == lake_container_within(tmp_path) / ".data-root.json"


class TestReadMarker:
    def test_returns_none_when_no_marker_file_exists(self, tmp_path: Path):
        assert read_marker(tmp_path) is None

    def test_parses_a_well_formed_marker(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id=str(_ROOT_A))

        marker = read_marker(tmp_path)

        assert marker is not None
        assert marker.data_root_id == _ROOT_A
        assert marker.schema_version == 1

    @pytest.mark.parametrize(
        ("marker_text", "match"),
        [
            ("{not json", "malformed"),
            (json.dumps({"schema_version": 2, "data_root_id": str(_ROOT_A)}), "schema_version"),
            (json.dumps({"schema_version": 1, "data_root_id": "not-a-uuid"}), None),
            # No timestamps, no host-specific paths — the marker carries exactly
            # schema_version and data_root_id, nothing else (issue #1876).
            (json.dumps({"schema_version": 1, "data_root_id": str(_ROOT_A), "created_at": "2026-01-01"}), None),
        ],
        ids=[
            "raises_on_malformed_json",
            "raises_on_wrong_schema_version",
            "raises_on_invalid_uuid",
            "raises_on_extra_fields_issue_1876",
        ],
    )
    def test_raises_on_a_marker_that_is_not_valid(self, tmp_path: Path, marker_text: str, match: str | None):
        path = marker_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker_text)

        with pytest.raises(LakeRootIdentityError, match=match):
            read_marker(tmp_path)


class TestResolveRootContext:
    def test_missing_marker_raises_before_anything_is_mutated_and_never_silently_creates_one(self, tmp_path: Path):
        """Normal application startup validates, never creates (issue #1876)."""
        with pytest.raises(LakeRootIdentityError, match="missing"):
            resolve_root_context(_ROOT_A, base_root=tmp_path)

        assert not marker_path(tmp_path).exists(), "the missing-marker error comes before anything is mutated"

        with pytest.raises(LakeRootIdentityError):
            resolve_root_context(_ROOT_A, base_root=tmp_path)

        assert read_marker(tmp_path) is None, "normal startup never silently creates a marker (issue #1876)"

    def test_mismatched_marker_raises(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id=str(_ROOT_A))

        with pytest.raises(LakeRootIdentityError, match="mismatch"):
            resolve_root_context(_ROOT_B, base_root=tmp_path)

    def test_matching_marker_returns_a_root_context(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id=str(_ROOT_A))

        ctx = resolve_root_context(_ROOT_A, base_root=tmp_path)

        assert isinstance(ctx, RootContext)
        assert ctx.root_id == _ROOT_A
        assert ctx.base_root == tmp_path


class TestRootContextPathResolution:

    def test_lake_and_staging_roots_match_path_policy(self, tmp_path: Path):
        ctx = RootContext(root_id=_ROOT_A, base_root=tmp_path)
        assert ctx.lake_root("raw") == lake_root_within(tmp_path, "raw"), "lake root matches path policy (raw)"
        assert ctx.lake_root("polygon_split_adjusted") == lake_root_within(
            tmp_path, "polygon_split_adjusted"
        ), "lake root matches path policy (polygon_split_adjusted)"
        assert ctx.staging_root("raw") == staging_root_within(tmp_path, "raw"), "staging root matches path policy"


class TestInitEmptyRoot:
    def test_creates_marker_at_the_expected_path_and_creation_is_atomic(self, tmp_path: Path):
        """The marker file, once present, is always fully-formed valid JSON —
        never a truncated partial write an interrupted process could leave."""
        ctx = init_empty_root(tmp_path, _ROOT_A)

        assert ctx.root_id == _ROOT_A, "creating the marker returns the root context"
        marker = read_marker(tmp_path)
        assert marker is not None, "the marker is created at the expected path"
        assert marker.data_root_id == _ROOT_A, "the marker carries the given root id"
        raw = marker_path(tmp_path).read_text()
        parsed = json.loads(raw)  # would raise on a torn/partial write
        assert parsed == {"schema_version": 1, "data_root_id": str(_ROOT_A)}, "no partial marker is left on disk"
        assert ctx.root_id == _ROOT_A, "the context still names the root after the atomic write"

    def test_refuses_when_a_marker_already_exists(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id=str(_ROOT_A))

        with pytest.raises(LakeRootIdentityError, match="already"):
            init_empty_root(tmp_path, _ROOT_B)

    def test_refuses_a_populated_root_with_no_marker(self, tmp_path: Path):
        """Never silently assign identity to a populated root (issue #1876)."""
        container = lake_container_within(tmp_path)
        container.mkdir(parents=True)
        (container / "raw").mkdir()
        (container / "raw" / "some_artifact.zip").write_bytes(b"data")

        with pytest.raises(LakeRootIdentityError, match="populated"):
            init_empty_root(tmp_path, _ROOT_A)

        assert read_marker(tmp_path) is None


class TestAtomicWriteMarkerConcurrency:
    """A second concurrent writer must never silently overwrite the first
    marker (TOCTOU between the ``read_marker(...) is not None`` check and
    the write itself in both administrative functions) — issue #1876
    review fix 2."""

    def test_a_second_write_to_an_already_marked_root_loses_and_leaves_the_first_intact(self, tmp_path: Path):
        _atomic_write_marker(tmp_path, _ROOT_A)

        with pytest.raises(LakeRootIdentityError, match="concurrently"):
            _atomic_write_marker(tmp_path, _ROOT_B)

        marker = read_marker(tmp_path)
        assert marker is not None, "the first writer's marker is still on disk"
        assert marker.data_root_id == _ROOT_A, "the first writer's marker survives the second writer's attempt"
        assert json.loads(marker_path(tmp_path).read_text()) == {
            "schema_version": 1,
            "data_root_id": str(_ROOT_A),
        }, "the first writer's marker file is byte-for-byte what it wrote"
        # The loser's staged file is cleaned up rather than left on disk.
        container = lake_container_within(tmp_path)
        leftovers = [p for p in container.iterdir() if p.name.startswith(f"{MARKER_FILENAME}.tmp-")]
        assert leftovers == [], "no leftover staged temp file after a losing write"
        assert [p.name for p in container.iterdir()] == [MARKER_FILENAME], "only the first writer's marker is left in the container"


class TestStampExistingRoot:
    @pytest.mark.parametrize(
        ("force", "match"),
        [
            (False, "force"),
            # Stamping a populated root with an arbitrary UUID would silently
            # orphan the catalog rows the migration backfilled to LEGACY_ROOT_ID
            # (issue #1876 review fix 4).
            (True, "populated"),
        ],
        ids=["requires_the_explicit_force_flag", "refuses_a_populated_root_with_a_non_legacy_root_id_issue_1876"],
    )
    def test_refuses_to_stamp_a_populated_root_with_a_non_legacy_root_id(
        self, tmp_path: Path, force: bool, match: str
    ):
        container = lake_container_within(tmp_path)
        container.mkdir(parents=True)
        (container / "raw").mkdir()

        with pytest.raises(LakeRootIdentityError, match=match):
            stamp_existing_root(tmp_path, _ROOT_A, force=force)

        assert read_marker(tmp_path) is None, "a refused stamp leaves no marker"

    @pytest.mark.parametrize(
        ("populated", "root_id"),
        [
            # The legitimate rollout path: the EF migration backfilled every
            # pre-existing catalog row to LEGACY_ROOT_ID, so stamping the
            # physical root that holds that data with the same id is safe.
            pytest.param(True, LEGACY_ROOT_ID, id="stamps_a_populated_root_with_the_legacy_root_id"),
            # An empty root has no existing catalog rows to orphan, so the
            # genuine "new secondary root" path still accepts any UUID.
            pytest.param(False, _ROOT_A, id="stamps_an_empty_root_with_a_non_legacy_root_id"),
        ],
    )
    def test_stamps_a_root_when_forced(self, tmp_path: Path, populated: bool, root_id: UUID):
        if populated:
            container = lake_container_within(tmp_path)
            container.mkdir(parents=True)
            (container / "raw").mkdir()

        ctx = stamp_existing_root(tmp_path, root_id, force=True)

        assert ctx.root_id == root_id, "the stamp returns a context naming the root id"
        marker = read_marker(tmp_path)
        assert marker is not None, "a marker is written"
        assert marker.data_root_id == root_id, "the marker carries the stamped root id"

    def test_refuses_to_overwrite_an_existing_marker_even_when_forced(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id=str(_ROOT_A))

        with pytest.raises(LakeRootIdentityError, match="already"):
            stamp_existing_root(tmp_path, _ROOT_B, force=True)


class TestInspectRoot:
    def test_returns_none_for_an_unmarked_root_without_mutating_it(self, tmp_path: Path):
        assert inspect_root(tmp_path) is None
        assert read_marker(tmp_path) is None

    def test_returns_the_marker_without_requiring_an_expected_uuid(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id=str(_ROOT_A))

        marker = inspect_root(tmp_path)

        assert marker is not None
        assert marker.data_root_id == _ROOT_A

    def test_raises_on_a_malformed_marker(self, tmp_path: Path):
        path = marker_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")

        with pytest.raises(LakeRootIdentityError):
            inspect_root(tmp_path)


class TestActiveRootId:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [("", LEGACY_ROOT_ID), (str(_ROOT_A), _ROOT_A)],
        ids=["falls_back_to_the_legacy_root_id_when_unconfigured", "reads_the_configured_root_id"],
    )
    def test_active_root_id(self, monkeypatch: pytest.MonkeyPatch, configured: str, expected: UUID):
        monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", configured)
        assert active_root_id() == expected


class TestLegacyRootId:
    def test_is_the_nil_uuid(self):
        """The deterministic legacy-root UUID the schema migration backfills
        every pre-#1876 row with (issue #1876)."""
        assert UUID("00000000-0000-0000-0000-000000000000") == LEGACY_ROOT_ID
