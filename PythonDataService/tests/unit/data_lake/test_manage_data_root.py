"""CLI wrapper over app.data_lake.root_identity's administrative functions.

Issue #1876: "An administrative command exists for init / stamp-with-
explicit-flag / inspect." These tests drive the parsed argparse actions
directly (same style as test_migrate_lake_to_mode_roots.py) rather than
shelling out to main(), so failures point at the actual function.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import pytest

from app.data_lake.root_identity import LEGACY_ROOT_ID, LakeRootIdentityError, init_empty_root, read_marker
from scripts.manage_data_root import run_init, run_inspect, run_stamp

_ROOT_A = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _capture_info_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Fix 3 (#1876 review): the CLI logs via the structured logger, not
    # print() — capture at INFO so these tests can assert on that output.
    caplog.set_level(logging.INFO, logger="manage_data_root")


def test_run_init_creates_a_marker_for_an_empty_root(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    run_init(tmp_path, _ROOT_A)

    marker = read_marker(tmp_path)
    assert marker is not None
    assert marker.data_root_id == _ROOT_A
    assert str(_ROOT_A) in caplog.text


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
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A populated root may only be force-stamped with LEGACY_ROOT_ID (#1876
    # review fix 4) — the migration backfilled existing catalog rows to
    # that id, so this is the legitimate rollout path, not an arbitrary UUID.
    container = tmp_path / "lake"
    (container / "raw").mkdir(parents=True)

    run_stamp(tmp_path, LEGACY_ROOT_ID, force=True)

    marker = read_marker(tmp_path)
    assert marker is not None
    assert marker.data_root_id == LEGACY_ROOT_ID
    assert str(LEGACY_ROOT_ID) in caplog.text


def test_run_inspect_reports_no_marker(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    run_inspect(tmp_path)

    assert "no root-identity marker" in caplog.text.lower()


def test_run_inspect_reports_the_marker(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    init_empty_root(tmp_path, _ROOT_A)
    caplog.clear()  # discard init's own log record

    run_inspect(tmp_path)

    assert str(_ROOT_A) in caplog.text
