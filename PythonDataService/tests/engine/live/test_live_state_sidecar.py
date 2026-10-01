"""Tests for the durable-write primitives kept in ``live_state_sidecar``.

``fsync_parent_dir`` makes a fresh rename survive a crash, and ``_file_lock``
serialises writers that share one temp-then-replace path.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from app.engine.live.live_state_sidecar import _file_lock, fsync_parent_dir


@pytest.mark.skipif(sys.platform == "win32", reason="parent-dir fsync is POSIX-only")
def test_write_fsyncs_parent_directory_for_rename_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync of a tempfile flushes its contents to disk, but on POSIX the
    rename's directory entry can still be lost on power loss unless the
    parent directory is also fsynced.
    """
    child = tmp_path / "state.json"
    child.write_text("{}", encoding="utf-8")
    fsynced_inodes: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        fsynced_inodes.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    fsync_parent_dir(child)

    assert fsynced_inodes == [tmp_path.stat().st_ino]


def test_concurrent_writers_serialize_without_error(tmp_path: Path) -> None:
    """Two threads writing the same file through one shared tempfile name must
    not race. Without serialisation the loser's ``os.replace`` finds the
    tempfile already renamed by the winner and raises ``FileNotFoundError``.
    With the advisory lock, writes take turns and the final content is one of
    the two writers'.
    """
    path = tmp_path / "state.json"
    tmp = path.with_suffix(".tmp")
    errors: list[BaseException] = []

    def writer(label: str) -> None:
        try:
            for _ in range(50):
                with _file_lock(path):
                    tmp.write_text(label, encoding="utf-8")
                    os.replace(tmp, path)
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("alpha",))
    t2 = threading.Thread(target=writer, args=("beta",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"concurrent writers raised: {errors!r}"
    assert path.read_text(encoding="utf-8") in {"alpha", "beta"}
