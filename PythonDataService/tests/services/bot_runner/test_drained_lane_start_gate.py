"""#2155: a lane that has learned its drain starts no new runs.

The fleet heartbeat sets the lane's drained flag; this is the runner-side
half — the gate refuses before any admission work runs, so a drained lane's
operator sees the drain refusal itself, not a downstream error dressed up
as one, and a non-fleet deployment (no gate) is untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from app.services.bot_runner import (
    BotTaskRegistry,
    MarketDataFeedUnavailableError,
    RunAdmissionRefusedError,
)
from tests._helpers.bot_runner.custody import _SID


def _registry(tmp_path: Path, gate: Callable[[], bool] | None) -> BotTaskRegistry:
    return BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        drained_lane_gate=gate,
    )


async def test_a_drained_lane_refuses_every_new_start_before_admission(
    tmp_path: Path,
) -> None:
    """With no feed at all, the drained refusal — not a feed error — answers."""
    registry = _registry(tmp_path, gate=lambda: True)
    with pytest.raises(RunAdmissionRefusedError, match="drained"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    with pytest.raises(RunAdmissionRefusedError, match="drained"):
        await registry.resume_existing_with_admission("alpaca", _SID)


async def test_the_gate_stays_silent_until_the_lane_learns_its_drain(
    tmp_path: Path,
) -> None:
    """The same registry flips from serving to refusing the moment the
    heartbeat's flag does — no restart, no deployment change."""
    drained = False
    registry = _registry(tmp_path, gate=lambda: drained)
    with pytest.raises(MarketDataFeedUnavailableError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    drained = True
    with pytest.raises(RunAdmissionRefusedError, match="drained"):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
