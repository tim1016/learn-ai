"""Regression coverage for blocking and non-blocking advisory lock failures."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.utils.advisory_lock import advisory_file_lock, try_advisory_file_lock


def _unavailable_fcntl() -> SimpleNamespace:
    def flock(_fd: int, _flags: int) -> None:
        raise OSError("filesystem does not provide advisory locks")

    return SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=flock)


def test_try_lock_treats_any_posix_lock_os_error_as_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "fcntl", _unavailable_fcntl())

    with try_advisory_file_lock(tmp_path / "attempt") as acquired:
        assert acquired is False


def test_blocking_lock_propagates_a_posix_lock_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "fcntl", _unavailable_fcntl())

    with pytest.raises(OSError, match="does not provide advisory locks"), advisory_file_lock(
        tmp_path / "attempt"
    ):
        pass
