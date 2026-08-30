"""CLI wrapper over app.data_lake.root_identity's administrative functions.

Issue #1876: "An administrative command exists for init / stamp-with-
explicit-flag / inspect." These tests drive the parsed argparse actions
directly (same style as test_migrate_lake_to_mode_roots.py) rather than
shelling out to main(), so failures point at the actual function.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app.data_lake.root_identity import LakeRootIdentityError, init_empty_root, read_marker
from scripts.manage_data_root import run_init, run_inspect, run_stamp

_ROOT_A = UUID("11111111-1111-1111-1111-111111111111")


def test_run_init_creates_a_marker_for_an_empty_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_init(tmp_path, _ROOT_A)

    marker = read_marker(tmp_path)
    assert marker is not None
    assert marker.data_root_id == _ROOT_A
    assert str(_ROOT_A) in capsys.readouterr().out


def test_run_init_refuses_a_populated_root(tmp_path: Path) -> None:
    container = tmp_path / "lake"
    (container / "raw").mkdir(parents=True)
    (container / "raw" / "artifact.zip").write_bytes(b"x")

    with pytest.raises(LakeRootIdentityError):
        run_init(tmp_path, _ROOT_A)


def test_run_stamp_refuses_without_force(tmp_path: Path) -> None:
    with pytest.raises(LakeRootIdentityError, match="force"):
        run_stamp(tmp_path, _ROOT_A, force=False)


def test_run_stamp_stamps_a_populated_root_when_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    container = tmp_path / "lake"
    (container / "raw").mkdir(parents=True)

    run_stamp(tmp_path, _ROOT_A, force=True)

    marker = read_marker(tmp_path)
    assert marker is not None
    assert marker.data_root_id == _ROOT_A
    assert str(_ROOT_A) in capsys.readouterr().out


def test_run_inspect_reports_no_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_inspect(tmp_path)

    out = capsys.readouterr().out
    assert "no root-identity marker" in out.lower()


def test_run_inspect_reports_the_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    init_empty_root(tmp_path, _ROOT_A)
    capsys.readouterr()  # discard init's own output

    run_inspect(tmp_path)

    assert str(_ROOT_A) in capsys.readouterr().out
