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
    LakeRootIdentityError,
    RootContext,
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

    def test_raises_on_malformed_json(self, tmp_path: Path):
        path = marker_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")

        with pytest.raises(LakeRootIdentityError, match="malformed"):
            read_marker(tmp_path)

    def test_raises_on_wrong_schema_version(self, tmp_path: Path):
        _write_marker(tmp_path, schema_version=2, data_root_id=str(_ROOT_A))

        with pytest.raises(LakeRootIdentityError, match="schema_version"):
            read_marker(tmp_path)

    def test_raises_on_invalid_uuid(self, tmp_path: Path):
        _write_marker(tmp_path, data_root_id="not-a-uuid")

        with pytest.raises(LakeRootIdentityError):
            read_marker(tmp_path)

    def test_raises_on_extra_fields(self, tmp_path: Path):
        """No timestamps, no host-specific paths — the marker carries exactly
        schema_version and data_root_id, nothing else (issue #1876)."""
        path = marker_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": 1, "data_root_id": str(_ROOT_A), "created_at": "2026-01-01"})
        )

        with pytest.raises(LakeRootIdentityError):
            read_marker(tmp_path)


class TestResolveRootContext:
    def test_missing_marker_raises_before_anything_is_mutated(self, tmp_path: Path):
        with pytest.raises(LakeRootIdentityError, match="missing"):
            resolve_root_context(_ROOT_A, base_root=tmp_path)

        assert not marker_path(tmp_path).exists()

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

    def test_never_silently_creates_a_marker(self, tmp_path: Path):
        """Normal application startup validates, never creates (issue #1876)."""
        with pytest.raises(LakeRootIdentityError):
            resolve_root_context(_ROOT_A, base_root=tmp_path)

        assert read_marker(tmp_path) is None


class TestRootContextPathResolution:
    def test_lake_container_matches_path_policy(self, tmp_path: Path):
        ctx = RootContext(root_id=_ROOT_A, base_root=tmp_path)
        assert ctx.lake_container() == lake_container_within(tmp_path)

    def test_lake_root_matches_path_policy(self, tmp_path: Path):
        ctx = RootContext(root_id=_ROOT_A, base_root=tmp_path)
        assert ctx.lake_root("raw") == lake_root_within(tmp_path, "raw")
        assert ctx.lake_root("polygon_split_adjusted") == lake_root_within(tmp_path, "polygon_split_adjusted")

    def test_staging_root_matches_path_policy(self, tmp_path: Path):
        ctx = RootContext(root_id=_ROOT_A, base_root=tmp_path)
        assert ctx.staging_root("raw") == staging_root_within(tmp_path, "raw")


class TestInitEmptyRoot:
    def test_creates_marker_at_the_expected_path(self, tmp_path: Path):
        ctx = init_empty_root(tmp_path, _ROOT_A)

        assert ctx.root_id == _ROOT_A
        marker = read_marker(tmp_path)
        assert marker is not None
        assert marker.data_root_id == _ROOT_A

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

    def test_creation_is_atomic_no_partial_marker_left_on_disk(self, tmp_path: Path):
        """The marker file, once present, is always fully-formed valid JSON —
        never a truncated partial write an interrupted process could leave."""
        ctx = init_empty_root(tmp_path, _ROOT_A)
        raw = marker_path(tmp_path).read_text()
        parsed = json.loads(raw)  # would raise on a torn/partial write
        assert parsed == {"schema_version": 1, "data_root_id": str(_ROOT_A)}
        assert ctx.root_id == _ROOT_A


class TestStampExistingRoot:
    def test_requires_the_explicit_force_flag(self, tmp_path: Path):
        container = lake_container_within(tmp_path)
        container.mkdir(parents=True)
        (container / "raw").mkdir()

        with pytest.raises(LakeRootIdentityError, match="force"):
            stamp_existing_root(tmp_path, _ROOT_A, force=False)

        assert read_marker(tmp_path) is None

    def test_stamps_a_populated_root_when_forced(self, tmp_path: Path):
        container = lake_container_within(tmp_path)
        container.mkdir(parents=True)
        (container / "raw").mkdir()

        ctx = stamp_existing_root(tmp_path, _ROOT_A, force=True)

        assert ctx.root_id == _ROOT_A
        marker = read_marker(tmp_path)
        assert marker is not None
        assert marker.data_root_id == _ROOT_A

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
    def test_falls_back_to_the_legacy_root_id_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
        assert active_root_id() == LEGACY_ROOT_ID

    def test_reads_the_configured_root_id(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(_ROOT_A))
        assert active_root_id() == _ROOT_A


class TestLegacyRootId:
    def test_is_the_nil_uuid(self):
        """The deterministic legacy-root UUID the schema migration backfills
        every pre-#1876 row with (issue #1876)."""
        assert UUID("00000000-0000-0000-0000-000000000000") == LEGACY_ROOT_ID
