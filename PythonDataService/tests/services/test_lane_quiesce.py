"""The lane half of installation migration's quiesce step (#2268).

Two lane-local acts back ``migrate-installation export``: stop every bot on
the lane with a durable, auditable receipt, and answer the account-quiet read
(#2154's canonical reader) without the lane being drained.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.fleet_boot import LaneQuietAnswer
from app.services import lane_quiesce
from app.services.bot_runner import LaneStopOutcome, LaneStoppedBot, LaneStopRefusal
from app.services.lane_quiesce import (
    LANE_STOP_ALL_REASON,
    STOP_ALL_RECEIPTS_DIRECTORY,
    LaneAccountQuietSource,
    read_lane_account_quiet,
    stop_all_bots_on_lane,
)

_T0 = 1_788_040_000_000


class _Clock:
    def __init__(self) -> None:
        self.now_ms = _T0

    def __call__(self) -> int:
        self.now_ms += 1_000
        return self.now_ms


class _FakeRegistry:
    def __init__(self, root: Path, outcome: LaneStopOutcome) -> None:
        self.artifacts_root = root
        self._outcome = outcome
        self.calls: list[tuple[str, str]] = []

    async def stop_every_running_bot(self, *, updated_by: str, reason: str) -> LaneStopOutcome:
        self.calls.append((updated_by, reason))
        return self._outcome


@pytest.mark.asyncio
async def test_stop_all_writes_a_durable_receipt_naming_every_stopped_bot(
    tmp_path: Path,
) -> None:
    registry = _FakeRegistry(
        tmp_path,
        LaneStopOutcome(
            stopped=(LaneStoppedBot(strategy_instance_id="ema-1", run_id="run-1"),),
            refused=(),
            still_running=False,
        ),
    )

    receipt = await stop_all_bots_on_lane(
        registry, operator="inkant", change_ref="migrate-2026-09-22", clock=_Clock()
    )

    assert registry.calls == [("inkant", LANE_STOP_ALL_REASON)]
    assert receipt.all_stopped is True
    assert receipt.requested_at_ms == _T0 + 1_000
    assert receipt.completed_at_ms == _T0 + 2_000
    written = list((tmp_path / STOP_ALL_RECEIPTS_DIRECTORY).glob("*.json"))
    assert len(written) == 1
    durable = json.loads(written[0].read_text(encoding="utf-8"))
    assert durable == receipt.to_json()
    assert durable["operator"] == "inkant"
    assert durable["change_ref"] == "migrate-2026-09-22"
    assert durable["stopped"] == [{"strategy_instance_id": "ema-1", "run_id": "run-1"}]
    assert durable["refused"] == []
    assert durable["still_running"] is False
    assert isinstance(durable["requested_at_ms"], int)


@pytest.mark.asyncio
async def test_an_incomplete_stop_is_still_recorded_and_says_so(tmp_path: Path) -> None:
    registry = _FakeRegistry(
        tmp_path,
        LaneStopOutcome(
            stopped=(),
            refused=(
                LaneStopRefusal(
                    strategy_instance_id="ema-1",
                    run_id="run-1",
                    message="custody unavailable",
                    detail=None,
                ),
            ),
            still_running=True,
        ),
    )

    receipt = await stop_all_bots_on_lane(
        registry, operator="inkant", change_ref="migrate", clock=_Clock()
    )

    assert receipt.all_stopped is False
    durable = json.loads(
        next((tmp_path / STOP_ALL_RECEIPTS_DIRECTORY).glob("*.json")).read_text(encoding="utf-8")
    )
    assert durable["refused"] == [
        {
            "strategy_instance_id": "ema-1",
            "run_id": "run-1",
            "message": "custody unavailable",
            "detail": None,
        }
    ]
    assert durable["still_running"] is True


@pytest.mark.asyncio
async def test_two_stops_write_two_receipts_never_overwriting(tmp_path: Path) -> None:
    registry = _FakeRegistry(
        tmp_path, LaneStopOutcome(stopped=(), refused=(), still_running=False)
    )
    clock = _Clock()

    await stop_all_bots_on_lane(registry, operator="a", change_ref="c", clock=clock)
    await stop_all_bots_on_lane(registry, operator="a", change_ref="c", clock=clock)

    assert len(list((tmp_path / STOP_ALL_RECEIPTS_DIRECTORY).glob("*.json"))) == 2


def _answer(**unsatisfied: bool) -> LaneQuietAnswer:
    conditions = {
        "runner_idle": True,
        "broker_work_ended": True,
        "account_flat": True,
        "intents_resolved": True,
    }
    conditions.update(unsatisfied)
    return LaneQuietAnswer(observed_at_ms=_T0, **conditions)


@pytest.mark.asyncio
async def test_account_quiet_read_returns_the_canonical_probe_answer() -> None:
    async def probe() -> LaneQuietAnswer:
        return _answer(account_flat=False)

    answer = await read_lane_account_quiet(LaneAccountQuietSource(account_id="PA1", probe=probe))

    assert answer is not None
    assert answer.outstanding == ("the account is not flat",)


@pytest.mark.asyncio
async def test_account_quiet_read_that_cannot_observe_is_no_answer() -> None:
    async def probe() -> None:
        return None

    assert await read_lane_account_quiet(LaneAccountQuietSource(account_id="PA1", probe=probe)) is None


@pytest.mark.asyncio
async def test_account_quiet_read_that_times_out_is_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lane_quiesce, "ACCOUNT_QUIET_READ_TIMEOUT_S", 0.01)

    async def probe() -> LaneQuietAnswer:
        await asyncio.sleep(1)
        return _answer()

    assert await read_lane_account_quiet(LaneAccountQuietSource(account_id="PA1", probe=probe)) is None
