"""A retirement cannot cross a reader or a publisher's critical section."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data_lake.maintenance import LakeMaintenanceBusyError, lake_activity


def test_retirement_excludes_activity_across_adjustment_modes(tmp_path: Path) -> None:
    raw = tmp_path / "lake" / "raw"
    adjusted = tmp_path / "lake" / "polygon_split_adjusted"
    with (
        lake_activity(raw),
        lake_activity(adjusted),
        pytest.raises(LakeMaintenanceBusyError),
        lake_activity(raw, exclusive=True),
    ):
        pytest.fail("retirement acquired an active lake")
    with lake_activity(raw, exclusive=True), pytest.raises(LakeMaintenanceBusyError), lake_activity(adjusted):
        pytest.fail("a publisher entered during retirement")
    with lake_activity(raw):
        pass
