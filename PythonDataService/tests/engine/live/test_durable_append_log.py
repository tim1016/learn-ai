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

    durable_append_log.append_jsonl_record(path, '{"seq":1}')

    assert path.read_text(encoding="utf-8") == '{"seq":1}\n'
    assert len(fsynced_fds) == 1
    assert parent_syncs == [path]


def test_append_jsonl_record_rejects_multirow_input_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"

    with pytest.raises(ValueError, match="exactly one JSONL row"):
        durable_append_log.append_jsonl_record(path, '{"seq":1}\n{"seq":2}')

    assert not path.exists()


def test_create_exclusive_durable_file_fsyncs_new_claim_and_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "records" / "claim.json"
    fsynced_fds: list[int] = []
    parent_syncs: list[Path] = []

    monkeypatch.setattr(durable_append_log.os, "fsync", fsynced_fds.append)
    monkeypatch.setattr(durable_append_log, "_fsync_parent_dir", parent_syncs.append)

    durable_append_log.create_exclusive_durable_file(path, '{"state":"PENDING"}')

    assert path.read_text(encoding="utf-8") == '{"state":"PENDING"}'
    assert len(fsynced_fds) == 1
    assert parent_syncs == [path]
    with pytest.raises(FileExistsError):
        durable_append_log.create_exclusive_durable_file(path, '{"state":"PENDING"}')


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

    durable_append_log.rewrite_jsonl_records(path, ('{"seq":2}',))

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
        durable_append_log.rewrite_jsonl_records(path, ('{"seq":2}',))

    assert path.read_text(encoding="utf-8") == '{"seq":1}\n'
    assert list(tmp_path.glob(".inbox.jsonl.*.tmp")) == []
