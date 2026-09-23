"""The lane-scoped operator stop of every running bot (#2268).

``stop_all`` is service shutdown: it preserves operator intent so the bots
want to run again after a restart. Installation migration needs the opposite
— the operator's Stop applied to every live task, so each bot's durable
desired state reads ``STOPPED`` and the bots stay stopped on the new host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError
from app.services.bot_runner import RunAdmissionRefusedError
from app.services.bot_runner_errors import BotRunnerError
from tests._helpers.bot_runner.custody import _SID, _registry
from tests._helpers.bot_runner.doubles import _FakeFeed

_OTHER_SID = f"{_SID}-b"


@pytest.mark.asyncio
async def test_stop_every_running_bot_stops_each_task_with_durable_stopped_intent(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    first = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    second = await registry.deploy(
        broker="alpaca", strategy_instance_id=_OTHER_SID, symbol="QQQ"
    )

    outcome = await registry.stop_every_running_bot(
        updated_by="inkant", reason="lane_stop_all"
    )

    assert {(bot.strategy_instance_id, bot.run_id) for bot in outcome.stopped} == {
        (_SID, first.active_run_id),
        (_OTHER_SID, second.active_run_id),
    }
    assert outcome.refused == ()
    assert outcome.still_running is False
    assert registry.any_running() is False
    for sid in (_SID, _OTHER_SID):
        view = registry.status("alpaca", sid)
        assert view.running is False
        # Unlike service shutdown, the operator's intent is now STOPPED.
        assert view.desired_state == "STOPPED"


@pytest.mark.asyncio
async def test_stop_every_running_bot_on_an_idle_lane_stops_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    outcome = await registry.stop_every_running_bot(
        updated_by="inkant", reason="lane_stop_all"
    )

    assert outcome.stopped == ()
    assert outcome.refused == ()
    assert outcome.still_running is False


@pytest.mark.asyncio
async def test_a_refused_stop_is_reported_and_the_lane_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    real_stop = registry.stop

    async def refusing_stop(broker: str, strategy_instance_id: str, **kwargs: object):
        if strategy_instance_id == _SID:
            raise RunAdmissionRefusedError(
                "custody unavailable", detail="broker custody must be restored"
            )
        return await real_stop(broker, strategy_instance_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "stop", refusing_stop)

    outcome = await registry.stop_every_running_bot(
        updated_by="inkant", reason="lane_stop_all"
    )

    assert outcome.stopped == ()
    assert [(bot.strategy_instance_id, bot.message) for bot in outcome.refused] == [
        (_SID, "custody unavailable")
    ]
    assert outcome.still_running is True
    monkeypatch.setattr(registry, "stop", real_stop)
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        BotRunnerError("runner fault", detail="typed runner refusal"),
        ClerkSqliteError("clerk database refused the STOP"),
    ],
    ids=["bot_runner_error", "clerk_error"],
)
async def test_any_per_bot_stop_failure_is_recorded_and_the_other_bots_still_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    other = await registry.deploy(
        broker="alpaca", strategy_instance_id=_OTHER_SID, symbol="QQQ"
    )
    real_stop = registry.stop

    async def failing_stop(broker: str, strategy_instance_id: str, **kwargs: object):
        if strategy_instance_id == _SID:
            raise failure
        return await real_stop(broker, strategy_instance_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "stop", failing_stop)

    outcome = await registry.stop_every_running_bot(
        updated_by="inkant", reason="lane_stop_all"
    )

    assert [(bot.strategy_instance_id, bot.run_id) for bot in outcome.stopped] == [
        (_OTHER_SID, other.active_run_id)
    ]
    [refusal] = outcome.refused
    assert refusal.strategy_instance_id == _SID
    assert str(failure) in refusal.message
    assert outcome.still_running is True
    monkeypatch.setattr(registry, "stop", real_stop)
    await registry.stop("alpaca", _SID)
