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
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.installation_migration.facts import bots_not_stopped
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


def _record_intent(root: Path, sid: str, state: DesiredState) -> None:
    DesiredStateRepo(stable_desired_state_path(root, sid)).set(
        state, updated_by="earlier-operator", now_ms=1, reason="test"
    )


@pytest.mark.asyncio
async def test_idle_bots_whose_intent_still_says_run_are_recorded_stopped(
    tmp_path: Path,
) -> None:
    """#2269: a bot with no live task whose durable intent is RUNNING (a crash,
    a restart that never resumed it) or PAUSED would still read as a bot that
    wants to run; the lane-wide stop records STOPPED for it, in the receipt."""
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    live = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    _record_intent(tmp_path, "bot-crashed", DesiredState.RUNNING)
    _record_intent(tmp_path, "bot-paused", DesiredState.PAUSED)
    _record_intent(tmp_path, "bot-stopped", DesiredState.STOPPED)
    stopped_version = DesiredStateRepo(stable_desired_state_path(tmp_path, "bot-stopped")).read()

    outcome = await registry.stop_every_running_bot(
        updated_by="inkant", reason="lane_stop_all"
    )

    assert [(bot.strategy_instance_id, bot.run_id) for bot in outcome.stopped] == [
        (_SID, live.active_run_id)
    ]
    assert [
        (bot.strategy_instance_id, bot.previous_desired_state) for bot in outcome.intent_stopped
    ] == [("bot-crashed", "RUNNING"), ("bot-paused", "PAUSED")]
    assert outcome.refused == ()
    for sid in ("bot-crashed", "bot-paused"):
        record = DesiredStateRepo(stable_desired_state_path(tmp_path, sid)).read()
        assert record is not None
        assert record.desired_state is DesiredState.STOPPED
        assert record.updated_by == "inkant"
        assert record.reason == "lane_stop_all"
    # Already STOPPED is left exactly as it was: no rewrite, no receipt entry.
    assert (
        DesiredStateRepo(stable_desired_state_path(tmp_path, "bot-stopped")).read()
        == stopped_version
    )


@pytest.mark.asyncio
async def test_after_the_lane_wide_stop_export_finds_no_bot_that_wants_to_run(
    tmp_path: Path,
) -> None:
    """The sweep reads the volume exactly as export's copy check does, so the
    check that used to refuse — with no way out — now passes."""
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    _record_intent(tmp_path, "bot-crashed", DesiredState.RUNNING)
    assert bots_not_stopped({"lane": tmp_path}) == [
        {"volume": "lane", "strategy_instance_id": "bot-crashed", "desired_state": "RUNNING"}
    ]

    await registry.stop_every_running_bot(updated_by="inkant", reason="lane_stop_all")

    assert bots_not_stopped({"lane": tmp_path}) == []


@pytest.mark.asyncio
async def test_an_unreadable_recorded_intent_is_a_refusal_never_skipped(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    _record_intent(tmp_path, "bot-crashed", DesiredState.RUNNING)
    corrupt = stable_desired_state_path(tmp_path, "bot-corrupt")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json", encoding="utf-8")

    outcome = await registry.stop_every_running_bot(
        updated_by="inkant", reason="lane_stop_all"
    )

    [refusal] = outcome.refused
    assert refusal.strategy_instance_id == "bot-corrupt"
    assert refusal.run_id is None
    assert "DesiredStateCorruptError" in refusal.message
    # The other idle bot is still recorded stopped.
    assert [bot.strategy_instance_id for bot in outcome.intent_stopped] == ["bot-crashed"]
