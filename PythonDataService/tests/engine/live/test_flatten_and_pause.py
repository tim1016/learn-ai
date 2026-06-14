"""Phase 6A / VCR-0007 / ADR 0010 — FLATTEN_NOW stays pure; the
"Flatten and pause" panic-button composition writes ``desired_state=PAUSED``
BEFORE enqueueing the one-shot.
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.config import LiveConfig
from app.engine.live.desired_state import DesiredState
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


def _bar(minute: int) -> TradeBar:
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


class _NoopStrategy(Strategy):
    def __init__(self) -> None:
        super().__init__()

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        return None


@pytest.mark.asyncio
async def test_flatten_does_not_mutate_desired_state(tmp_path: Path) -> None:
    """Phase 6A / VCR-0007 — ``FLATTEN`` on ``command_channel`` is now pure.
    It must NOT write ``desired_state = STOPPED`` like it did before; the
    durable intent is the operator's, set by the composing endpoint."""
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.FLATTEN)

    written_states: list[DesiredState] = []

    def _capture(state: DesiredState, reason: str) -> None:
        written_states.append(state)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=_capture,
    )

    bars = [_bar(minute) for minute in range(30, 200)]
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    assert written_states == [], (
        "FLATTEN must not mutate desired_state — VCR-0007 closed the alias to STOP. "
        f"Saw writes: {[s.value for s in written_states]}"
    )


@pytest.mark.asyncio
async def test_flatten_does_not_terminate_the_process(tmp_path: Path) -> None:
    """Phase 6A — ``FLATTEN_NOW`` runs WITHIN the bar loop and the engine
    continues processing further bars. The pre-Phase-6A code set
    ``shutdown_event`` and exited."""
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.FLATTEN)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    bars = [_bar(minute) for minute in range(30, 60)]
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    # FLATTEN was acked — confirm by sidecar file.
    ack_files = sorted(commands_dir.glob("command.*.FLATTEN.ack.json"))
    assert len(ack_files) == 1
    outcome = _json.loads(ack_files[0].read_text(encoding="utf-8"))["outcome"]
    assert outcome["status"] == "accepted"
    assert outcome["effect"] == "flatten_now_queued"


@pytest.mark.asyncio
async def test_flatten_ack_remains_accepted_not_shutdown(tmp_path: Path) -> None:
    """The outcome payload no longer says ``shutdown_signalled_with_flatten``
    — VCR-0007's specific lie was the ack claiming a shutdown happened."""
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.FLATTEN)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    bars = [_bar(minute) for minute in range(30, 60)]
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    ack_files = sorted(commands_dir.glob("command.*.FLATTEN.ack.json"))
    outcome = _json.loads(ack_files[0].read_text(encoding="utf-8"))["outcome"]
    assert "shutdown" not in outcome.get("effect", "")
    assert "stopped" not in outcome.get("effect", "").lower()
