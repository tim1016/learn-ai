"""Tests for canonical live artifact file-or-directory readers."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.engine.live.live_artifact_io import (
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

    digest = artifact_sha256(dataset_path)
    listed = {artifact.name: artifact for artifact in list_run_artifacts(tmp_path)}

    assert len(digest) == 64
    assert listed["executions.parquet"].row_count == 2
    assert listed["executions.parquet"].size_bytes > 0
