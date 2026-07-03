"""Canonical I/O helpers for live-run evidence artifacts.

Live parquet artifacts are addressed by stable names such as
``decisions.parquet``. Newer writers publish those paths as parquet dataset
directories, while older runs may still have a single parquet file at the same
path. This module owns that file-or-directory contract for readers, hash
manifest writers, and pre-flight verification.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

LiveArtifactErrorPolicy = Literal["raise", "warn_empty"]


@dataclass(frozen=True)
class LiveArtifactMetadata:
    name: str
    size_bytes: int
    mtime_ms: int
    row_count: int | None = None


@dataclass(frozen=True)
class LiveArtifactReadError(Exception):
    path: Path
    operation: str
    cause: Exception

    def __str__(self) -> str:
        return f"Could not {self.operation} live artifact {self.path}: {self.cause}"


def artifact_exists(path: Path) -> bool:
    """Return true when a stable artifact path exists as a file or directory."""

    return path.is_file() or path.is_dir()


def artifact_mtime_signature(path: Path) -> object:
    """Return a cache signature that changes when an artifact changes."""

    try:
        if path.is_file():
            stat = path.stat()
            return (stat.st_mtime, stat.st_size)
        if path.is_dir():
            entries: list[tuple[str, float, int]] = []
            for child in sorted(p for p in path.rglob("*") if p.is_file()):
                stat = child.stat()
                entries.append((child.relative_to(path).as_posix(), stat.st_mtime, stat.st_size))
            return tuple(entries)
    except OSError:
        return ()
    return ()


def artifact_size_bytes(path: Path) -> int:
    """Return total bytes for a file artifact or all files in a directory artifact."""

    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
    except OSError:
        return 0
    return 0


def artifact_mtime_ms(path: Path) -> int:
    """Return the artifact's newest file mtime in ms UTC."""

    best = 0.0
    try:
        if path.is_file():
            best = path.stat().st_mtime
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    best = max(best, child.stat().st_mtime)
    except OSError:
        return 0
    return int(best * 1000)


def parquet_row_count(
    path: Path,
    *,
    on_error: LiveArtifactErrorPolicy = "raise",
) -> int:
    """Return row count for a parquet file or dataset directory."""

    if not artifact_exists(path):
        return 0
    try:
        return sum(_parquet_file_row_count(file_path) for file_path in _parquet_files(path))
    except (FileNotFoundError, OSError, pa.ArrowException) as exc:
        return _handle_artifact_read_error(
            path=path,
            operation="count rows in",
            exc=exc,
            on_error=on_error,
            empty_value=0,
        )


def read_parquet_rows(
    path: Path,
    *,
    on_error: LiveArtifactErrorPolicy = "raise",
) -> list[dict[str, Any]]:
    """Read all rows from a parquet file or dataset directory."""

    if not artifact_exists(path):
        return []
    try:
        rows: list[dict[str, Any]] = []
        for file_path in _parquet_files(path):
            rows.extend(pq.read_table(file_path).to_pylist())
        return rows
    except (FileNotFoundError, OSError, pa.ArrowException) as exc:
        return _handle_artifact_read_error(
            path=path,
            operation="read rows from",
            exc=exc,
            on_error=on_error,
            empty_value=[],
        )


def read_parquet_tail(
    path: Path,
    n: int,
    *,
    on_error: LiveArtifactErrorPolicy = "raise",
) -> list[dict[str, Any]]:
    """Read the last ``n`` rows from a parquet file or dataset directory."""

    if n <= 0:
        return []
    if not artifact_exists(path):
        return []
    try:
        remaining = n
        chunks: list[list[dict[str, Any]]] = []
        for file_path in reversed(_parquet_files(path)):
            row_count = _parquet_file_row_count(file_path)
            if row_count <= 0:
                continue
            take = min(remaining, row_count)
            start = row_count - take
            rows = pq.read_table(file_path).slice(start, take).to_pylist()
            chunks.append(rows)
            remaining -= take
            if remaining == 0:
                break
        return [row for chunk in reversed(chunks) for row in chunk]
    except (FileNotFoundError, OSError, pa.ArrowException) as exc:
        return _handle_artifact_read_error(
            path=path,
            operation="read tail from",
            exc=exc,
            on_error=on_error,
            empty_value=[],
        )


def artifact_sha256(path: Path) -> str:
    """Return a stable lowercase SHA-256 for a file or artifact directory."""

    if path.is_dir():
        return _directory_sha256(path)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def list_run_artifacts(run_dir: Path) -> list[LiveArtifactMetadata]:
    """List immediate file artifacts and known parquet dataset directories."""

    artifacts: list[LiveArtifactMetadata] = []
    try:
        entries = sorted(run_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return artifacts
    for path in entries:
        if not _is_listable_artifact(path):
            continue
        row_count: int | None = None
        if path.suffix == ".parquet":
            row_count = parquet_row_count(path, on_error="warn_empty")
        artifacts.append(
            LiveArtifactMetadata(
                name=path.name,
                size_bytes=artifact_size_bytes(path),
                mtime_ms=artifact_mtime_ms(path),
                row_count=row_count,
            )
        )
    return artifacts


def _handle_artifact_read_error[T](
    *,
    path: Path,
    operation: str,
    exc: Exception,
    on_error: LiveArtifactErrorPolicy,
    empty_value: T,
) -> T:
    error = LiveArtifactReadError(path=path, operation=operation, cause=exc)
    if on_error == "warn_empty":
        logger.warning(
            "live artifact unreadable: %s",
            error,
            extra={"path": str(path), "error": str(error)},
        )
        return empty_value
    raise error from exc


def _is_listable_artifact(path: Path) -> bool:
    return path.is_file() or (path.is_dir() and path.suffix == ".parquet")


def _parquet_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*.parquet") if p.is_file())
    return []


def _parquet_file_row_count(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def _directory_sha256(path: Path) -> str:
    h = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        with child.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


__all__ = [
    "LiveArtifactMetadata",
    "LiveArtifactReadError",
    "artifact_exists",
    "artifact_mtime_ms",
    "artifact_mtime_signature",
    "artifact_sha256",
    "artifact_size_bytes",
    "list_run_artifacts",
    "parquet_row_count",
    "read_parquet_rows",
    "read_parquet_tail",
]
