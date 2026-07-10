"""Shared crash-atomic Parquet publication primitive."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def atomic_parquet_write(
    path: Path,
    write_parquet: Callable[[Path], None],
    *,
    replace: Callable[[Path, Path], None] = os.replace,
    temp_dir: Path | None = None,
    temp_prefix: str | None = None,
    cleanup_errors: tuple[type[BaseException], ...] = (OSError,),
) -> None:
    """Write a Parquet temp file, fsync it, atomically replace, then fsync parent."""

    directory = temp_dir or path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=temp_prefix or f".{path.name}.",
        suffix=".tmp",
        dir=str(directory),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        write_parquet(tmp_path)
        fsync_file(tmp_path)
        replace(tmp_path, path)
        fsync_parent_dir(path)
    except Exception:
        with contextlib.suppress(*cleanup_errors):
            tmp_path.unlink()
        raise


def fsync_parent_dir(child_path: Path) -> None:
    dir_fd = os.open(str(child_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def fsync_file(path: Path) -> None:
    with path.open("rb") as fh:
        os.fsync(fh.fileno())
