"""#2269: a lane restored by installation migration starts no bot until go-live.

``migrate-installation import`` writes a hold marker at each clerk volume's
root; the runner's start seam — beside the drained-lane gate — refuses every
Start and Resume while it exists, before any admission work runs, and fails
closed when it cannot tell. Releasing the hold lands on the next start with
no restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.routers.broker_bots import _raise_runner_error
from app.services.bot_runner import (
    BotTaskRegistry,
    MarketDataFeedUnavailableError,
    RunAdmissionRefusedError,
    fleet_lane_start_gate,
    go_live_start_gate,
)
from app.services.bot_runner_errors import (
    LANE_GO_LIVE_HOLD_UNREADABLE,
    LANE_GO_LIVE_PENDING,
)
from app.services.go_live_hold import (
    GO_LIVE_HOLD_MARKER,
    GoLiveHoldMarker,
    go_live_marker_bytes,
    read_go_live_hold,
    release_go_live_hold,
)
from tests._helpers.bot_runner.custody import _SID

_MARKER = GoLiveHoldMarker(
    kind="learn-ai-go-live-hold",
    schema_version=1,
    written_at_ms=1_789_100_000_000,
    volume="learn-ai-alpaca-clerk-data",
    source_commit="a" * 40,
    registry_id="reg_1",
)


def _registry(lane_root: Path) -> BotTaskRegistry:
    """No feed at all: a start that passes the lane gates fails on the feed."""
    return BotTaskRegistry(
        lane_root,
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        lane_start_gates=(go_live_start_gate(lambda: read_go_live_hold(lane_root)),),
    )


def _hold(lane_root: Path) -> None:
    (lane_root / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(_MARKER))


async def test_a_held_lane_refuses_every_start_and_resume_before_admission(
    tmp_path: Path,
) -> None:
    _hold(tmp_path)
    registry = _registry(tmp_path)

    with pytest.raises(RunAdmissionRefusedError, match="awaits go-live") as start:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    with pytest.raises(RunAdmissionRefusedError, match="awaits go-live") as resume:
        await registry.resume_existing_with_admission("alpaca", _SID)

    assert start.value.reason_code == LANE_GO_LIVE_PENDING
    assert resume.value.reason_code == LANE_GO_LIVE_PENDING
    assert "go-live" in (start.value.detail or "")


async def test_releasing_the_hold_lands_on_the_next_start_without_a_restart(
    tmp_path: Path,
) -> None:
    _hold(tmp_path)
    registry = _registry(tmp_path)
    with pytest.raises(RunAdmissionRefusedError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    release_go_live_hold(tmp_path, operator="inkant", change_ref="go-live", bar_check={})

    # The hold no longer answers; the next gate (no feed) does.
    with pytest.raises(MarketDataFeedUnavailableError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


async def test_a_lane_that_was_never_held_starts_as_before(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(MarketDataFeedUnavailableError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")


async def test_a_marker_that_does_not_parse_still_holds(tmp_path: Path) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).write_text("{half a marker", encoding="utf-8")
    registry = _registry(tmp_path)

    with pytest.raises(RunAdmissionRefusedError, match="cannot read its go-live hold") as refused:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert refused.value.reason_code == LANE_GO_LIVE_HOLD_UNREADABLE


async def test_a_lane_that_cannot_read_its_root_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "volume-not-mounted"
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        lane_start_gates=(go_live_start_gate(lambda: read_go_live_hold(missing)),),
    )

    with pytest.raises(RunAdmissionRefusedError) as refused:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert refused.value.reason_code == LANE_GO_LIVE_HOLD_UNREADABLE


async def test_the_refusal_reaches_the_wire_with_its_reason_code(tmp_path: Path) -> None:
    _hold(tmp_path)
    registry = _registry(tmp_path)
    with pytest.raises(RunAdmissionRefusedError) as refused:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as translated:
        _raise_runner_error(refused.value)

    assert translated.value.status_code == 409
    assert translated.value.detail["reason_code"] == LANE_GO_LIVE_PENDING


async def test_the_drain_answers_before_the_go_live_hold(tmp_path: Path) -> None:
    _hold(tmp_path)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        lane_start_gates=(
            fleet_lane_start_gate(lambda: "clerk_lane_draining"),
            go_live_start_gate(lambda: read_go_live_hold(tmp_path)),
        ),
    )

    with pytest.raises(RunAdmissionRefusedError, match="lane is drained") as refused:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert refused.value.reason_code != LANE_GO_LIVE_PENDING
