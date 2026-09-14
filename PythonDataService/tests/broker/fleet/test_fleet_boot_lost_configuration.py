"""A lost fleet configuration must not revert to the unfenced posture in silence.

``combined`` is the legacy posture and stays permitted — a fresh clone that has
never been enrolled boots exactly as it always did. But an *enrolled* volume with
no fleet configuration is a different fact: that deployment had fleet
configuration and lost it. The isolation the volume was enrolled for is no longer
in effect, and nothing else about the boot looks unusual.

The running fleet's role assignment lives only in an untracked compose overlay,
so "the configuration went missing" is a reachable production state, not a
hypothetical. These tests pin that the case is announced.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.fleet_boot import open_fleet_lane
from app.broker.fleet import volume as volume_module
from app.config import FleetSettings

_LOST_CONFIGURATION = "fleet_configuration_missing_on_enrolled_volume"


def _mark_enrolled(root: Path) -> None:
    marker = volume_module.marker_path(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")


def _legacy_settings() -> FleetSettings:
    """``combined`` with no control directory and no coordinator URL."""
    return FleetSettings(ROLE="combined", CONTROL_DIR=None, COORDINATOR_URL=None)


@pytest.mark.asyncio
async def test_enrolled_volume_without_fleet_configuration_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _mark_enrolled(tmp_path)

    with caplog.at_level(logging.WARNING):
        boot = await open_fleet_lane(settings=_legacy_settings(), volume_root=tmp_path)

    # Behaviour is unchanged: the legacy posture is still permitted.
    assert boot is None

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and getattr(record, "action", None) == _LOST_CONFIGURATION
    ]
    assert warnings, (
        "An enrolled volume booting with no fleet configuration must be announced. "
        "Silently running the unfenced posture over a fenced volume is exactly the "
        "failure that makes a lost deployment overlay undetectable."
    )
    assert "UNFENCED" in warnings[0].getMessage()
    assert getattr(warnings[0], "next_step", None)


@pytest.mark.asyncio
async def test_unenrolled_volume_stays_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A never-enrolled volume is a legitimate legacy install, not a regression."""
    with caplog.at_level(logging.WARNING):
        boot = await open_fleet_lane(settings=_legacy_settings(), volume_root=tmp_path)

    assert boot is None
    assert not [
        record
        for record in caplog.records
        if getattr(record, "action", None) == _LOST_CONFIGURATION
    ]
