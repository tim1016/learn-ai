"""Behavior tests for crash-atomic file publication."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from app.utils.atomic_file import atomic_write_bytes


def test_atomic_write_bytes_preserves_existing_destination_mode(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_bytes(b"old report")
    path.chmod(0o640)

    atomic_write_bytes(path, b"new report")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_atomic_write_bytes_applies_umask_to_new_destination(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    previous_umask = os.umask(0o027)
    try:
        atomic_write_bytes(path, b"new report")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_atomic_write_bytes_applies_explicit_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "activation-plan.json"
    path.write_bytes(b"old plan")
    path.chmod(0o666)

    atomic_write_bytes(path, b"new plan", mode=0o600)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
