"""Unit tests for app.data_lake.atomic.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.2
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from app.data_lake.atomic import (
    AtomicRenameUnsafeError,
    assert_same_filesystem,
    atomic_write_and_promote,
    stage_path_for,
)


class TestAssertSameFilesystem:
    def test_same_directory_passes(self, tmp_path: Path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        # No exception.
        assert_same_filesystem(a, b)

    def test_missing_directory_raises_FileNotFoundError(self, tmp_path: Path):
        a = tmp_path / "a"
        b = tmp_path / "does-not-exist"
        a.mkdir()
        with pytest.raises(FileNotFoundError):
            assert_same_filesystem(a, b)


class TestStagePathFor:
    def test_layout(self, tmp_path: Path):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        result = stage_path_for(
            staging_root=tmp_path / "staging",
            rel_lake_path=rel,
            request_id=request_id,
            worker_id="worker-1",
            attempt=1,
        )
        assert result == (
            tmp_path
            / "staging"
            / "12345678-1234-5678-1234-567812345678"
            / "worker-1"
            / "attempt_1"
            / "equity"
            / "usa"
            / "minute"
            / "spy"
            / "20240520_trade.zip.tmp"
        )

    def test_two_attempts_distinct(self, tmp_path: Path):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        a1 = stage_path_for(tmp_path / "staging", rel, request_id, "w", 1)
        a2 = stage_path_for(tmp_path / "staging", rel, request_id, "w", 2)
        assert a1 != a2


class TestAtomicWriteAndPromote:
    def test_writes_bytes_and_returns_sha256(self, tmp_path: Path):
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()

        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        content = b"hello world deci-cent payload"
        expected_sha = hashlib.sha256(content).hexdigest()

        result_sha = atomic_write_and_promote(
            content=content,
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel,
            request_id=UUID("12345678-1234-5678-1234-567812345678"),
            worker_id="w",
            attempt=1,
        )

        assert result_sha == expected_sha
        final = lake_root / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip"
        assert final.is_file()
        assert final.read_bytes() == content

    def test_no_staging_leftover_after_promote(self, tmp_path: Path):
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")

        atomic_write_and_promote(
            content=b"x",
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel,
            request_id=UUID("12345678-1234-5678-1234-567812345678"),
            worker_id="w",
            attempt=1,
        )

        # The .tmp staging file should be gone (rename moved it).
        staged = stage_path_for(
            staging_root,
            rel,
            UUID("12345678-1234-5678-1234-567812345678"),
            "w",
            1,
        )
        assert not staged.exists()

    def test_cross_device_raises(self, tmp_path: Path, monkeypatch):
        """If lake_root and staging_root are on different st_dev values,
        atomic_write_and_promote refuses to proceed."""
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        rel = PurePosixPath("a.zip")

        # Force assert_same_filesystem to disagree.
        from app.data_lake import atomic as atomic_module

        def fake_assert(a: Path, b: Path) -> None:
            raise AtomicRenameUnsafeError(f"different filesystems: {a} vs {b}")

        monkeypatch.setattr(atomic_module, "assert_same_filesystem", fake_assert)
        with pytest.raises(AtomicRenameUnsafeError):
            atomic_write_and_promote(
                content=b"x",
                lake_root=lake_root,
                staging_root=staging_root,
                rel_lake_path=rel,
                request_id=UUID("12345678-1234-5678-1234-567812345678"),
                worker_id="w",
                attempt=1,
            )

    def test_rejects_absolute_path(self, tmp_path: Path):
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        with pytest.raises(ValueError, match="absolute"):
            atomic_write_and_promote(
                content=b"x",
                lake_root=lake_root,
                staging_root=staging_root,
                rel_lake_path=PurePosixPath("/tmp/x.zip"),
                request_id=UUID("12345678-1234-5678-1234-567812345678"),
                worker_id="w",
                attempt=1,
            )

    def test_rejects_dotdot_traversal(self, tmp_path: Path):
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        with pytest.raises(ValueError, match=r"\.\."):
            atomic_write_and_promote(
                content=b"x",
                lake_root=lake_root,
                staging_root=staging_root,
                rel_lake_path=PurePosixPath("equity/../../etc/passwd"),
                request_id=UUID("12345678-1234-5678-1234-567812345678"),
                worker_id="w",
                attempt=1,
            )

    def test_rejects_empty_path(self, tmp_path: Path):
        """PurePosixPath('') has no parts; Path(*[]) resolves to CWD, which
        would overwrite lake_root itself if not rejected."""
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        with pytest.raises(ValueError, match="empty"):
            atomic_write_and_promote(
                content=b"x",
                lake_root=lake_root,
                staging_root=staging_root,
                rel_lake_path=PurePosixPath(""),
                request_id=UUID("12345678-1234-5678-1234-567812345678"),
                worker_id="w",
                attempt=1,
            )
