"""Failure-injection coverage for the shared durable append primitive."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.engine.live.durable_append_log as durable_append_log


def test_append_jsonl_record_fsyncs_row_and_parent_before_returning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    fsynced_fds: list[int] = []

    monkeypatch.setattr(durable_append_log.os, "fsync", fsynced_fds.append)

    durable_append_log.append_jsonl_record(path, '{"seq":1}', trusted_root=tmp_path)

    assert path.read_text(encoding="utf-8") == '{"seq":1}\n'
    assert len(fsynced_fds) == 2


def test_append_jsonl_record_rejects_multirow_input_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"

    with pytest.raises(ValueError, match="exactly one JSONL row"):
        durable_append_log.append_jsonl_record(
            path,
            '{"seq":1}\n{"seq":2}',
            trusted_root=tmp_path,
        )

    assert not path.exists()


def test_append_jsonl_record_rejects_a_path_outside_its_trusted_root(tmp_path: Path) -> None:
    path = tmp_path.parent / "outside.jsonl"

    with pytest.raises(ValueError, match="escapes root"):
        durable_append_log.append_jsonl_record(
            path,
            '{"seq":1}',
            trusted_root=tmp_path,
        )

    assert not path.exists()


def test_append_jsonl_record_rejects_an_account_leaf_symlink_escape(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    account_root = accounts_root / "DU123456"
    outside_root = tmp_path / "outside"
    account_root.mkdir(parents=True)
    outside_root.mkdir()
    account_root.rmdir()
    try:
        account_root.symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(ValueError, match="escapes root"):
        durable_append_log.append_jsonl_record(
            account_root / "ledger.jsonl",
            '{"seq":1}',
            trusted_root=accounts_root,
        )

    assert not (outside_root / "ledger.jsonl").exists()


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


def test_rewrite_jsonl_records_fsyncs_temp_before_replace_and_parent_after(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.jsonl"
    path.write_text('{"seq":1}\n', encoding="utf-8")
    events: list[str] = []
    original_replace = durable_append_log.os.replace

    def record_fsync(_file_descriptor: int) -> None:
        events.append("fsync")

    def record_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        source_fd = durable_append_log.os.open(source, durable_append_log.os.O_RDONLY, dir_fd=src_dir_fd)
        with durable_append_log.os.fdopen(source_fd, "r", encoding="utf-8") as source_file:
            assert source_file.read() == '{"seq":2}\n'
        assert destination == path.name
        assert src_dir_fd == dst_dir_fd
        events.append("replace")
        original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(durable_append_log.os, "fsync", record_fsync)
    monkeypatch.setattr(durable_append_log.os, "replace", record_replace)

    durable_append_log.rewrite_jsonl_records(
        path,
        ('{"seq":2}',),
        trusted_root=tmp_path,
    )

    assert events == ["fsync", "replace", "fsync"]
    assert path.read_text(encoding="utf-8") == '{"seq":2}\n'


def test_rewrite_jsonl_records_removes_temporary_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.jsonl"
    path.write_text('{"seq":1}\n', encoding="utf-8")

    def fail_replace(_source: str, _destination: str, **_kwargs: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(durable_append_log.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        durable_append_log.rewrite_jsonl_records(
            path,
            ('{"seq":2}',),
            trusted_root=tmp_path,
        )

    assert path.read_text(encoding="utf-8") == '{"seq":1}\n'
    assert list(tmp_path.glob(".inbox.jsonl.*.tmp")) == []


def test_rewrite_jsonl_records_ignores_a_predictable_legacy_temp_symlink(tmp_path: Path) -> None:
    path = tmp_path / "inbox.jsonl"
    path.write_text('{"seq":1}\n', encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside evidence", encoding="utf-8")
    predictable_temp = tmp_path / f".{path.name}.{durable_append_log.os.getpid()}.tmp"
    try:
        predictable_temp.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    durable_append_log.rewrite_jsonl_records(
        path,
        ('{"seq":2}',),
        trusted_root=tmp_path,
    )

    assert path.read_text(encoding="utf-8") == '{"seq":2}\n'
    assert outside.read_text(encoding="utf-8") == "outside evidence"


def test_append_rejects_an_intermediate_directory_swap_after_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    accounts_root = tmp_path / "accounts"
    account_root = accounts_root / "DU123456"
    outside_root = tmp_path / "outside"
    account_root.mkdir(parents=True)
    outside_root.mkdir()
    path = account_root / "ledger.jsonl"
    original_open = durable_append_log.os.open
    swapped = False

    def swap_then_open(
        file: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if file == account_root.name and dir_fd is not None and not swapped:
            swapped = True
            account_root.rmdir()
            account_root.symlink_to(outside_root, target_is_directory=True)
        return original_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(durable_append_log.os, "open", swap_then_open)

    with pytest.raises(OSError):
        durable_append_log.append_jsonl_record(path, '{"seq":1}', trusted_root=accounts_root)

    assert swapped
    assert not (outside_root / "ledger.jsonl").exists()


def test_service_owned_filesystem_fallback_writes_in_root_and_rejects_leaf_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(durable_append_log, "_supports_descriptor_relative_writes", lambda: False)
    safe_path = tmp_path / "safe" / "ledger.jsonl"

    durable_append_log.append_jsonl_record(safe_path, '{"seq":1}', trusted_root=tmp_path)

    assert safe_path.read_text(encoding="utf-8") == '{"seq":1}\n'
    accounts_root = tmp_path / "accounts"
    account_root = accounts_root / "DU123456"
    outside_root = tmp_path / "outside"
    account_root.mkdir(parents=True)
    outside_root.mkdir()
    account_root.rmdir()
    try:
        account_root.symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(ValueError, match="escapes root"):
        durable_append_log.append_jsonl_record(
            account_root / "ledger.jsonl",
            '{"seq":1}',
            trusted_root=accounts_root,
        )

    assert not (outside_root / "ledger.jsonl").exists()
