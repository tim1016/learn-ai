"""Failure-injection coverage for the shared durable append primitive."""

from __future__ import annotations

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
