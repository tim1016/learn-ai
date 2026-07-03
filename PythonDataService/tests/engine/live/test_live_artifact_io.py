"""Tests for canonical live artifact file-or-directory readers."""

from __future__ import annotations

import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.engine.live.live_artifact_io import (
    LiveArtifactReadError,
    artifact_exists,
    artifact_sha256,
    list_run_artifacts,
    parquet_row_count,
    read_parquet_tail,
)


def _write_dataset(path: Path, values: list[str]) -> None:
    path.mkdir()
    pq.write_table(pa.table({"signal": values[:1]}), path / "part-000001.parquet")
    if len(values) > 1:
        pq.write_table(pa.table({"signal": values[1:]}), path / "part-000002.parquet")


def test_parquet_helpers_read_file_and_dataset_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "single.parquet"
    pq.write_table(pa.table({"signal": ["HOLD", "ENTER"]}), file_path)
    dataset_path = tmp_path / "decisions.parquet"
    _write_dataset(dataset_path, ["HOLD", "ENTER", "EXIT"])

    assert artifact_exists(file_path)
    assert artifact_exists(dataset_path)
    assert parquet_row_count(file_path) == 2
    assert parquet_row_count(dataset_path) == 3
    assert [row["signal"] for row in read_parquet_tail(dataset_path, 2)] == ["ENTER", "EXIT"]


def test_hash_and_listing_cover_dataset_directories(tmp_path: Path) -> None:
    dataset_path = tmp_path / "executions.parquet"
    _write_dataset(dataset_path, ["BUY", "SELL"])
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "pending.json").write_text("{}", encoding="utf-8")
    (tmp_path / "reconcile").mkdir()
    (tmp_path / "reconcile" / "day-0.md").write_text("# reconcile", encoding="utf-8")

    digest = artifact_sha256(dataset_path)
    listed = {artifact.name: artifact for artifact in list_run_artifacts(tmp_path)}

    assert len(digest) == 64
    assert listed["executions.parquet"].row_count == 2
    assert listed["executions.parquet"].size_bytes > 0
    assert "commands" not in listed
    assert "reconcile" not in listed


def test_corrupt_parquet_raises_by_default_and_logs_in_warn_empty_mode(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "executions.parquet"
    path.write_text("not parquet", encoding="utf-8")

    with pytest.raises(LiveArtifactReadError):
        parquet_row_count(path)

    with caplog.at_level(logging.WARNING):
        assert parquet_row_count(path, on_error="warn_empty") == 0

    assert "live artifact unreadable" in caplog.text
    assert "executions.parquet" in caplog.text


def test_tail_reads_latest_dataset_segment_without_scanning_earlier_segments(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "decisions.parquet"
    dataset_path.mkdir()
    (dataset_path / "part-000001.parquet").write_text("not parquet", encoding="utf-8")
    pq.write_table(
        pa.table({"signal": ["ENTER", "EXIT"]}),
        dataset_path / "part-000002.parquet",
    )

    assert [row["signal"] for row in read_parquet_tail(dataset_path, 1)] == ["EXIT"]
