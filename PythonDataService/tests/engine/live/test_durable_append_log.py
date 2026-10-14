"""Failure-injection coverage for the shared durable append primitive."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest

import app.engine.live.durable_append_log as durable_append_log


def test_create_exclusive_durable_file_fsyncs_new_claim_and_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "records" / "claim.json"
    fsynced_fds: list[int] = []

    monkeypatch.setattr(durable_append_log.os, "fsync", fsynced_fds.append)

    durable_append_log.create_exclusive_durable_file(
        path,
        '{"state":"PENDING"}',
        trusted_root=tmp_path,
    )

    assert path.read_text(encoding="utf-8") == '{"state":"PENDING"}'
    assert len(fsynced_fds) == 2
    with pytest.raises(FileExistsError):
        durable_append_log.create_exclusive_durable_file(
            path,
            '{"state":"PENDING"}',
            trusted_root=tmp_path,
        )


def _fail_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_fd: int) -> None:
        raise OSError("the disk refused the sync")

    monkeypatch.setattr(durable_append_log.os, "fsync", _boom)


def test_a_claim_whose_sync_failed_stays_on_disk_so_a_retry_cannot_repeat_the_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "claim.json"
    with monkeypatch.context() as failing:
        _fail_fsync(failing)
        with pytest.raises(OSError, match="refused the sync"):
            durable_append_log.create_exclusive_durable_file(path, '{"state":"PENDING"}', trusted_root=tmp_path)

    assert path.exists()
    with pytest.raises(FileExistsError):
        durable_append_log.create_exclusive_durable_file(path, '{"state":"PENDING"}', trusted_root=tmp_path)


def test_an_atomic_publish_whose_sync_failed_leaves_neither_a_record_nor_a_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipts" / "receipt.json"
    with monkeypatch.context() as failing:
        _fail_fsync(failing)
        with pytest.raises(OSError, match="refused the sync"):
            durable_append_log.create_atomic_exclusive_durable_file(path, '{"state":"DONE"}', trusted_root=tmp_path)

    assert not path.exists()
    assert list(path.parent.iterdir()) == []


def test_an_atomic_publish_refuses_a_name_another_writer_owns_and_keeps_its_record(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = root / "receipt.json"
    durable_append_log.create_atomic_exclusive_durable_file(path, '{"writer":"first"}', trusted_root=root)

    with pytest.raises(FileExistsError):
        durable_append_log.create_atomic_exclusive_durable_file(path, '{"writer":"second"}', trusted_root=root)

    assert path.read_text(encoding="utf-8") == '{"writer":"first"}'
    assert [entry.name for entry in root.iterdir()] == ["receipt.json"]


@pytest.mark.parametrize(
    "create",
    [durable_append_log.create_exclusive_durable_file, durable_append_log.create_atomic_exclusive_durable_file],
)
def test_a_path_outside_the_trusted_root_and_a_nul_byte_are_refused(
    create: Callable[..., None], tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="escapes root"):
        create(tmp_path / "elsewhere.json", "{}", trusted_root=root)
    with pytest.raises(ValueError, match="NUL"):
        create(root / "claim.json", "{\x00}", trusted_root=root)

    assert list(tmp_path.glob("elsewhere.json")) == []


@pytest.mark.parametrize(
    "create",
    [durable_append_log.create_exclusive_durable_file, durable_append_log.create_atomic_exclusive_durable_file],
)
def test_a_directory_swapped_for_a_symlink_after_the_path_was_proven_is_not_followed(
    create: Callable[..., None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The proof ran on a path that named a real directory; by the write it names a link out of the root."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "ledger").symlink_to(outside, target_is_directory=True)
    with monkeypatch.context() as swapped:
        # The swap won the race: the proof's ``realpath`` still saw the path as it was before the link.
        swapped.setattr(durable_append_log.os.path, "realpath", lambda path, **_kw: os.path.abspath(path))

        with pytest.raises(OSError) as refused:
            create(root / "ledger" / "claim.json", "{}", trusted_root=root)

    assert refused.value.errno in {errno.ELOOP, errno.ENOTDIR}  # which one is the platform's; either is the refusal
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "create",
    [durable_append_log.create_exclusive_durable_file, durable_append_log.create_atomic_exclusive_durable_file],
)
def test_the_service_owned_root_fallback_creates_once_and_refuses_an_escape(
    create: Callable[..., None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(durable_append_log, "_supports_descriptor_relative_writes", lambda: False)
    root = tmp_path / "root"
    path = root / "nested" / "claim.json"

    create(path, '{"state":"PENDING"}', trusted_root=root)

    assert path.read_text(encoding="utf-8") == '{"state":"PENDING"}'
    with pytest.raises(FileExistsError):
        create(path, '{"state":"PENDING"}', trusted_root=root)
    with pytest.raises(ValueError, match="escapes root"):
        create(tmp_path / "elsewhere.json", "{}", trusted_root=root)
