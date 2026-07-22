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
    parent_syncs: list[Path] = []

    monkeypatch.setattr(durable_append_log.os, "fsync", fsynced_fds.append)
    monkeypatch.setattr(durable_append_log, "_fsync_parent_dir", parent_syncs.append)

    durable_append_log.append_jsonl_record(path, '{"seq":1}', trusted_root=tmp_path)

    assert path.read_text(encoding="utf-8") == '{"seq":1}\n'
    assert len(fsynced_fds) == 1
    assert parent_syncs == [path]


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
    parent_syncs: list[Path] = []

    monkeypatch.setattr(durable_append_log.os, "fsync", fsynced_fds.append)
    monkeypatch.setattr(durable_append_log, "_fsync_parent_dir", parent_syncs.append)

    durable_append_log.create_exclusive_durable_file(
        path,
        '{"state":"PENDING"}',
        trusted_root=tmp_path,
    )

    assert path.read_text(encoding="utf-8") == '{"state":"PENDING"}'
    assert len(fsynced_fds) == 1
    assert parent_syncs == [path]
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
        events.append("file_fsync")

    def record_replace(source: Path, destination: Path) -> None:
        assert source.read_text(encoding="utf-8") == '{"seq":2}\n'
        assert destination == path
        events.append("replace")
        original_replace(source, destination)

    def record_parent_sync(child_path: Path) -> None:
        assert child_path == path
        assert path.read_text(encoding="utf-8") == '{"seq":2}\n'
        events.append("parent_fsync")

    monkeypatch.setattr(durable_append_log.os, "fsync", record_fsync)
    monkeypatch.setattr(durable_append_log.os, "replace", record_replace)
    monkeypatch.setattr(durable_append_log, "_fsync_parent_dir", record_parent_sync)

    durable_append_log.rewrite_jsonl_records(
        path,
        ('{"seq":2}',),
        trusted_root=tmp_path,
    )

    assert events == ["file_fsync", "replace", "parent_fsync"]
    assert path.read_text(encoding="utf-8") == '{"seq":2}\n'


def test_rewrite_jsonl_records_removes_temporary_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.jsonl"
    path.write_text('{"seq":1}\n', encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
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
